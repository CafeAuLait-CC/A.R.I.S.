from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from aris.apps.api.main import create_app
from aris.interfaces.members.public import PUBLIC_GET_PATHS, PublicPortalBoundary
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.web_paths import PUBLIC_PATHS
from tests.test_member_portal_api import portal as portal
from tests.test_member_portal_api import window
from tests.test_member_portal_core import ADMIN_TOKEN
from tests.test_member_portal_core import members as members

ORIGIN = "https://platform.example.test"


@pytest.fixture
def public_portal(portal, settings):
    _, components, _, _ = portal
    components.portal.origin = ORIGIN
    app = create_app(
        settings,
        SlackSettings(_env_file=None, team_id="", signing_secret="", bot_token=""),
        user_commands=components.portal.commands,
        usage_history=components.portal.usage,
        member_portal=components.portal,
        member_paths=PUBLIC_PATHS,
    )
    return PublicPortalBoundary(app, ORIGIN, 53387), components


def login(client):
    response = client.post(
        "/account/login", json={"credential": ADMIN_TOKEN}, headers={"Origin": ORIGIN}
    )
    assert response.status_code == 200, response.text
    return response


def post(client, path, body, key=None, *, accept="application/json"):
    csrf = client.get("/account/session").json()["csrf_token"]
    return client.post(
        path,
        json=body,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Accept": accept,
            **({"Idempotency-Key": key} if key else {}),
        },
    )


def test_public_pages_and_every_asset_use_only_the_public_layout(public_portal):
    app, _ = public_portal
    with TestClient(app, base_url=ORIGIN) as client:
        for path in sorted(PUBLIC_GET_PATHS - {"/account/session"}):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)
            assert "/internal/" not in response.text, path
            assert response.headers["cache-control"] == "no-store"
            assert "Content-Security-Policy" in response.headers
            assert client.head(path).content == b""
        assert 'href="/usage"' in client.get("/").text
        assert 'href="/">Members &amp; profile</a>' in client.get("/usage").text
        assert 'data-cancel-path="cancel"' in client.get("/").text
        assert client.get("/account/session").status_code == 401


def test_public_cookie_csrf_confirmed_save_cancel_and_usage_share_one_session(public_portal):
    app, components = public_portal
    with TestClient(app, base_url=ORIGIN) as client:
        response = login(client)
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Path=/;" in cookie and "Domain=" not in cookie
        actor = client.get("/account/session").json()["actor"]
        payload = {
            "command": "identity.edit_own_profile",
            "parameters": {
                "expected_revision": actor["directory_revision"],
                "profile": {"display_name": "Public Admin", "email": "admin@example.test"},
            },
        }
        assert client.post("/requests", json=payload, headers={"Origin": ORIGIN}).status_code == 403
        response = post(client, "/requests", payload, "public-save")
        assert response.status_code == 202, response.text
        outcome = response.json()["outcome"]
        request_path = "/requests/" + outcome["command_id"]
        assert client.get(request_path + "/review").status_code == 200
        response = post(
            client, request_path + "/confirm", {"proof": outcome["proof"]}, "public-confirm"
        )
        assert response.status_code == 202, response.text
        assert components.worker.run_once().state == "succeeded"
        assert client.get(request_path + "/review").json()["outcome"]["state"] == "succeeded"
        actor = client.get("/account/session").json()["actor"]
        assert actor["display_name"] == "Public Admin" and actor["email"] == "admin@example.test"
        payload["parameters"]["expected_revision"] = actor["directory_revision"]
        payload["parameters"]["profile"]["display_name"] = "Cancelled name"
        outcome = post(client, "/requests", payload, "public-cancel").json()["outcome"]
        path = "/requests/" + outcome["command_id"]
        assert post(client, path + "/cancel", {}, "public-cancel-confirm").status_code == 200
        assert client.get(path + "/review").json()["outcome"]["state"] == "cancelled"
        assert post(client, "/usage/session", None).status_code == 200
        usage = post(
            client, "/usage/query", {"node_id": "node-a", "mode": "mine", "window": window()}
        )
        assert usage.status_code == 200, usage.text
        for accept in ("text/csv", "text/html"):
            selected = {"node_id": "node-a", "mode": "mine", "id": str(uuid4())}
            denied = client.post(
                "/usage/snapshots/read", json=selected, headers={"Origin": ORIGIN, "Accept": accept}
            )
            assert denied.status_code == 403
            missing = post(client, "/usage/snapshots/read", selected, accept=accept)
            assert (
                missing.status_code == 404
                and missing.json()["error"]["message"] == "Window snapshot not found"
            )
        assert post(client, "/account/logout", {}).status_code == 200
        assert client.get("/account/session").status_code == 401


def test_public_boundary_blocks_internal_endpoints_scans_extra_methods_and_bad_ids(public_portal):
    app, _ = public_portal
    cid = str(uuid4())
    with TestClient(app, base_url=ORIGIN) as client:
        for path in (
            "/internal/members/ui",
            "/internal/members/login",
            "/internal/usage/ui",
            "/health/ready",
            "/health/live",
            "/docs",
            "/openapi.json",
            "/redoc",
            "/public/resources",
            "/slack/events",
            "/vega/live",
            "/.env",
            "/assets/unlisted.js",
            "/usage/",
            "/requests/not-a-uuid/review",
            f"/requests/{cid}/withdraw",
            f"/requests/{cid}",
            "/%61ccount/session",
            "/assets%2fmembers.js",
        ):
            assert client.get(path, follow_redirects=False).status_code == 404, path
        for method, path in (
            ("DELETE", "/account/logout"),
            ("GET", "/account/login"),
            ("OPTIONS", "/requests"),
            ("POST", "/"),
        ):
            response = client.request(method, path, headers={"Origin": ORIGIN})
            assert response.status_code == 404, (method, path)
        with pytest.raises(WebSocketDisconnect) as rejected:
            with client.websocket_connect("/vega/live"):
                raise AssertionError("Unexpected public WebSocket")
        assert rejected.value.code == 1008


def test_https_origin_is_exact_and_proxy_headers_are_only_trusted_from_loopback(public_portal):
    app, _ = public_portal
    proxy = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1,::1")
    headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.25"}
    with TestClient(
        proxy, base_url="http://platform.example.test", client=("127.0.0.1", 50000)
    ) as client:
        assert client.get("/", headers=headers).status_code == 200
        assert client.get("/health/ready", headers=headers).status_code == 404
        assert client.get("/", headers={**headers, "Host": "other.example.test"}).status_code == 403
        response = client.post(
            "/account/login",
            json={"credential": ADMIN_TOKEN},
            headers={**headers, "Origin": "https://other.example.test"},
        )
        assert response.status_code == 403
    with TestClient(
        proxy, base_url="http://platform.example.test", client=("203.0.113.26", 50000)
    ) as client:
        assert client.get("/", headers=headers).status_code == 403
    with TestClient(
        proxy, base_url="http://127.0.0.1:53387", client=("127.0.0.1", 50000)
    ) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/").status_code == 403
