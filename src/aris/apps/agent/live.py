import logging
import random
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from websockets.exceptions import InvalidStatus
from websockets.sync.client import ClientConnection, connect

from aris.apps.agent.config import AgentSettings
from aris.apps.agent.resource_sampler import ResourceSampler
from aris.apps.agent.sampler import Sampler
from aris.modules.vega.live_schemas import (
    LiveFrame,
    LiveHello,
    PreviewLease,
    ResourceLiveFrame,
    ResourceLiveHello,
)

logger = logging.getLogger(__name__)


class PreviewSession:
    """A bounded read-only lease; there is no shell or generic command dispatch."""

    def __init__(
        self,
        sampler: Sampler | ResourceSampler,
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
        self.last_observed_at: datetime | None = None

    def apply(self, lease: PreviewLease) -> None:
        self.deadline = self.clock() + lease.lease_seconds if lease.lease_seconds else 0
        self.interval = lease.interval_seconds

    def frame(self) -> LiveFrame | ResourceLiveFrame | None:
        now = self.clock()
        if now >= self.deadline or now < self.next_sample:
            return None
        self.next_sample = now + self.interval
        self.seq += 1
        frame: LiveFrame | ResourceLiveFrame
        if isinstance(self.sampler, ResourceSampler):
            resources = self.sampler.sample(fresh_after=self.last_observed_at)
            frame = ResourceLiveFrame(session_id=self.session_id, seq=self.seq, resources=resources)
        else:
            frame = self.gpu_frame()
        # A slow driver call may have outlived its lease. Never renew it ourselves.
        if self.clock() >= self.deadline:
            return None
        self.last_observed_at = frame.observed_at
        return frame

    def gpu_frame(self) -> LiveFrame:
        assert isinstance(self.sampler, Sampler)
        try:
            observation = self.sampler.sample(fresh_after=self.last_observed_at)
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
        return frame


def live_url(settings: AgentSettings) -> str:
    base = urlsplit(settings.api_url)
    return urlunsplit(
        (
            "wss" if base.scheme == "https" else "ws",
            base.netloc,
            base.path.rstrip("/")
            + ("/vega/v2/live" if settings.resource_enabled else "/vega/v1/live"),
            urlencode({"agent_id": settings.id, "node_id": settings.node_id}),
            "",
        )
    )


@contextmanager
def outbound_connection(settings: AgentSettings) -> Iterator[ClientConnection]:
    url = live_url(settings)
    target = urlsplit(url)
    assert target.hostname is not None
    # A preconnected socket forbids redirects in websockets. Custom node headers
    # must never be forwarded to a redirect destination, even on the same host.
    with socket.create_connection(
        (target.hostname, target.port or (443 if target.scheme == "wss" else 80)), timeout=10
    ) as transport:
        with connect(
            url,
            sock=transport,
            additional_headers={"X-Agent-Token": settings.token.get_secret_value()},
            proxy=None,
            open_timeout=10,
            close_timeout=2,
            ping_interval=20,
            ping_timeout=20,
            max_size=2048,
            max_queue=2,
            compression=None,
        ) as websocket:
            yield websocket


class LiveClient:
    def __init__(self, settings: AgentSettings, sampler: Sampler | ResourceSampler) -> None:
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
                with outbound_connection(self.settings) as websocket:
                    hello_type = (
                        ResourceLiveHello
                        if isinstance(self.sampler, ResourceSampler)
                        else LiveHello
                    )
                    hello = hello_type(
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
                logger.warning(
                    "preview_transport failure_type=%s http_status=%s",
                    type(exc).__name__,
                    exc.response.status_code if isinstance(exc, InvalidStatus) else None,
                )
                failures = min(failures + 1, 5)
                self.stopped.wait(min(30, 2**failures) + random.uniform(0, 1))
