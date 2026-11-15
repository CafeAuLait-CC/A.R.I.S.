import json
import socket
import struct
import threading
import time
import zlib
from pathlib import Path
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
from aris.interfaces.slack.home import (
    block_cost,
    details_view,
    disk_text,
    home_view,
    node_heading,
    node_summary,
    system_text,
)
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.schemas import ModalHandle
from aris.interfaces.slack.vram_assets import VRAM_ASSET_ROUTE, VRAM_ASSET_SET
from aris.modules.vega.disk_schemas import DiskObservation
from aris.modules.vega.resource_schemas import ComputeOwnership
from aris.modules.vega.service import VegaService
from scripts.generate_slack_vram_assets import asset_set_digest, png_bytes
from tests.test_slack import signed
from tests.test_slack import slack_client as slack_client
from tests.test_slack import slack_settings as slack_settings
from tests.test_telemetry import batch, post


def snapshot(client, settings, *, used=3482, gpu_count=2):
    first = batch()["resources"]["gpu"]["gpus"][0]
    first.update(memory_total_mib=5000, memory_used_mib=used, model="actual test model")
    gpus = [
        {**first, "gpu_uuid": f"GPU-{chr(97 + index)}", "index": index}
        for index in range(gpu_count)
    ]
    assert post(client, batch(gpus=gpus)).status_code == 200
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
    data = snapshot(slack_client, settings, gpu_count=3)
    view = home_view(data, asset_base_url="https://assets.example.test/vram")
    banner = view["blocks"][0]
    title_item, refresh_item = banner["rows"][0]["items"]
    assert title_item["block"]["type"] == "header" and title_item["block"]["level"] == 1
    assert title_item["width"] == "wide" and title_item["horizontal_alignment"] == "start"
    assert refresh_item["block"]["type"] == "section"
    assert refresh_item["horizontal_alignment"] == "end"
    assert refresh_item["block"]["accessory"]["action_id"] == "refresh_home"
    assert [block["type"] for block in banner["fallback"]] == ["header", "section"]
    assert banner["fallback"][1]["accessory"] == refresh_item["block"]["accessory"]
    assert "vertical_alignment" not in json.dumps(banner)
    assert block_cost(banner) == 3
    layouts = [block for block in view["blocks"][1:] if block["type"] == "layout"]
    assert [len(block["rows"][0]["items"]) for block in layouts] == [2, 1]
    layout, odd_layout = layouts
    assert [len(row["items"]) for row in layout["rows"]] == [2]
    panels = [item["block"] for item in layout["rows"][0]["items"]]
    assert all(panel["type"] == "container" and panel["width"] == "narrow" for panel in panels)
    assert panels[0]["title"]["text"] == "GPU 0 · actual test model"
    assert panels[1]["title"]["text"] == "GPU 1 · actual test model"
    assert panels[0]["child_blocks"][0]["type"] == "context"
    assert panels[0]["child_blocks"][0]["elements"][0]["text"] == "In use · Unknown"
    assert panels[0]["child_blocks"][1]["fields"] == [
        {"type": "mrkdwn", "text": "VRAM", "verbatim": True},
        {"type": "mrkdwn", "text": "*70%*", "verbatim": True},
    ]
    assert panels[0]["child_blocks"][2]["image_url"].endswith("vram-070.png")
    assert panels[1]["child_blocks"][2]["image_url"].endswith("vram-070.png")
    assert "GB free" in panels[0]["child_blocks"][3]["elements"][0]["text"]
    assert block_cost(layout) == 13  # Layout + 2 containers + all 10 device blocks.
    odd_item = odd_layout["rows"][0]["items"][0]
    assert odd_item["horizontal_alignment"] == "start"
    assert odd_item["block"]["width"] == "narrow"
    assert odd_item["block"]["title"]["text"] == "GPU 2 · actual test model"
    assert block_cost(odd_layout) == 7
    assert [block["type"] for block in odd_layout["fallback"]] == [
        "section",
        "context",
        "section",
        "image",
        "context",
        "section",
    ]
    assert json.loads(odd_layout["fallback"][-1]["accessory"]["value"]) == {
        "node_id": "node-a",
        "gpu_uuid": "GPU-c",
    }
    footer = panels[0]["child_blocks"][-1]["text"]["text"]
    assert footer.startswith("GPU ") and "\u00a0°C" in footer and "GB" not in footer
    assert sum(block_cost(block) for block in view["blocks"]) <= 100
    assert [block["type"] for block in layout["fallback"]] == [
        "section",
        "context",
        "section",
        "image",
        "context",
        "section",
        "section",
        "context",
        "section",
        "image",
        "context",
        "section",
    ]
    first = json.loads(layout["fallback"][5]["accessory"]["value"])
    second = json.loads(layout["fallback"][11]["accessory"]["value"])
    assert first == {"node_id": "node-a", "gpu_uuid": "GPU-a"}
    assert second == {"node_id": "node-a", "gpu_uuid": "GPU-b"}
    for panel, fallback in zip(
        panels, (layout["fallback"][:6], layout["fallback"][6:]), strict=True
    ):
        assert "actual test model" in fallback[0]["text"]["text"]
        assert panel["child_blocks"] == fallback[1:]
    assert (
        layout["fallback"][5]["accessory"]["action_id"]
        != layout["fallback"][11]["accessory"]["action_id"]
    )
    serialized = json.dumps(view, ensure_ascii=False)
    assert "*0 GPUs available* · 3 in use · 0 reserved" in serialized
    assert "▰" not in serialized and "Disk unknown" in serialized
    assert all(
        text not in serialized
        for text in ("Heartbeat", "Disk sampled", "System ", "Snapshot updated", "Current sample")
    )
    assert serialized.count("Updated ") == 1
    single = home_view(
        data, asset_base_url="https://assets.example.test/vram", layout="single-column"
    )
    assert not any(block["type"] == "layout" for block in single["blocks"])
    assert not any(block["type"] == "container" for block in single["blocks"])
    assert sum(block_cost(block) for block in single["blocks"]) <= 100


def test_summary_uses_observations_without_writing_placeholder_reservations(slack_client, settings):
    data = snapshot(slack_client, settings, gpu_count=3)
    entry = data.nodes[0]
    mixed = entry.model_copy(
        update={
            "node": entry.node.model_copy(
                update={
                    "gpus": [
                        gpu.model_copy(update={"observed_state": state})
                        for gpu, state in zip(
                            entry.node.gpus, ("idle", "busy", "unknown"), strict=True
                        )
                    ]
                }
            )
        }
    )
    assert node_summary(mixed) == "*1 GPU available* · 1 in use · 0 reserved · 1 unknown"
    before = data.model_dump(mode="json")
    queries = []

    def queried(*args):
        queries.append(True)

    event.listen(slack_client.app.state.engine, "before_cursor_execute", queried)
    try:
        view = home_view(data)
    finally:
        event.remove(slack_client.app.state.engine, "before_cursor_execute", queried)
    assert queries == [] and data.model_dump(mode="json") == before
    heading = next(block for block in view["blocks"] if block["type"] == "rich_text")
    assert heading["elements"][0]["elements"] == [
        {"type": "text", "text": "node-a", "style": {"bold": True}},
        {"type": "text", "text": " · Online"},
    ]
    for changes in ({"status": "OFFLINE"}, {"telemetry_status": "STALE"}, {"gpus": []}):
        unknown = entry.model_copy(update={"node": entry.node.model_copy(update=changes)})
        assert "GPU availability unknown" in node_summary(unknown)
        assert "0 GPUs available" not in node_summary(unknown)


@pytest.mark.parametrize(("used", "display"), [(1, "1 MB"), (9, "9 MB")])
def test_home_keeps_small_binary_values_with_mb_labels(slack_client, settings, used, display):
    data = snapshot(slack_client, settings, used=used, gpu_count=1)
    view = home_view(data, asset_base_url="https://assets.example.test/vram")
    memory = view["blocks"][-2]["rows"][0]["items"][0]["block"]["child_blocks"][3]["elements"][0][
        "text"
    ]
    assert memory.startswith(f"{display} / ")


def test_slack_keeps_binary_capacity_values_with_preferred_labels(slack_client, settings):
    data = snapshot(slack_client, settings, gpu_count=1)
    entry = data.nodes[0]
    gpu = entry.node.gpus[0].model_copy(update={"memory_total_mib": 49140, "memory_used_mib": 4096})
    total, used = int(62.6 * 2**40), int(27.9 * 2**40)
    disks = DiskObservation.model_validate(
        {
            "observed_at": entry.node.observed_at,
            "quality": "ok",
            "volumes": [
                {
                    "device_id": "8:1",
                    "source": "/dev/test1",
                    "fs_type": "xfs",
                    "mount_points": ["/mnt/example"],
                    "quality": "ok",
                    "total_bytes": total,
                    "used_bytes": used,
                    "available_bytes": total - used - 2**40,
                }
            ],
        }
    )
    entry = entry.model_copy(
        update={
            "node": entry.node.model_copy(update={"gpus": [gpu]}),
            "system": entry.system.model_copy(
                update={
                    "memory": entry.system.memory.model_copy(
                        update={
                            "total_bytes": 128 * 2**30,
                            "available_bytes": 42 * 2**30,
                        }
                    )
                }
            ),
            "disks": disks,
            "disks_freshness": "CURRENT",
        }
    )
    before = entry.model_dump(mode="json")
    assert disk_text(entry) == "Disk 27.9 TB used / 62.6 TB"
    assert "RAM 86.0 / 128.0 GB" in system_text(entry)
    view = home_view(
        data.model_copy(update={"nodes": [entry]}),
        asset_base_url="https://assets.example.test/vram",
    )
    serialized = json.dumps(view, ensure_ascii=False)
    assert "4.0 / 48.0 GB · 44.0 GB free" in serialized
    modal = details_view(entry, gpu)
    assert modal["blocks"][1]["fields"][0]["text"] == "*VRAM*\n4.0 / 48.0 GB"
    assert modal["blocks"][1]["fields"][1]["text"] == "*Memory occupancy*\n8%"
    assert all(unit not in serialized + json.dumps(modal) for unit in ("MiB", "GiB", "TiB"))
    assert entry.model_dump(mode="json") == before
    partial = entry.model_copy(update={"disks": disks.model_copy(update={"quality": "partial"})})
    assert disk_text(partial) == "Disk partially unknown"
    assert disk_text(entry.model_copy(update={"disks_freshness": "STALE"})) == "Disk unknown"
    small = entry.model_copy(
        update={
            "disks": disks.model_copy(
                update={
                    "volumes": [
                        disks.volumes[0].model_copy(
                            update={
                                "total_bytes": 900 * 2**30,
                                "used_bytes": 400 * 2**30,
                                "available_bytes": 400 * 2**30,
                            }
                        )
                    ]
                }
            )
        }
    )
    assert disk_text(small) == "Disk 400.0 GB used / 900.0 GB"


def test_node_weight_uses_single_line_literal_text_without_mentions_or_mrkdwn():
    heading = node_heading("host<@U999> *literal*\nnext\u2028line", "Online")
    node, status = heading["elements"][0]["elements"]
    assert node == {
        "type": "text",
        "text": "host<@U999> *literal* next line",
        "style": {"bold": True},
    }
    assert status == {"type": "text", "text": " · Online"}
    assert all(part["type"] == "text" for part in heading["elements"][0]["elements"])


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
    assert "VRAM 70%" not in serialized and "Current metrics unknown" in serialized
    assert "GPU availability unknown" in serialized and "0 GPUs available" not in serialized


def test_details_matches_six_field_reference_and_keeps_observed_owners(slack_client, settings):
    data = snapshot(slack_client, settings)
    entry, gpu = data.nodes[0], data.nodes[0].node.gpus[0]
    ownership = ComputeOwnership.model_validate(
        {
            "quality": "ok",
            "unresolved_processes": 0,
            "users": [
                {
                    "uid": 1001,
                    "username": "observed-linux-user",
                    "reported_processes": 1,
                    "gpus": [{"gpu_uuid": gpu.gpu_uuid, "reported_processes": 1}],
                }
            ],
        }
    )
    entry = entry.model_copy(
        update={"compute": entry.compute.model_copy(update={"ownership": ownership})}
    )
    gpu = gpu.model_copy(update={"model": "NVIDIA RTX A6000"})
    view = details_view(entry, gpu)
    assert view["title"]["text"] == "RTX A6000 · GPU 0"
    assert view["close"]["text"] == "Close"
    assert [
        [field["text"].split("\n")[0] for field in block["fields"]] for block in view["blocks"]
    ] == [
        ["*Node*", "*Status*"],
        ["*VRAM*", "*Memory occupancy*"],
        ["*GPU utilization*", "*Temperature*"],
    ]
    assert "In use · Linux observed-linux-user" in view["blocks"][0]["fields"][1]["text"]
    assert view["blocks"][1]["fields"][1]["text"] == "*Memory occupancy*\n70%"
    serialized = json.dumps(view, ensure_ascii=False)
    assert all(
        text not in serialized
        for text in ("Heartbeat", "GPU sampled", "UUID", "*CPU*", "*RAM*", "Disk", "Reservation")
    )
    long_gpu = gpu.model_copy(
        update={"model": "NVIDIA " + "Very long actual GPU model name " * 3, "index": 12}
    )
    title = details_view(entry, long_gpu)["title"]["text"]
    assert len(title) <= 24 and title.endswith(" · GPU 12") and "…" in title
    offline = entry.model_copy(update={"node": entry.node.model_copy(update={"status": "OFFLINE"})})
    unknown = details_view(offline, gpu)
    assert unknown["blocks"][0]["fields"][1]["text"] == "*Status*\nUnknown"
    assert unknown["blocks"][1]["fields"][1]["text"] == "*Memory occupancy*\nUnknown"
    assert "GiB" not in json.dumps(unknown)


def test_data_free_assets_are_public_bounded_and_do_not_query_database(client):
    queries = []
    event.listen(
        client.app.state.engine, "before_cursor_execute", lambda *args: queries.append(True)
    )
    try:
        response = client.get(VRAM_ASSET_ROUTE + "/vram-070.png")
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert struct.unpack(">II", response.content[16:24]) == (256, 10)
        assert "immutable" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get(VRAM_ASSET_ROUTE + "/vram-unknown.png").status_code == 200
        assert client.get("/slack/assets/vram/5cdcfb515c70/vram-070.png").status_code == 404
        for filename in ("vram-101.png", "manifest.json", "api.env", "..%2Fapi.env"):
            assert client.get(VRAM_ASSET_ROUTE + "/" + filename).status_code == 404
        assert queries == []
    finally:
        # The listener belongs only to this disposable engine.
        client.app.state.engine.dispose()


def test_native_sized_pngs_preserve_visible_percentage_and_content_version():
    directory = Path(__file__).resolve().parents[1] / "src/aris/interfaces/slack/assets/vram"
    assets = {path.name: path.read_bytes() for path in directory.glob("*.png")}
    assert len(assets) == 102
    assert asset_set_digest(assets) == VRAM_ASSET_SET != "5cdcfb515c70"
    for data in assets.values():
        assert struct.unpack(">II", data[16:24]) == (256, 10)
    for percent in (None, 0, 62, 70, 90, 100):
        name = "vram-unknown.png" if percent is None else f"vram-{percent:03d}.png"
        data = assets[name]
        assert png_bytes(percent) == data
        offset, compressed = 8, bytearray()
        kinds = []
        while offset < len(data):
            size = struct.unpack(">I", data[offset : offset + 4])[0]
            kind, payload = data[offset + 4 : offset + 8], data[offset + 8 : offset + 8 + size]
            assert struct.unpack(">I", data[offset + 8 + size : offset + 12 + size])[0] == (
                zlib.crc32(kind + payload)
            )
            kinds.append(kind)
            if kind == b"IDAT":
                compressed.extend(payload)
            offset += size + 12
        assert kinds == [b"IHDR", b"IDAT", b"IEND"]  # No embedded identities or metadata.
        rows = zlib.decompress(compressed)
        assert len(rows) == 10 * (1 + 256 * 4)
        middle = rows[5 * (1 + 256 * 4) + 1 : 6 * (1 + 256 * 4)]
        opaque_pixels = sum(middle[x * 4 + 3] == 255 for x in range(256))
        assert abs(opaque_pixels - 256 * (percent or 0) / 100) <= 1
        if percent == 62:
            assert middle[255 * 4 + 3] == 56  # The gray tail remains inside the actual image.


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
        assert "Memory occupancy" in json.dumps(view, ensure_ascii=False)
        assert "GPU UUID" not in json.dumps(view, ensure_ascii=False)
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
