"""One Collector owns a spool; never hold a SQLite transaction during network I/O."""

import fcntl
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class CollectorAlreadyRunning(RuntimeError):
    pass


@contextmanager
def collector_lock(spool_path: Path) -> Iterator[None]:
    path = spool_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the sidecar inode on release. Unlinking it could let a third process
    # lock a different inode while another process still owns the original one.
    with path.with_name(path.name + ".lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise CollectorAlreadyRunning("Collector already owns this spool") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
