import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from aris.apps.dashboard.main import create_dashboard, preview_proxy, resource_events
from aris.core.db import session_factory
from aris.modules.vega.service import VegaService
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_telemetry import batch, post


def local_client(settings, **kwargs):
    return TestClient(
        create_dashboard(settings),
        base_url="http://127.0.0.1",
        client=("127.0.0.1", 50000),
        **kwargs,
    )


def test_dashboard_is_local_and_has_no_browser_credentials(settings):
    with local_client(settings) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "资源观测" in response.text
        assert READ_TOKEN not in response.text
        assert AGENT_TOKEN not in response.text
        script = client.get("/assets/app.js")
        assert script.status_code == 200
        assert READ_TOKEN not in script.text
        assert AGENT_TOKEN not in script.text


def test_dashboard_rejects_remote_client_host_rebinding_and_foreign_origin(settings):
    with local_client(settings) as client:
        assert client.get("/", headers={"host": "evil.example"}).status_code == 400
        assert client.get("/events", headers={"origin": "https://evil.example"}).status_code == 403
        assert client.get("/events", headers={"origin": "http://[invalid"}).status_code == 403
    with TestClient(
        create_dashboard(settings), base_url="http://localhost", client=("203.0.113.1", 50000)
    ) as client:
        assert client.get("/").status_code == 403


def test_dashboard_display_zone_is_explicit_and_validated(settings):
    app = create_dashboard(settings, interval=5, time_zone="Europe/London")
    with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        text = client.get("/").text
        assert 'data-time-zone="Europe/London"' in text
        assert 'data-refresh-seconds="5"' in text
    with pytest.raises(ValueError, match="time zone"):
        create_dashboard(settings, time_zone="not-a-time-zone")


def test_preview_proxy_keeps_read_credentials_server_side_and_closes_upstream(
    settings, monkeypatch
):
    import httpx

    requests = []
    real_client = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200, text='event: preview\ndata: {"generated_at":"2026-01-01T00:00:00Z","nodes":[]}\n\n'
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(respond)),
    )

    async def connected():
        return False

    async def consume():
        stream = preview_proxy(settings, "http://127.0.0.1:8000", connected)
        await anext(stream)
        line = await anext(stream)
        assert "event: preview" in line
        assert READ_TOKEN not in line
        await stream.aclose()

    asyncio.run(consume())
    assert requests[0].headers["Authorization"] == "Bearer " + READ_TOKEN
    assert requests[0].url.path == "/internal/v1/live"
    for url in ["http://remote.example", "https://user:password@host"]:
        with pytest.raises(ValueError, match="preview API"):
            create_dashboard(settings, api_url=url)


def test_preview_failure_is_unknown_and_does_not_leak_upstream_body(settings, monkeypatch):
    import httpx

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(lambda _: httpx.Response(403, text="private token")),
        ),
    )

    async def connected():
        return False

    async def consume():
        stream = preview_proxy(settings, "http://127.0.0.1:8000", connected)
        await anext(stream)
        failure = await anext(stream)
        assert "preview_error" in failure
        assert "private" not in failure
        await stream.aclose()

    asyncio.run(consume())


def test_stream_preserves_fact_freshness_and_never_exposes_tokens(client, settings):
    assert post(client, batch(seconds_ago=90)).status_code == 200
    service = VegaService(session_factory(client.app.state.engine), settings)

    async def connected():
        return False

    async def consume():
        stream = resource_events(service, connected)
        assert await anext(stream) == "retry: 3000\n\n"
        frame = await anext(stream)
        assert frame.startswith("event: resources\ndata: ")
        snapshot = json.loads(frame.split("data: ", 1)[1])
        node = snapshot["nodes"][0]
        assert node["status"] == "STALE"
        assert node["gpus"][0]["observed_state"] == "unknown"
        assert READ_TOKEN not in frame and AGENT_TOKEN not in frame
        await stream.aclose()

    asyncio.run(consume())


def test_stream_reports_dependency_failure_without_leaking_exception():
    class BrokenService:
        def status(self):
            raise RuntimeError("password=private-database-password")

    async def connected():
        return False

    async def consume():
        stream = resource_events(BrokenService(), connected)
        await anext(stream)
        frame = await anext(stream)
        assert "event: dependency_error" in frame
        assert "DEPENDENCY_UNAVAILABLE" in frame
        assert "private-database-password" not in frame
        await stream.aclose()

    asyncio.run(consume())


def test_stream_stops_when_browser_disconnects(settings):
    async def disconnected():
        return True

    async def consume():
        stream = resource_events(None, disconnected)
        await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(consume())
