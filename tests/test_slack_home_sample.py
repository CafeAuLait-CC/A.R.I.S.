import json
import socket
import struct
import threading
import time
from urllib.parse import urlencode

import httpx
import pytest
import uvicorn
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from aris.core.audit import AuditEntry
from aris.core.db import session_factory
from aris.interfaces.slack.client import SlackClient, SlackDeliveryError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.home import block_cost, home_view
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.schemas import ModalHandle
from aris.interfaces.slack.vram_assets import VRAM_ASSET_ROUTE
from aris.modules.vega.service import VegaService
from tests.test_slack import signed
from tests.test_slack import slack_client as slack_client
from tests.test_slack import slack_settings as slack_settings
from tests.test_telemetry import batch, post


def snapshot(client, settings, *, used=3482):
    first = batch()["resources"]["gpu"]["gpus"][0]
    first.update(memory_total_mib=5000, memory_used_mib=used, model="actual test model")
    second = {**first, "gpu_uuid": "GPU-b", "index": 1}
    assert post(client, batch(gpus=[first, second])).status_code == 200
    return VegaService(session_factory(client.app.state.engine), settings).resource_status()


def details_body(**changes):
    payload = {
        "type": "block_actions",
        "team": {"id": "T111"},
        "user": {"id": "U111"},
        "trigger_id": "test-short-lived-trigger-only",
        "response_url": "https://must-not-be-called.example/private",
        "actions": [
            {
                "action_id": "gpu_details:test",
                "value": json.dumps({"node_id": "node-a", "gpu_uuid": "GPU-a"}),
            }
        ],
        **changes,
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def test_native_pairs_use_matching_quantized_vram_and_complete_fallback(slack_client, settings):
    data = snapshot(slack_client, settings)
    view = home_view(data, asset_base_url="https://assets.example.test/vram")
    layout = next(block for block in view["blocks"] if block["type"] == "layout")
    assert [len(row["items"]) for row in layout["rows"]] == [2, 2, 2]
    title = layout["rows"][0]["items"][0]["block"]["text"]["text"]
    assert "VRAM 70%" in title and "actual test model" in title
    assert layout["rows"][1]["items"][0]["block"]["image_url"].endswith("vram-070.png")
    assert [block["type"] for block in layout["fallback"]] == [
        "section",
        "image",
        "section",
        "section",
        "image",
        "section",
    ]
    first = json.loads(layout["fallback"][2]["accessory"]["value"])
    second = json.loads(layout["fallback"][5]["accessory"]["value"])
    assert first == {"node_id": "node-a", "gpu_uuid": "GPU-a"}
    assert second == {"node_id": "node-a", "gpu_uuid": "GPU-b"}
    assert (
        layout["fallback"][2]["accessory"]["action_id"]
        != layout["fallback"][5]["accessory"]["action_id"]
    )
    serialized = json.dumps(view, ensure_ascii=False)
    assert "available" not in serialized and "Reserved" not in serialized
    assert "▰" not in serialized and "磁盘 —" in serialized
    single = home_view(
        data, asset_base_url="https://assets.example.test/vram", layout="single-column"
    )
    assert not any(block["type"] == "layout" for block in single["blocks"])
    assert sum(block_cost(block) for block in single["blocks"]) <= 100


def test_stale_pair_uses_unknown_asset_and_does_not_show_old_numbers(slack_client, settings):
    data = snapshot(slack_client, settings)
    stale = data.nodes[0].model_copy(
        update={"node": data.nodes[0].node.model_copy(update={"status": "OFFLINE"})}
    )
    view = home_view(
        data.model_copy(update={"nodes": [stale]}),
        asset_base_url="https://assets.example.test/vram",
    )
    serialized = json.dumps(view, ensure_ascii=False)
    assert "vram-unknown.png" in serialized
    assert "vram-000.png" not in serialized and "GiB" not in serialized
    assert "VRAM 70%" not in serialized and "当前指标不可用" in serialized


def test_data_free_assets_are_public_bounded_and_do_not_query_database(client):
    queries = []
    event.listen(
        client.app.state.engine, "before_cursor_execute", lambda *args: queries.append(True)
    )
    try:
        response = client.get(VRAM_ASSET_ROUTE + "/vram-070.png")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert struct.unpack(">II", response.content[16:24]) == (640, 20)
        assert "immutable" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(VRAM_ASSET_ROUTE + "/vram-unknown.png").status_code == 200
        for filename in ("vram-101.png", "manifest.json", "api.env", "..%2Fapi.env"):
            assert client.get(VRAM_ASSET_ROUTE + "/" + filename).status_code == 404
        assert queries == []
    finally:
        # The listener belongs only to this disposable engine.
        client.app.state.engine.dispose()


def test_asset_configuration_rejects_credential_or_non_http_urls():
    for value in (
        "file:///tmp/image",
        "data:image/png;base64,AAA",
        "https://u:private@host/path",
        "https://host/path?token=private",
        "https://host/path#fragment",
    ):
        with pytest.raises(ValidationError) as rejected:
            SlackSettings(_env_file=None, home_asset_base_url=value)
        assert "u:private" not in str(rejected.value)
    assert (
        SlackSettings(_env_file=None, home_asset_base_url="https://host/path/").home_asset_base_url
        == "https://host/path"
    )


def test_details_ack_is_sent_before_modal_network_or_snapshot_query_and_is_deduplicated(
    slack_client,
    monkeypatch,
):
    assert post(slack_client, batch()).status_code == 200
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    calls = []
    original_status = VegaService.resource_status

    def opened(self, trigger, view):
        calls.append("open")
        assert trigger == "test-short-lived-trigger-only" and view["type"] == "modal"
        entered.set()
        assert release.wait(3)
        return ModalHandle("V-test", "hash-test")

    def status(self):
        assert calls == ["open"]
        calls.append("snapshot")
        return original_status(self)

    def updated(self, handle, view):
        calls.append("update")
        assert handle.view_id == "V-test"
        assert "GPU UUID" in json.dumps(view, ensure_ascii=False)
        completed.set()

    monkeypatch.setattr(SlackClient, "open_modal", opened)
    monkeypatch.setattr(SlackClient, "update_modal", updated)
    monkeypatch.setattr(VegaService, "resource_status", status)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(slack_client.app, log_level="error", ws="none"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    body = details_body()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline
            time.sleep(0.01)
        with httpx.Client(timeout=1, trust_env=False) as http:
            response = http.post(
                f"http://127.0.0.1:{listener.getsockname()[1]}/slack/events",
                content=body,
                headers=signed(body, "application/x-www-form-urlencoded"),
            )
            assert response.status_code == 200
            assert entered.wait(1) and not completed.is_set()
            # The real HTTP client already has its ACK while the modal call is blocked.
            release.set()
            assert completed.wait(2)
            assert (
                http.post(
                    f"http://127.0.0.1:{listener.getsockname()[1]}/slack/events",
                    content=body,
                    headers=signed(body, "application/x-www-form-urlencoded"),
                ).status_code
                == 200
            )
    finally:
        release.set()
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
    assert not thread.is_alive()
    assert calls == ["open", "snapshot", "update"]
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 0
        receipt = session.scalars(select(SlackReceipt)).one()
        assert "trigger" not in repr(receipt.__dict__)
        audit = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "slack.details.opened")
        ).one()
        assert audit.changes == {"error_code": ""}


def test_details_failure_is_recorded_without_faking_a_success_or_using_response_url(
    slack_client,
    monkeypatch,
):
    def expired(self, trigger, view):
        raise SlackDeliveryError("expired_trigger_id", retryable=False)

    monkeypatch.setattr(SlackClient, "open_modal", expired)
    body = details_body()
    response = slack_client.post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )
    assert response.status_code == 200
    with Session(slack_client.app.state.engine) as session:
        audit = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "slack.details.failed")
        ).one()
        assert audit.changes == {"error_code": "expired_trigger_id"}
        assert "test-short-lived-trigger-only" not in repr(audit.__dict__)
        assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 0


def test_modal_client_consumes_trigger_only_for_fixed_open_then_updates_by_view_id(slack_settings):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("views.open"):
            assert request.extensions["timeout"]["connect"] == 0.5
            return httpx.Response(200, json={"ok": True, "view": {"id": "V123", "hash": "h1"}})
        return httpx.Response(200, json={"ok": True})

    publisher = SlackClient(slack_settings, httpx.MockTransport(respond))
    handle = publisher.open_modal("ephemeral-trigger", {"type": "modal", "blocks": []})
    publisher.update_modal(handle, {"type": "modal", "blocks": []})
    assert [str(request.url) for request in requests] == [
        "https://slack.com/api/views.open",
        "https://slack.com/api/views.update",
    ]
    assert json.loads(requests[0].content)["trigger_id"] == "ephemeral-trigger"
    assert "trigger_id" not in json.loads(requests[1].content)
    assert json.loads(requests[1].content)["view_id"] == "V123"
