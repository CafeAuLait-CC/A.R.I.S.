import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict
from uuid import uuid4

from aris import __version__
from aris.modules.vega.reports import AgentReport
from aris.modules.vega.resource_schemas import ResourceObservation, ResourceTelemetryBatch
from aris.modules.vega.schemas import Heartbeat


class SpoolStatistics(TypedDict):
    pending_batches: int
    pending_heartbeats: int
    capacity_batches: int
    capacity_heartbeats: int
    last_seq: int
    spool_bytes: int
    reusable_bytes: int
    oldest_pending_age_seconds: float | None
    telemetry_fill_percent: float
    heartbeat_fill_percent: float


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

    def __init__(
        self,
        path: Path,
        agent_id: str,
        node_id: str,
        max_batches: int = 10000,
        max_heartbeats: int | None = None,
    ) -> None:
        if max_batches < 1 or (max_heartbeats is not None and max_heartbeats < 1):
            raise ValueError("spool capacity must be positive")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.agent_id = agent_id
        self.node_id = node_id
        self.max_batches = max_batches
        self.max_heartbeats = max_batches if max_heartbeats is None else max_heartbeats
        with connection(path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS pending "
                "(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL, kind TEXT NOT NULL)"
            )
            for key, value in (("agent_id", agent_id), ("node_id", node_id), ("last_seq", "0")):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            metadata = dict(db.execute("SELECT key, value FROM metadata"))
            if metadata["agent_id"] != agent_id or metadata["node_id"] != node_id:
                raise ValueError("spool belongs to another agent/node; use a separate spool")
            # Validate before collecting; never convert or discard an unsupported backlog.
            for payload, kind in db.execute("SELECT payload, kind FROM pending"):
                self._decode(payload, kind)
            db.execute("CREATE INDEX IF NOT EXISTS ix_pending_kind_seq ON pending(kind, seq)")

    @staticmethod
    def _decode(payload: str, kind: str) -> AgentReport:
        if kind == "heartbeat":
            return Heartbeat.model_validate_json(payload)
        if kind == "telemetry":
            return ResourceTelemetryBatch.model_validate_json(payload)
        raise ValueError("unsupported buffered message kind")

    def enqueue_heartbeat(self, hostname: str, status: Literal["ok", "error"]) -> Heartbeat:
        message = self._enqueue(hostname, status=status)
        assert isinstance(message, Heartbeat)
        return message

    def enqueue_resource(
        self, hostname: str, resources: ResourceObservation
    ) -> ResourceTelemetryBatch:
        message = self._enqueue(hostname, observed_at=resources.observed_at, resources=resources)
        assert isinstance(message, ResourceTelemetryBatch)
        return message

    def _enqueue(
        self,
        hostname: str,
        status: Literal["ok", "error"] = "ok",
        observed_at: datetime | None = None,
        resources: ResourceObservation | None = None,
    ) -> AgentReport:
        now = datetime.now(UTC)
        kind = "heartbeat" if resources is None else "telemetry"
        with connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM pending WHERE kind=?", (kind,)).fetchone()[0]
            # Separate budgets keep health facts observable when telemetry is full.
            if count >= (self.max_heartbeats if kind == "heartbeat" else self.max_batches):
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
            else:
                batch = Heartbeat.model_validate(
                    {
                        **envelope,
                        "collector_status": status,
                        "capabilities": ["node.resources", "heartbeat"],
                    }
                )
            db.execute(
                "INSERT INTO pending (seq, payload, kind) VALUES (?, ?, ?)",
                (seq, batch.model_dump_json(), kind),
            )
            db.execute("UPDATE metadata SET value=? WHERE key='last_seq'", (str(seq),))
            return batch

    def pending(self) -> list[AgentReport]:
        with connection(self.path) as db:
            return [
                self._decode(row[0], row[1])
                for row in db.execute("SELECT payload, kind FROM pending ORDER BY seq")
            ]

    def delivery_batch(self, limit: int) -> list[AgentReport]:
        if not 3 <= limit <= 1000:
            raise ValueError("delivery batch must contain between 3 and 1000 messages")
        with connection(self.path) as db:
            # Refresh liveness/current facts before history, while always advancing history.
            rows = db.execute(
                "SELECT payload, kind FROM pending ORDER BY CASE "
                "WHEN seq=(SELECT max(seq) FROM pending WHERE kind='heartbeat') THEN 0 "
                "WHEN seq=(SELECT max(seq) FROM pending WHERE kind='telemetry') THEN 1 "
                "ELSE 2 END, seq LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._decode(payload, kind) for payload, kind in rows]

    def statistics(self) -> SpoolStatistics:
        with connection(self.path) as db:
            counts = dict(db.execute("SELECT kind, count(*) FROM pending GROUP BY kind"))
            last_seq = int(
                db.execute("SELECT value FROM metadata WHERE key='last_seq'").fetchone()[0]
            )
            oldest = db.execute("SELECT payload,kind FROM pending ORDER BY seq LIMIT 1").fetchone()
            free_pages = db.execute("PRAGMA freelist_count").fetchone()[0]
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
        age = None
        if oldest:
            observed_at = self._decode(*oldest).observed_at
            age = max(0.0, (datetime.now(UTC) - observed_at).total_seconds())
        return {
            "pending_batches": counts.get("telemetry", 0),
            "pending_heartbeats": counts.get("heartbeat", 0),
            "capacity_batches": self.max_batches,
            "capacity_heartbeats": self.max_heartbeats,
            "last_seq": last_seq,
            "spool_bytes": self.path.stat().st_size,
            "reusable_bytes": free_pages * page_size,
            "oldest_pending_age_seconds": age,
            "telemetry_fill_percent": 100 * counts.get("telemetry", 0) / self.max_batches,
            "heartbeat_fill_percent": 100 * counts.get("heartbeat", 0) / self.max_heartbeats,
        }

    def acknowledge(self, batch: AgentReport) -> None:
        with connection(self.path) as db:
            row = db.execute(
                "SELECT payload, kind FROM pending WHERE seq=?", (batch.seq,)
            ).fetchone()
            if row is None:
                return
            stored = self._decode(row[0], row[1])
            if stored != batch:
                return
            db.execute(
                "DELETE FROM pending WHERE seq=? AND payload=?",
                (batch.seq, row[0]),
            )
