import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from aris.apps.composition.m2 import build_m2_components
from aris.core.access.models import Permission, RoleBinding, RolePermission
from aris.core.commands.models import CommandRecord
from aris.interfaces.slack.user_commands import CONFIRM_ACTION, STATUS_ACTION
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.vega.models import GpuDevice
from tests.test_m2_composition import config
from tests.test_reservation_service import booking as booking
from tests.test_slack import slack_settings as slack_settings
from tests.test_slack import slash
from tests.test_slack_reservation_drafts import NAMESPACE, actions, click, count, reserve
from tests.test_slack_reservation_drafts import drafts as drafts


@pytest.fixture
def changes(drafts):
    sessions, ids = drafts[3][2:4]
    with sessions.begin() as session:
        for name, role in (
            ("cancel_own", "reader"),
            ("release_own", "reader"),
            ("extend_own", "reader"),
            ("extend", "creator"),
        ):
            permission = str(uuid4())
            session.add(Permission(id=permission, name="reservation." + name))
            session.flush()
            session.add(RolePermission(role_id=ids[role], permission_id=permission))
    return drafts


def booked(state):
    assert reserve(state).status_code == 200
    assert click(state, actions(state[2].updates[-1][1])[CONFIRM_ACTION]).status_code == 200
    assert state[1].run_once("all")["commands"]["state"] == "succeeded"
    with state[3][2]() as session:
        return session.scalar(select(Reservation.id))


def change(state, operation, reservation_id, *, end=None, reason="Private changed plan", **extra):
    text = f"{operation} {reservation_id} "
    if end is not None:
        text += end.isoformat() + " "
    return slash(state[0], text + reason, trigger_id="fixture-change-trigger", **extra)


@pytest.mark.parametrize("operation", ["cancel", "release", "extend"])
def test_changes_review_confirm_worker_and_replayed_body_preserve_original_intent(
    changes, operation
):
    reservation_id = booked(changes)
    state = changes[3]
    end = state[4]["now"] + timedelta(hours=3) if operation == "extend" else None
    if operation == "release":
        state[4]["now"] += timedelta(hours=1, seconds=1)
    assert change(changes, operation, reservation_id, end=end).status_code == 200
    view = changes[2].updates[-1][1]
    text = json.dumps(view)
    assert "Private draft purpose" in text and "Private changed plan" in text
    assert "node-a" in text and "Version: 1" in text and "<!date^" in text
    if operation == "release":
        assert "GPU processes keep running" in text
    if operation == "extend":
        assert "New end" in text
    confirmation = actions(view)[CONFIRM_ACTION]
    with state[2]() as session:
        record = session.scalar(
            select(CommandRecord).where(CommandRecord.command_type == "reservation." + operation)
        )
        original_id, expiry, parameters = (
            record.id,
            record.confirmation_expires_at,
            record.parameters,
        )
        assert parameters["expected_version"] == 1
    assert click(changes, confirmation).status_code == 200
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    with state[2]() as session:
        assert (
            session.scalar(
                select(ReservationEvent.version).order_by(ReservationEvent.version.desc())
            )
            == 2
        )
    assert change(changes, operation, reservation_id, end=end).status_code == 200
    assert "has completed" in json.dumps(changes[2].updates[-1][1])
    assert count(state[2], CommandRecord) == 2
    with state[2]() as session:
        record = session.get(CommandRecord, original_id)
        assert record.parameters == parameters and record.confirmation_expires_at == expiry


def test_changed_version_refresh_suppresses_confirm_without_rewriting_the_original(changes):
    reservation_id = booked(changes)
    change(changes, "cancel", reservation_id)
    view = changes[2].updates[-1][1]
    change(
        changes,
        "extend",
        reservation_id,
        end=changes[3][4]["now"] + timedelta(hours=3),
    )
    click(changes, actions(changes[2].updates[-1][1])[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    assert click(changes, actions(view)[STATUS_ACTION]).status_code == 200
    current = changes[2].updates[-1][1]
    assert CONFIRM_ACTION not in actions(current) and "has changed" in json.dumps(current)
    with changes[3][2]() as session:
        record = session.scalar(
            select(CommandRecord).where(CommandRecord.command_type == "reservation.cancel")
        )
        assert record.parameters["expected_version"] == 1


@pytest.mark.parametrize("operation", ["cancel", "release"])
def test_missing_hardware_label_does_not_prevent_capacity_reduction(changes, operation):
    reservation_id = booked(changes)
    if operation == "release":
        changes[3][4]["now"] += timedelta(hours=1)
    with changes[3][2].begin() as session:
        session.execute(delete(GpuDevice))
    change(changes, operation, reservation_id)
    view = changes[2].updates[-1][1]
    assert "Unavailable" in json.dumps(view) and CONFIRM_ACTION in actions(view)
    click(changes, actions(view)[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"


@pytest.mark.parametrize("operation", ["cancel", "release", "extend"])
def test_other_owner_cannot_read_or_prepare_private_change(changes, operation):
    reservation_id = booked(changes)
    end = changes[3][4]["now"] + timedelta(hours=3) if operation == "extend" else None
    change(changes, operation, reservation_id, end=end, user_id="U222")
    view = changes[2].updates[-1][1]
    assert "Private draft purpose" not in json.dumps(view) and CONFIRM_ACTION not in actions(view)
    assert count(changes[3][2], CommandRecord) == 1


@pytest.mark.parametrize(
    "text",
    [
        "cancel not-a-uuid reason",
        "release",
        f"cancel {uuid4()}",
        f"extend {uuid4()} 2026-12-01T12:00:00 reason",
        f"extend {uuid4()} bad reason",
    ],
)
def test_invalid_change_syntax_does_not_open_or_admit(changes, text):
    response = slash(changes[0], text, trigger_id="fixture-change-trigger")
    assert response.status_code == 200 and "Use /gpu cancel" in response.text
    assert changes[2].opens == [] and count(changes[3][2], CommandRecord) == 0


def test_reduction_only_configuration_needs_no_create_rules(changes, settings, slack_settings):
    graph = build_m2_components(
        changes[3][2],
        settings,
        slack_settings,
        config(
            namespace=NAMESPACE,
            commands=("reservation.cancel", "reservation.release"),
            rules=None,
            maintenance=(),
            slack_callbacks=True,
        ),
        modal_publisher=changes[2],
        clock=lambda: changes[3][4]["now"],
    )
    assert graph.slack_drafts is not None


@pytest.mark.parametrize("operation", ["cancel", "release", "extend"])
def test_inapplicable_state_or_window_has_no_blind_confirmation(changes, operation):
    reservation_id = booked(changes)
    end = changes[3][4]["now"] + timedelta(hours=1) if operation == "extend" else None
    if operation == "cancel":
        changes[3][4]["now"] += timedelta(hours=1)
    change(changes, operation, reservation_id, end=end)
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])


def test_no_history_version_does_not_invent_an_expected_version(changes):
    reservation_id = booked(changes)
    with changes[3][2].begin() as session:
        session.execute(delete(ReservationEvent))
    change(changes, "cancel", reservation_id)
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])
    assert count(changes[3][2], CommandRecord) == 1


def test_extension_missing_resource_label_never_offers_blind_capacity_increase(changes):
    reservation_id = booked(changes)
    with changes[3][2].begin() as session:
        session.execute(delete(GpuDevice))
    change(changes, "extend", reservation_id, end=changes[3][4]["now"] + timedelta(hours=3))
    view = changes[2].updates[-1][1]
    assert "cannot be identified" in json.dumps(view) and CONFIRM_ACTION not in actions(view)


@pytest.mark.parametrize("stage", ["draft", "refresh", "worker"])
def test_current_mine_and_change_permissions_are_not_inferred_from_create(changes, stage):
    reservation_id = booked(changes)
    if stage != "draft":
        change(changes, "cancel", reservation_id)
        view = changes[2].updates[-1][1]
        if stage == "worker":
            click(changes, actions(view)[CONFIRM_ACTION])
    with changes[3][2].begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == changes[3][3]["alice"],
                RoleBinding.role_id == changes[3][3]["reader"],
            )
        )
    if stage == "draft":
        change(changes, "cancel", reservation_id)
        assert count(changes[3][2], CommandRecord) == 1
    elif stage == "refresh":
        click(changes, actions(view)[STATUS_ACTION])
    else:
        assert changes[1].run_once("all")["commands"]["state"] == "failed"
        with changes[3][2]() as session:
            assert session.scalar(select(Reservation.status)) == "confirmed"
        assert count(changes[3][2], ReservationEvent) == 1
        return
    rendered = changes[2].updates[-1][1]
    assert CONFIRM_ACTION not in actions(rendered) and "Private draft purpose" not in json.dumps(
        rendered
    )


def test_fresh_request_after_old_intent_uses_current_version(changes):
    reservation_id = booked(changes)
    now = changes[3][4]["now"]
    change(changes, "extend", reservation_id, end=now + timedelta(hours=3))
    click(changes, actions(changes[2].updates[-1][1])[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    change(changes, "cancel", reservation_id, reason="New explicit request")
    assert "Version: 2" in json.dumps(changes[2].updates[-1][1])


def test_first_admission_race_recovers_canonical_intent_after_version_changes(changes, monkeypatch):
    reservation_id = booked(changes)
    change(changes, "cancel", reservation_id)
    original = changes[2].updates[-1][1]
    change(changes, "extend", reservation_id, end=changes[3][4]["now"] + timedelta(hours=3))
    click(changes, actions(changes[2].updates[-1][1])[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    gateway = changes[1].slack_drafts.gateway
    recover = gateway.recover_by_key
    calls = []

    def missed_first_lookup(context):
        calls.append(context)
        return None if len(calls) == 1 else recover(context)

    monkeypatch.setattr(gateway, "recover_by_key", missed_first_lookup)
    change(changes, "cancel", reservation_id)
    assert len(calls) == 2 and count(changes[3][2], CommandRecord) == 3
    view = changes[2].updates[-1][1]
    assert "has changed" in json.dumps(view) and CONFIRM_ACTION not in actions(view)
    assert (
        json.loads(actions(view)[STATUS_ACTION]["value"])["command_id"]
        == json.loads(actions(original)[STATUS_ACTION]["value"])["command_id"]
    )
