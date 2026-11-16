"""Local, read-only backlog diagnostics without starting a Collector or probe."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from aris.apps.agent.spool import Spool, SpoolStatistics

Reason = Literal[
    "missing",
    "unreadable",
    "busy",
    "recovery_required",
    "invalid_schema",
    "invalid_data",
    "identity_mismatch",
    "unsupported_journal_mode",
]


class SpoolInspection(TypedDict):
    status: Literal["ok", "missing", "unknown"]
    reason: Reason | None
    checked_at: str
    read_only: bool
    statistics: SpoolStatistics | None
    oldest_pending_observed_at: str | None
    remaining_sample_cycles: int | None
    estimated_seconds_without_delivery: int | None
    estimate_basis: str
    pending_payloads_checked: int


class _InvalidSpool(Exception):
    def __init__(self, reason: Reason) -> None:
        self.reason = reason


def inspect_spool(
    path: Path,
    agent_id: str,
    node_id: str,
    *,
    max_batches: int = 10000,
    max_heartbeats: int = 10000,
    interval_seconds: int = 15,
    now: datetime | None = None,
) -> SpoolInspection:
    """Estimate remaining slots; this does not verify every historical payload.

    Missing, incompatible, busy or crash-recovery-required files stay unknown.
    No schema initialization, lock file, hardware read, delivery or repair is done.
    WAL is deliberately rejected before SQLite opens it: even a read-only WAL
    connection may need to create shared-memory sidecars. Current Collectors use
    the default rollback journal, whose recovery remains the writer's job.
    """
    if min(max_batches, max_heartbeats, interval_seconds) < 1:
        raise ValueError("capacities and sampling interval must be positive")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("diagnostic time must have a timezone")
    result: SpoolInspection = {
        "status": "unknown",
        "reason": None,
        "checked_at": now.isoformat(),
        "read_only": True,
        "statistics": None,
        "oldest_pending_observed_at": None,
        "remaining_sample_cycles": None,
        "estimated_seconds_without_delivery": None,
        "estimate_basis": "configured interval, one message per kind per cycle, no delivery",
        "pending_payloads_checked": 0,
    }
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise _InvalidSpool("unreadable")
        with resolved.open("rb") as source:
            header = source.read(100)
        if not header.startswith(b"SQLite format 3\x00") or len(header) < 100:
            raise _InvalidSpool("invalid_data")
        if header[18:20] != b"\x01\x01":
            raise _InvalidSpool("unsupported_journal_mode")
        with closing(sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            for table, required in (
                ("metadata", {"key", "value"}),
                ("pending", {"seq", "payload", "kind"}),
            ):
                columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
                if not required <= columns:
                    raise _InvalidSpool("invalid_schema")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            if not {"agent_id", "node_id", "last_seq"} <= metadata.keys():
                raise _InvalidSpool("invalid_data")
            if metadata["agent_id"] != agent_id or metadata["node_id"] != node_id:
                raise _InvalidSpool("identity_mismatch")
            last_seq = int(metadata["last_seq"])
            first_seq, final_seq = db.execute("SELECT min(seq),max(seq) FROM pending").fetchone()
            if last_seq < 0 or (first_seq is not None and (first_seq < 1 or final_seq > last_seq)):
                raise _InvalidSpool("invalid_data")
            counts = dict(db.execute("SELECT kind,count(*) FROM pending GROUP BY kind"))
            if not counts.keys() <= {"telemetry", "heartbeat"}:
                raise _InvalidSpool("invalid_data")
            oldest = db.execute("SELECT payload,kind FROM pending ORDER BY seq LIMIT 1").fetchone()
            observed = Spool._decode(*oldest).observed_at if oldest else None
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            telemetry, heartbeats = counts.get("telemetry", 0), counts.get("heartbeat", 0)
            remaining = max(0, min(max_batches - telemetry, max_heartbeats - heartbeats))
            statistics: SpoolStatistics = {
                "pending_batches": telemetry,
                "pending_heartbeats": heartbeats,
                "capacity_batches": max_batches,
                "capacity_heartbeats": max_heartbeats,
                "last_seq": last_seq,
                "spool_bytes": resolved.stat().st_size,
                "reusable_bytes": free_pages * page_size,
                "oldest_pending_age_seconds": (
                    max(0.0, (now - observed).total_seconds()) if observed else None
                ),
                "telemetry_fill_percent": 100 * telemetry / max_batches,
                "heartbeat_fill_percent": 100 * heartbeats / max_heartbeats,
            }
        result["status"] = "ok"
        result["statistics"] = statistics
        result["oldest_pending_observed_at"] = observed.isoformat() if observed else None
        result["remaining_sample_cycles"] = remaining
        result["estimated_seconds_without_delivery"] = remaining * interval_seconds
        result["pending_payloads_checked"] = int(oldest is not None)
    except FileNotFoundError:
        result["status"] = "missing"
        result["reason"] = "missing"
    except (PermissionError, OSError):
        result["reason"] = "unreadable"
    except _InvalidSpool as exc:
        result["reason"] = exc.reason
    except sqlite3.Error as exc:
        code = getattr(exc, "sqlite_errorname", "")
        result["reason"] = (
            "busy"
            if code in {"SQLITE_BUSY", "SQLITE_LOCKED"}
            else "recovery_required"
            if code.startswith("SQLITE_READONLY")
            else "invalid_data"
        )
    except (ValueError, TypeError, OverflowError):
        result["reason"] = "invalid_data"
    return result
