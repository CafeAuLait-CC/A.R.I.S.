from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from aris.interfaces.members import live
from aris.interfaces.members.api import COOKIE
from aris.modules.vega.live_schemas import LiveNode, LiveSnapshot
from tests.resource_fixtures import resource_observation
from tests.test_member_portal_api import login, post
from tests.test_member_portal_api import portal as portal
from tests.test_member_portal_core import members as members

HOST = "http://127.0.0.1:53384"


@pytest.mark.parametrize(
    "origin", ("https://example.test:8000", HOST + "/live", "http://localhost")
)
def test_portal_preview_destination_is_fixed_loopback(origin):
    with pytest.raises(ValueError, match="explicit HTTP loopback"):
        live.dashboard_origin(origin)


def test_preview_checks_scope_and_session_and_only_streams_public_node_fields(portal, monkeypatch):
    app, components, _, _ = portal
    surface = components.portal
    surface.live_preview_origin = HOST
    now = datetime.now(UTC)
    resources = resource_observation(observed_at=now)
    nodes = [
        LiveNode(
            node_id=node,
            agent_id="private-agent",
            hostname="private-host",
            connected=True,
            quality="live",
            session_id=uuid4(),
            observed_at=now,
            gpus=[{"observed_state": "idle", **gpu.model_dump()} for gpu in resources.gpu.gpus],
            system_freshness="CURRENT",
            system_observed_at=now,
            system=resources.system,
            compute_freshness="CURRENT",
            compute=resources.compute,
        )
        for node in ("node-a", "node-b")
    ]
    payload = (
        "data: " + LiveSnapshot(generated_at=now, nodes=nodes).model_dump_json() + "\n\n"
    ).encode()
    calls = []
    clock = [0]
    monkeypatch.setattr(live, "monotonic", lambda: clock[0])
    async_client = httpx.AsyncClient

    class RevokedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield payload
            self.logout()
            clock[0] += live.AUTHORIZATION_INTERVAL_SECONDS
            yield payload

    stream = RevokedStream()

    def upstream(request):
        assert str(request.url) == HOST + "/live"
        assert "cookie" not in request.headers and "authorization" not in request.headers
        calls.append(request)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)

    monkeypatch.setattr(
        live.httpx,
        "AsyncClient",
        lambda **kw: async_client(**kw, transport=httpx.MockTransport(upstream)),
    )
    with TestClient(app) as client:
        path = "/internal/members/general"
        data = {"node_ids": ["node-a"]}
        assert (
            client.post(
                path,
                json=data,
                headers={"Accept": live.PREVIEW_MEDIA_TYPE, "Origin": "http://testserver"},
            ).status_code
            == 401
        )
        _, session = login(client)
        assert session["live_preview_available"]
        assert (
            post(
                client, path, {"node_ids": ["outside"]}, accept=live.PREVIEW_MEDIA_TYPE
            ).status_code
            == 403
        )
        assert (
            post(
                client, path, {"node_ids": ["node-a", "node-a"]}, accept=live.PREVIEW_MEDIA_TYPE
            ).status_code
            == 422
        )
        assert calls == []
        token = client.cookies[COOKIE]
        stream.logout = lambda: surface.login.logout(token)
        response = post(client, path, data, accept=live.PREVIEW_MEDIA_TYPE)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(live.PREVIEW_MEDIA_TYPE)
        frames = response.text.splitlines()
        first = live.PreviewFrame.model_validate_json(frames[0])
        assert len(frames) == 2 and frames[1] == '{"error":"PERMISSION_DENIED"}'
        assert [node.node_id for node in first.nodes] == ["node-a"]
        assert first.nodes[0].gpus[0].state == "idle" and first.nodes[0].cpu_percent == 40
        assert all(
            value not in response.text
            for value in ("private-agent", "private-host", "compute", token)
        )
        assert len(calls) == 1
