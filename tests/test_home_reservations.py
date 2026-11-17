import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, select, text, update

from aris.apps.api.main import create_app
from aris.apps.composition.m2 import build_m2_components
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import (
    ExternalIdentity,
    Permission,
    Role,
    RoleBinding,
    RolePermission,
    User,
)
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject
from aris.core.contracts import DomainError
from aris.interfaces.slack.client import SlackClient
from aris.interfaces.slack.home import block_cost, details_view, home_view, node_summary, state_text
from aris.interfaces.slack.home_reservations import (
    HomeReservationSnapshot,
    HomeReservationSource,
)
from aris.interfaces.slack.models import SlackHomeJob
from aris.interfaces.slack.schemas import ModalHandle
from aris.modules.nova.models import Reservation
from aris.modules.nova.occupancy import ReservationOccupancyService
from aris.modules.vega.service import VegaService
from tests.test_availability import reservation
from tests.test_gpu_target_facts import NOW
from tests.test_m2_composition import config
from tests.test_reservation_service import booking as booking
from tests.test_slack import home_event, send_event, signed
from tests.test_slack import slack_settings as slack_settings
from tests.test_user_commands import LIMITS

NS = IdentityNamespace(provider="slack", namespace="T111")
SUBJECT = ExternalSubject(provider="slack", namespace="T111", external_id="U111")


def resources(state, settings, *, hardware="idle", online=True):
    snapshot = VegaService(state[2], settings).resource_status()
    entry = next(e for e in snapshot.nodes if e.node.node_id == "node-a")
    gpus = [
        g.model_copy(
            update={
                "observed_state": hardware,
                "memory_used_mib": 0 if hardware == "idle" else 30000,
                "utilization_percent": 0 if hardware == "idle" else 99,
            }
        )
        for g in entry.node.gpus
    ]
    node = entry.node.model_copy(
        update={
            "gpus": gpus,
            "status": "ONLINE" if online else "OFFLINE",
            "telemetry_status": "CURRENT",
        }
    )
    return snapshot.model_copy(update={"nodes": [entry.model_copy(update={"node": node})]})


@pytest.fixture
def home_state(booking, settings):
    service, _, sessions, ids, clock, _ = booking
    reader, permission = str(uuid4()), str(uuid4())
    with sessions.begin() as session:
        session.add_all(
            [
                Role(id=reader, name="fixture-reservation-observer"),
                Permission(id=permission, name="reservation.view_current"),
                ExternalIdentity(id=str(uuid4()), user_id=ids["alice"], **SUBJECT.model_dump()),
            ]
        )
        session.flush()
        session.add(RolePermission(role_id=reader, permission_id=permission))
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=ids["alice"],
                role_id=reader,
                scope_type="node",
                scope_id="node-a",
            )
        )
        session.add(reservation(ids["bob"], start=NOW))
    source = HomeReservationSource(
        sessions,
        NS,
        service.availability.targets,
        ReservationOccupancyService(sessions, clock=lambda: clock["now"]),
        LIMITS,
        lambda: clock["now"],
    )
    return source, resources(booking, settings), booking, reader


def test_current_aggregate_is_authorized_separately_and_has_no_owner_or_purpose(home_state):
    source, snapshot, state, _ = home_state
    result = source.snapshot(SUBJECT, snapshot)
    assert result.state_for("node-a", "GPU-target") == "occupied"
    assert (
        state[3]["bob"] not in result.model_dump_json()
        and "Private purpose" not in result.model_dump_json()
    )
    assert "reservation_id" not in result.model_dump_json()
    unknown = source.snapshot(SUBJECT.model_copy(update={"external_id": "U_UNKNOWN"}), snapshot)
    assert unknown.state_for("node-a", "GPU-target") == "unknown"
    assert source.snapshot(SUBJECT.model_copy(update={"namespace": "T_OTHER"}), snapshot).gpus == ()


@pytest.mark.parametrize("change", ["revoke", "other_node", "suspend", "remap"])
def test_current_mapping_active_and_exact_read_grants_are_rechecked(home_state, change):
    source, snapshot, state, reader = home_state
    assert source.snapshot(SUBJECT, snapshot).gpus
    with state[2].begin() as session:
        if change == "revoke":
            session.execute(delete(RoleBinding).where(RoleBinding.role_id == reader))
        elif change == "other_node":
            session.execute(
                update(RoleBinding).where(RoleBinding.role_id == reader).values(scope_id="node-b")
            )
        elif change == "suspend":
            session.execute(
                update(User).where(User.id == state[3]["alice"]).values(status="suspended")
            )
        else:
            session.execute(update(ExternalIdentity).values(user_id=state[3]["bob"]))
    # Existing creator/mine grants never stand in for reservation.view_current.
    assert source.snapshot(SUBJECT, snapshot).state_for("node-a", "GPU-target") == "unknown"


@pytest.mark.parametrize("change", ["permission", "identity", "node"])
def test_projection_does_not_publish_under_context_that_changed_during_query(
    home_state, monkeypatch, change
):
    source, snapshot, state, reader = home_state
    original = source.reservations.current_in_transaction

    def changed(session, query):
        result = original(session, query)
        if change == "node":
            original_labels = source.targets.labels_in_transaction
            monkeypatch.setattr(
                source.targets,
                "labels_in_transaction",
                lambda s, q: tuple(
                    label.model_copy(update={"node_id": "node-b"})
                    for label in original_labels(s, q)
                ),
            )
        else:
            with state[2].begin() as other:
                if change == "permission":
                    other.execute(delete(RoleBinding).where(RoleBinding.role_id == reader))
                else:
                    other.execute(update(ExternalIdentity).values(user_id=state[3]["bob"]))
        return result

    monkeypatch.setattr(source.reservations, "current_in_transaction", changed)
    assert source.snapshot(SUBJECT, snapshot).state_for("node-a", "GPU-target") == "unknown"


def test_rendering_keeps_busy_and_reserved_independent_and_uses_unknown_without_authority(
    home_state, settings
):
    source, snapshot, state, _ = home_state
    result = source.snapshot(SUBJECT, snapshot)
    entry = snapshot.nodes[0]
    gpu = entry.node.gpus[0]
    assert state_text(entry, gpu, result) == "Reserved"
    assert node_summary(entry, result) == "*0 GPUs available* · 0 in use · 1 reserved"
    busy = resources(state, settings, hardware="busy")
    assert node_summary(busy.nodes[0], result) == "*0 GPUs available* · 1 in use · 1 reserved"
    view = home_view(busy, reservations=result, asset_base_url="https://assets.example.test/vram")
    assert "Reserved counts are independent" in json.dumps(view)
    assert "In use" in json.dumps(view) and "Reserved" in json.dumps(view)
    unknown = HomeReservationSnapshot(as_of=NOW)
    assert "Reservations unknown" in node_summary(entry, unknown)
    assert "0 reserved" not in node_summary(
        entry, unknown
    ) and "1 GPU available" not in node_summary(entry, unknown)
    assert state_text(entry, gpu, unknown) == "Reservation unknown"
    offline = resources(state, settings, online=False).nodes[0]
    assert state_text(offline, offline.node.gpus[0], result) == "Unknown · Reserved"
    assert "1 GPU available" not in node_summary(offline, result)
    assert "GPU availability unknown" in node_summary(offline, result)


def test_details_and_native_fallback_share_status_without_changing_six_fields_or_progress_images(
    home_state,
):
    source, snapshot, _, _ = home_state
    result = source.snapshot(SUBJECT, snapshot)
    entry = snapshot.nodes[0]
    gpu = entry.node.gpus[0]
    details = details_view(entry, gpu, result)
    assert len(details["blocks"]) == 3 and sum(len(b["fields"]) for b in details["blocks"]) == 6
    assert "Reserved" in json.dumps(details) and "GPU-target" not in json.dumps(details)
    old = home_view(snapshot, asset_base_url="https://assets.example.test/vram")
    new = home_view(
        snapshot, asset_base_url="https://assets.example.test/vram", reservations=result
    )
    panels = [b for b in new["blocks"] if b["type"] == "layout"][1:]
    panel = panels[0]
    assert panel["rows"][0]["items"][0]["block"]["child_blocks"] == panel["fallback"][1:]

    def image_urls(value):
        if isinstance(value, dict):
            return ([value["image_url"]] if value.get("type") == "image" else []) + [
                result for item in value.values() for result in image_urls(item)
            ]
        return (
            [result for item in value for result in image_urls(item)]
            if isinstance(value, list)
            else []
        )

    assert image_urls(old) == image_urls(new)
    assert sum(block_cost(b) for b in new["blocks"]) <= 100


def test_empty_inventory_does_not_report_zero_known_reservations(home_state):
    _, snapshot, _, _ = home_state
    entry = snapshot.nodes[0]
    entry = entry.model_copy(update={"node": entry.node.model_copy(update={"gpus": []})})
    assert "Reservations unknown" in node_summary(entry, HomeReservationSnapshot(as_of=NOW))


def test_home_query_works_with_read_only_pg_and_does_not_write_or_flush(home_state):
    source, snapshot, state, _ = home_state
    engine = state[5]

    def readonly(connection):
        if engine.dialect.name == "postgresql":
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(engine, "begin", readonly)
    try:
        assert source.snapshot(SUBJECT, snapshot).state_for("node-a", "GPU-target") == "occupied"
    finally:
        event.remove(engine, "begin", readonly)


def test_home_query_timeout_returns_unknown_instead_of_zero(home_state, monkeypatch):
    source, snapshot, state, _ = home_state
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL projection budget")

    def slow(session, query):
        session.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("query budget did not apply")

    monkeypatch.setattr(source.targets, "labels_in_transaction", slow)
    result = source.snapshot(SUBJECT, snapshot)
    assert not result.gpus and "0 reserved" not in node_summary(snapshot.nodes[0], result)


def test_m2_home_dispatch_is_explicit_and_network_io_has_no_open_reservation_locks(
    home_state, settings, slack_settings, monkeypatch
):
    _, snapshot, state, _ = home_state
    now = datetime.now(UTC)
    with state[2].begin() as session:
        session.execute(
            update(Reservation).values(
                start_at=now - timedelta(seconds=1), end_at=now + timedelta(hours=1)
            )
        )

    class Publisher:
        def __init__(self):
            self.calls = []

        def publish_home(self, user, view):
            with state[2].begin() as other:
                other.execute(select(Reservation).with_for_update(nowait=True)).all()
            self.calls.append((user, view))

    publisher = Publisher()
    cfg = config(namespace=NS, reservation_home=True)
    graph = build_m2_components(state[2], settings, slack_settings, cfg, home_publisher=publisher)
    monkeypatch.setattr(graph.home.vega, "resource_status", lambda: snapshot)
    with TestClient(create_app(settings, slack_settings, m2_config=cfg)) as client:
        assert send_event(client, home_event()).status_code == 200
    assert graph.run_once("home")["home"]["outcome"] == "delivered"
    assert "1 reserved" in json.dumps(publisher.calls[0][1])
    with state[2]() as session:
        assert session.scalar(select(SlackHomeJob.state)) == "completed"
    assert graph.run_once("home")["home"]["outcome"] == "idle"
    disabled = build_m2_components(state[2], settings, slack_settings, config(namespace=NS))
    with pytest.raises(ValueError, match="disabled"):
        disabled.run_once("home")


def test_signed_details_uses_the_same_current_user_projection(
    home_state, settings, slack_settings, monkeypatch
):
    source, snapshot, state, _ = home_state
    views = []
    monkeypatch.setattr(VegaService, "resource_status", lambda self: snapshot)
    monkeypatch.setattr(SlackClient, "open_modal", lambda *args: ModalHandle("V111", "hash"))
    monkeypatch.setattr(SlackClient, "update_modal", lambda self, handle, view: views.append(view))
    with TestClient(create_app(settings, slack_settings, home_reservations=source)) as client:
        payload = {
            "type": "block_actions",
            "team": {"id": "T111"},
            "user": {"id": "U111"},
            "trigger_id": "fixture-trigger",
            "actions": [
                {
                    "action_id": "gpu_details:test",
                    "value": json.dumps({"node_id": "node-a", "gpu_uuid": "GPU-target"}),
                }
            ],
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        assert (
            client.post(
                "/slack/events",
                content=body,
                headers=signed(body, "application/x-www-form-urlencoded"),
            ).status_code
            == 200
        )
    assert "*Status*\nReserved" in json.dumps(views[-1]).replace("\\n", "\n")
    assert "Private purpose" not in json.dumps(views[-1])


def test_unknown_label_snapshot_and_home_budget_never_create_false_zeroes(home_state, monkeypatch):
    source, snapshot, _, _ = home_state
    monkeypatch.setattr(source.targets, "labels_in_transaction", lambda *args: ())
    result = source.snapshot(SUBJECT, snapshot)
    assert result.gpus == () and "0 reserved" not in json.dumps(
        home_view(snapshot, reservations=result)
    )


def test_global_view_grant_is_explicit_and_never_grants_booking_authority(home_state):
    source, snapshot, state, reader = home_state
    with state[2].begin() as session:
        session.execute(
            update(RoleBinding)
            .where(RoleBinding.role_id == reader)
            .values(scope_type="global", scope_id="*")
        )
        session.execute(delete(RoleBinding).where(RoleBinding.role_id == state[3]["creator"]))
    assert source.snapshot(SUBJECT, snapshot).state_for("node-a", "GPU-target") == "occupied"
    with pytest.raises(DomainError) as error:
        source.access.require_permission(
            AccessRequest(
                user_id=state[3]["alice"],
                permission="reservation.create",
                scope=AccessScope(type="node", id="node-a"),
            )
        )
    assert error.value.code == "PERMISSION_DENIED"


def test_targets_are_bounded_and_duplicate_cross_node_identity_remains_unknown(
    home_state, monkeypatch
):
    source, snapshot, _, _ = home_state
    entry = snapshot.nodes[0]
    original = source.targets.labels_in_transaction
    sizes = []

    def capture(session, query):
        sizes.append(len(query.gpu_uuids))
        return original(session, query)

    monkeypatch.setattr(source.targets, "labels_in_transaction", capture)
    many = [
        entry.node.gpus[0].model_copy(
            update={"gpu_uuid": "GPU-target" if i == 0 else f"GPU-extra-{i}"}
        )
        for i in range(101)
    ]
    large = snapshot.model_copy(
        update={
            "nodes": [
                entry.model_copy(update={"node": entry.node.model_copy(update={"gpus": many})})
            ]
        }
    )
    result = source.snapshot(SUBJECT, large)
    assert sizes and max(sizes) == 100
    assert result.state_for("node-a", "GPU-extra-100") == "unknown"
    other = entry.model_copy(update={"node": entry.node.model_copy(update={"node_id": "node-b"})})
    duplicated = snapshot.model_copy(update={"nodes": [entry, other]})
    assert source.snapshot(SUBJECT, duplicated).state_for("node-a", "GPU-target") == "unknown"
