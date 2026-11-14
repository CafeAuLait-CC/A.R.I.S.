import logging
import random
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from websockets.sync.client import connect

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.sampler import Sampler
from aris.modules.vega.live_schemas import LiveFrame, LiveHello, PreviewLease

logger = logging.getLogger(__name__)


class PreviewSession:
    """A bounded read-only lease; there is no shell or generic command dispatch."""

    def __init__(
        self,
        sampler: Sampler,
        session_id: UUID,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.sampler = sampler
        self.session_id = session_id
        self.clock = clock
        self.deadline = 0.0
        self.interval = 1
        self.next_sample = 0.0
        self.seq = 0

    def apply(self, lease: PreviewLease) -> None:
        self.deadline = self.clock() + lease.lease_seconds if lease.lease_seconds else 0
        self.interval = lease.interval_seconds

    def frame(self) -> LiveFrame | None:
        now = self.clock()
        if now >= self.deadline or now < self.next_sample:
            return None
        self.next_sample = now + self.interval
        self.seq += 1
        try:
            observation = self.sampler.sample()
            frame = LiveFrame(
                session_id=self.session_id,
                seq=self.seq,
                observed_at=observation.observed_at,
                quality="ok",
                gpus=observation.gpus,
            )
        except Exception:
            frame = LiveFrame(
                session_id=self.session_id,
                seq=self.seq,
                observed_at=datetime.now(UTC),
                quality="error",
            )
        # A slow driver call may have outlived its lease. Never renew it ourselves.
        return frame if self.clock() < self.deadline else None


def live_url(settings: AgentSettings) -> str:
    base = urlsplit(settings.api_url)
    return urlunsplit(
        (
            "wss" if base.scheme == "https" else "ws",
            base.netloc,
            base.path.rstrip("/") + "/vega/v1/live",
            urlencode({"agent_id": settings.id, "node_id": settings.node_id}),
            "",
        )
    )


class LiveClient:
    def __init__(self, settings: AgentSettings, sampler: Sampler) -> None:
        self.settings = settings
        self.sampler = sampler
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name="vega-live-preview", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=2)

    def run(self) -> None:
        failures = 0
        while not self.stopped.is_set():
            try:
                with connect(
                    live_url(self.settings),
                    additional_headers={"X-Agent-Token": self.settings.token.get_secret_value()},
                    proxy=None,
                    open_timeout=10,
                    close_timeout=2,
                    ping_interval=20,
                    ping_timeout=20,
                    max_size=2048,
                    max_queue=2,
                    compression=None,
                ) as websocket:
                    hello = LiveHello(
                        agent_id=self.settings.id,
                        node_id=self.settings.node_id,
                        hostname=socket.gethostname(),
                        session_id=uuid4(),
                    )
                    session = PreviewSession(self.sampler, hello.session_id)
                    websocket.send(hello.model_dump_json())
                    failures = 0
                    while not self.stopped.is_set():
                        try:
                            message = websocket.recv(timeout=0.1 if session.deadline else 1)
                            session.apply(PreviewLease.model_validate_json(message))
                        except TimeoutError:
                            pass
                        frame = session.frame()
                        if frame:
                            websocket.send(frame.model_dump_json())
            except Exception as exc:
                # Neither connection URL, credentials nor external response is logged.
                logger.warning("preview_transport failure_type=%s", type(exc).__name__)
                failures = min(failures + 1, 5)
                self.stopped.wait(min(30, 2**failures) + random.uniform(0, 1))
