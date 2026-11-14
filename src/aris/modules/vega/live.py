"""One-process, bounded RAM relay. No database, spool, events or audit writes."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from aris.core.contracts import DomainError
from aris.modules.vega.live_schemas import (
    LiveFrame,
    LiveHello,
    LiveNode,
    LiveSnapshot,
    PreviewLease,
)


def signal(queue: asyncio.Queue[None]) -> None:
    if queue.empty():
        queue.put_nowait(None)


@dataclass
class CollectorConnection:
    hello: LiveHello
    control: asyncio.Queue[None] = field(default_factory=lambda: asyncio.Queue(maxsize=1))
    frame: LiveFrame | None = None
    received: float = 0
    connected: bool = True


@dataclass
class Viewer:
    expires: float
    changed: asyncio.Queue[None] = field(default_factory=lambda: asyncio.Queue(maxsize=1))


class LiveHub:
    """Owned by the API event loop; authenticated callers supply configured identities."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_viewers: int = 32) -> None:
        self.clock = clock
        self.max_viewers = max_viewers
        self.collectors: dict[str, CollectorConnection] = {}
        self.viewers: dict[UUID, Viewer] = {}

    def active(self) -> bool:
        now = self.clock()
        self.viewers = {key: viewer for key, viewer in self.viewers.items() if viewer.expires > now}
        return bool(self.viewers)

    def notify(self) -> None:
        for viewer in self.viewers.values():
            signal(viewer.changed)

    def controls_changed(self) -> None:
        for collector in self.collectors.values():
            if collector.connected:
                signal(collector.control)

    def attach(self, hello: LiveHello) -> CollectorConnection:
        previous = self.collectors.get(hello.node_id)
        if previous and previous.connected:
            # A reconnect cannot let an old socket overwrite its replacement.
            previous.connected = False
            signal(previous.control)
        connection = CollectorConnection(hello)
        self.collectors[hello.node_id] = connection
        signal(connection.control)
        self.notify()
        return connection

    def detach(self, connection: CollectorConnection) -> None:
        connection.connected = False
        if self.collectors.get(connection.hello.node_id) is connection:
            self.notify()

    def accept(self, connection: CollectorConnection, frame: LiveFrame) -> None:
        now = self.clock()
        if (
            not connection.connected
            or self.collectors.get(connection.hello.node_id) is not connection
            or frame.session_id != connection.hello.session_id
            or (connection.frame is not None and frame.seq <= connection.frame.seq)
        ):
            raise ValueError("invalid preview session or sequence")
        if not self.active():
            # Allow the one in-flight frame that races the stop notification.
            return
        if connection.frame is not None and now - connection.received < 0.4:
            raise ValueError("preview rate exceeded")
        if (frame.observed_at - datetime.now(UTC)).total_seconds() > 60:
            raise ValueError("preview observation is in the future")
        connection.frame = frame
        connection.received = now
        self.notify()

    def lease(self, connection: CollectorConnection) -> PreviewLease:
        return PreviewLease(lease_seconds=30 if connection.connected and self.active() else 0)

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[tuple[UUID, Viewer]]:
        key, viewer = self.open_viewer()
        try:
            yield key, viewer
        finally:
            self.close_viewer(key)

    def open_viewer(self) -> tuple[UUID, Viewer]:
        self.active()
        if len(self.viewers) >= self.max_viewers:
            raise DomainError("CAPACITY_EXCEEDED", "Too many preview subscribers", 429)
        key = uuid4()
        viewer = Viewer(self.clock() + 25)
        self.viewers[key] = viewer
        self.controls_changed()
        return key, viewer

    def close_viewer(self, key: UUID) -> None:
        self.viewers.pop(key, None)
        self.controls_changed()

    def renew(self, key: UUID, viewer: Viewer) -> bool:
        if self.viewers.get(key) is not viewer or viewer.expires <= self.clock():
            self.close_viewer(key)
            return False
        viewer.expires = self.clock() + 25
        return True

    def snapshot(self) -> LiveSnapshot:
        now = datetime.now(UTC)
        nodes: list[LiveNode] = []
        for connection in self.collectors.values():
            frame = connection.frame
            quality: Literal["live", "waiting", "stale", "error", "offline"] = "waiting"
            if not connection.connected:
                quality = "offline"
            elif frame is not None:
                age = (now - frame.observed_at).total_seconds()
                if self.clock() - connection.received > 3 or age > 3 or age < -3:
                    quality = "stale"
                else:
                    quality = "live" if frame.quality == "ok" else "error"
            nodes.append(
                LiveNode(
                    node_id=connection.hello.node_id,
                    agent_id=connection.hello.agent_id,
                    hostname=connection.hello.hostname,
                    session_id=connection.hello.session_id,
                    connected=connection.connected,
                    quality=quality,
                    seq=frame.seq if frame else None,
                    observed_at=frame.observed_at if frame else None,
                    gpus=frame.gpus if frame and quality == "live" else [],
                )
            )
        return LiveSnapshot(generated_at=now, nodes=nodes)
