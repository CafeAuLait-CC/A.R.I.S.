import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from aris import __version__
from aris.modules.vega.reports import TELEMETRY_REPORT, AgentReport
from aris.modules.vega.resource_schemas import ResourceObservation, ResourceTelemetryBatch
from aris.modules.vega.schemas import GpuSample, Heartbeat, TelemetryBatch


@contextmanager
def connection(path: Path) -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class Spool:
    """Persist sequence and exact message bytes before any network request."""

    def __init__(self, path: Path, agent_id: str, node_id: str, max_batches: int = 10000) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.agent_id = agent_id
        self.node_id = node_id
        self.max_batches = max_batches
        with connection(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending "
                "(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(pending)")}
            if "kind" not in columns:
                db.execute("ALTER TABLE pending ADD COLUMN kind TEXT NOT NULL DEFAULT 'telemetry'")
            for key, value in (("agent_id", agent_id), ("node_id", node_id), ("last_seq", "0")):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            metadata = dict(db.execute("SELECT key, value FROM metadata"))
            if metadata["agent_id"] != agent_id or metadata["node_id"] != node_id:
                raise ValueError("spool belongs to another agent/node; use a separate spool")

    def enqueue(
        self, hostname: str, samples: list[GpuSample], observed_at: datetime | None = None
    ) -> TelemetryBatch:
        message = self._enqueue(hostname, samples, observed_at=observed_at)
        assert isinstance(message, TelemetryBatch)
        return message

    def enqueue_heartbeat(
        self, hostname: str, status: Literal["ok", "error"], *, resource_enabled: bool = False
    ) -> Heartbeat:
        message = self._enqueue(hostname, None, status, resource_enabled=resource_enabled)
        assert isinstance(message, Heartbeat)
        return message

    def enqueue_resource(
        self, hostname: str, resources: ResourceObservation
    ) -> ResourceTelemetryBatch:
        message = self._enqueue(
            hostname, None, observed_at=resources.observed_at, resources=resources
        )
        assert isinstance(message, ResourceTelemetryBatch)
        return message

    def _enqueue(
        self,
        hostname: str,
        samples: list[GpuSample] | None,
        status: Literal["ok", "error"] = "ok",
        observed_at: datetime | None = None,
        resources: ResourceObservation | None = None,
        resource_enabled: bool = False,
    ) -> AgentReport:
        now = datetime.now(UTC)
        kind = "heartbeat" if samples is None and resources is None else "telemetry"
        with connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM pending WHERE kind=?", (kind,)).fetchone()[0]
            # One reserved heartbeat slot keeps collection failures observable at capacity.
            if count >= (1 if kind == "heartbeat" else self.max_batches):
                raise RuntimeError("agent spool full; unacknowledged data retained")
            seq = (
                int(db.execute("SELECT value FROM metadata WHERE key='last_seq'").fetchone()[0]) + 1
            )
            envelope = dict(
                message_id=uuid4(),
                agent_id=self.agent_id,
                node_id=self.node_id,
                hostname=hostname,
                agent_version=__version__,
                seq=seq,
                observed_at=observed_at or now,
                sent_at=now,
            )
            batch: AgentReport
            if resources is not None:
                batch = ResourceTelemetryBatch.model_validate({**envelope, "resources": resources})
            elif samples is None:
                batch = Heartbeat.model_validate(
                    {
                        **envelope,
                        "collector_status": status,
                        "capabilities": [
                            "node.resources/v2" if resource_enabled else "gpu.telemetry/v1",
                            "agent.heartbeat/v1",
                        ],
                    }
                )
            else:
                batch = TelemetryBatch.model_validate({**envelope, "gpus": samples})
            db.execute(
                "INSERT INTO pending (seq, payload, kind) VALUES (?, ?, ?)",
                (seq, batch.model_dump_json(), kind),
            )
            db.execute("UPDATE metadata SET value=? WHERE key='last_seq'", (str(seq),))
            return batch

    def pending(self) -> list[AgentReport]:
        with connection(self.path) as db:
            return [
                (
                    Heartbeat.model_validate_json(row[0])
                    if row[1] == "heartbeat"
                    else TELEMETRY_REPORT.validate_json(row[0])
                )
                for row in db.execute("SELECT payload, kind FROM pending ORDER BY seq")
            ]

    def delivery_batch(self, limit: int) -> list[AgentReport]:
        if not 3 <= limit <= 1000:
            raise ValueError("delivery batch must contain between 3 and 1000 messages")
        with connection(self.path) as db:
            # Refresh liveness/current facts before history, while always advancing history.
            rows = db.execute(
                "SELECT payload, kind FROM pending ORDER BY CASE "
                "WHEN kind='heartbeat' THEN 0 "
                "WHEN seq=(SELECT max(seq) FROM pending WHERE kind='telemetry') THEN 1 "
                "ELSE 2 END, seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            (
                Heartbeat.model_validate_json(payload)
                if kind == "heartbeat"
                else TELEMETRY_REPORT.validate_json(payload)
            )
            for payload, kind in rows
        ]

    def statistics(self) -> dict[str, int]:
        with connection(self.path) as db:
            counts = dict(db.execute("SELECT kind, count(*) FROM pending GROUP BY kind"))
            last_seq = int(
                db.execute("SELECT value FROM metadata WHERE key='last_seq'").fetchone()[0]
            )
        return {
            "pending_batches": counts.get("telemetry", 0),
            "pending_heartbeats": counts.get("heartbeat", 0),
            "capacity_batches": self.max_batches,
            "last_seq": last_seq,
            "spool_bytes": self.path.stat().st_size,
        }

    def acknowledge(self, batch: AgentReport) -> None:
        with connection(self.path) as db:
            row = db.execute(
                "SELECT payload, kind FROM pending WHERE seq=?", (batch.seq,)
            ).fetchone()
            if row is None:
                return
            stored = (
                Heartbeat.model_validate_json(row[0])
                if row[1] == "heartbeat"
                else TELEMETRY_REPORT.validate_json(row[0])
            )
            if stored != batch:
                return
            db.execute(
                "DELETE FROM pending WHERE seq=? AND payload=?",
                (batch.seq, row[0]),
            )
