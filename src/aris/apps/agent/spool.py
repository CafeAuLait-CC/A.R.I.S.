import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from aris import __version__
from aris.modules.vega.schemas import GpuSample, TelemetryBatch


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
            for key, value in (("agent_id", agent_id), ("node_id", node_id), ("last_seq", "0")):
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?, ?)", (key, value))
            metadata = dict(db.execute("SELECT key, value FROM metadata"))
            if metadata["agent_id"] != agent_id or metadata["node_id"] != node_id:
                raise ValueError("spool belongs to another agent/node; use a separate spool")

    def enqueue(self, hostname: str, samples: list[GpuSample]) -> TelemetryBatch:
        now = datetime.now(UTC)
        with connection(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM pending").fetchone()[0]
            if count >= self.max_batches:
                raise RuntimeError("agent spool full; unacknowledged data retained")
            seq = (
                int(db.execute("SELECT value FROM metadata WHERE key='last_seq'").fetchone()[0]) + 1
            )
            batch = TelemetryBatch(
                message_id=uuid4(),
                agent_id=self.agent_id,
                node_id=self.node_id,
                hostname=hostname,
                agent_version=__version__,
                seq=seq,
                observed_at=now,
                sent_at=now,
                gpus=samples,
            )
            db.execute("INSERT INTO pending VALUES (?, ?)", (seq, batch.model_dump_json()))
            db.execute("UPDATE metadata SET value=? WHERE key='last_seq'", (str(seq),))
            return batch

    def pending(self) -> list[TelemetryBatch]:
        with connection(self.path) as db:
            return [
                TelemetryBatch.model_validate_json(row[0])
                for row in db.execute("SELECT payload FROM pending ORDER BY seq")
            ]

    def acknowledge(self, batch: TelemetryBatch) -> None:
        with connection(self.path) as db:
            db.execute(
                "DELETE FROM pending WHERE seq=? AND payload=?",
                (batch.seq, batch.model_dump_json()),
            )
