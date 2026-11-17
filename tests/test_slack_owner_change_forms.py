import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from sqlalchemy import delete, select

from aris.core.access.models import RoleBinding
from aris.core.commands.models import CommandRecord
from aris.interfaces.slack.reservation_changes import CHANGE_ACTION_PREFIX, CHANGE_FORM_CALLBACK
from aris.interfaces.slack.user_commands import CONFIRM_ACTION
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.vega.models import GpuDevice
from tests.test_reservation_service import booking as booking
from tests.test_slack import signed, slash
from tests.test_slack import slack_settings as slack_settings
from tests.test_slack_reservation_changes import booked
from tests.test_slack_reservation_changes import change as slash_change
from tests.test_slack_reservation_changes import changes as changes
from tests.test_slack_reservation_drafts import actions, click, count
from tests.test_slack_reservation_drafts import drafts as drafts


def open_change(state, operation, reservation_id, *, user="U111", edit=None):
    payload = {
        "type": "block_actions",
        "team": {"id": "T111"},
        "user": {"id": user},
        "trigger_id": "private-owner-change-trigger",
        "container": {"type": "message"},
        "actions": [{"action_id": CHANGE_ACTION_PREFIX + operation, "value": str(reservation_id)}],
    }
    if edit:
        edit(payload)
    return send(state, payload)


def send(state, payload):
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return state[0].post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )


def submit(state, form, *, view_id="VCHANGE1", user="U111", edit=None):
    selection = json.loads(form["private_metadata"])
    values = {
        "reason": {
            "aris.change.reason": {
                "type": "plain_text_input",
                "value": "Private native changed plan",
            }
        }
    }
    if selection["operation"] == "extend":
        values["end"] = {
            "aris.change.end": {
                "type": "datetimepicker",
                "selected_date_time": int((state[3][4]["now"] + timedelta(hours=3)).timestamp()),
            }
        }
    payload = {
        "type": "view_submission",
        "team": {"id": "T111"},
        "user": {"id": user},
        "view": {
            "type": "modal",
            "id": view_id,
            "callback_id": CHANGE_FORM_CALLBACK,
            "hash": "old-change-form-hash",
            "private_metadata": form["private_metadata"],
            "state": {"values": values},
        },
    }
    if edit:
        edit(payload)
    return send(state, payload)


@pytest.mark.parametrize("operation", ["cancel", "release", "extend"])
def test_native_owner_action_ack_form_canonical_confirm_worker_and_replay(
    changes, operation, monkeypatch
):
    reservation_id = booked(changes)
    if operation == "release":
        changes[3][4]["now"] += timedelta(hours=1, seconds=1)
    assert open_change(changes, operation, reservation_id).status_code == 200
    form = changes[2].updates[-1][1]
    assert form["callback_id"] == CHANGE_FORM_CALLBACK
    assert "Private draft purpose" in json.dumps(form) and "node-a" in json.dumps(form)
    assert count(changes[3][2], CommandRecord) == 1
    gateway = changes[1].slack_drafts.gateway
    original = gateway.submit
    before = len(changes[2].events)

    def only_after_ack(*args, **kwargs):
        assert len(changes[2].events) == before + 1
        return original(*args, **kwargs)

    monkeypatch.setattr(gateway, "submit", only_after_ack)
    response = submit(changes, form)
    assert response.status_code == 200 and response.json()["response_action"] == "update"
    review = changes[2].updates[-1][1]
    assert CONFIRM_ACTION in actions(review)
    assert "Private native changed plan" in json.dumps(review)
    assert "Version: 1" in json.dumps(review) and "node-a" in json.dumps(review)
    assert changes[2].updates[-1][0].hash is None
    monkeypatch.setattr(gateway, "submit", original)
    with changes[3][2]() as session:
        record = session.scalar(
            select(CommandRecord).where(CommandRecord.command_type == "reservation." + operation)
        )
        original_id, expiry, parameters = (
            record.id,
            record.confirmation_expires_at,
            record.parameters,
        )
    click(changes, actions(review)[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    assert count(changes[3][2], ReservationEvent) == 2
    click(changes, actions(review)[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"] is None
    assert submit(changes, form).status_code == 200
    assert "has completed" in json.dumps(changes[2].updates[-1][1])
    with changes[3][2]() as session:
        record = session.get(CommandRecord, original_id)
        assert record.confirmation_expires_at == expiry and record.parameters == parameters
    assert count(changes[3][2], CommandRecord) == 2


def test_personal_list_buttons_follow_status_time_and_exposed_commands(changes):
    reservation_id = booked(changes)

    def names():
        response = slash(changes[0], "mine")
        return actions(response.json())

    assert set(names()) == {CHANGE_ACTION_PREFIX + "cancel", CHANGE_ACTION_PREFIX + "extend"}
    assert all(item["value"] == reservation_id for item in names().values())
    changes[3][4]["now"] += timedelta(hours=1)
    assert set(names()) == {CHANGE_ACTION_PREFIX + "release", CHANGE_ACTION_PREFIX + "extend"}
    changes[3][4]["now"] += timedelta(hours=1)
    assert set(names()) == {CHANGE_ACTION_PREFIX + "release"}
    changes[1].slack_reservations.change_commands = ("reservation.create",)
    assert names() == {}


def test_changed_form_choices_are_rejected_without_renewing_or_admitting_another_intent(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    submit(changes, form)
    original = changes[2].updates[-1][1]
    submit(
        changes,
        form,
        edit=lambda p: p["view"]["state"]["values"]["reason"]["aris.change.reason"].update(
            value="Different reason"
        ),
    )
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])
    assert count(changes[3][2], CommandRecord) == 2
    submit(changes, form)
    assert changes[2].updates[-1][1] == original


def test_other_owner_cannot_read_form_or_admit_using_stolen_metadata(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    open_change(changes, "cancel", reservation_id, user="U222")
    assert "Private draft purpose" not in json.dumps(changes[2].updates[-1][1])
    assert "private_metadata" not in changes[2].updates[-1][1]
    submit(changes, form, user="U222")
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])
    assert "Private draft purpose" not in json.dumps(changes[2].updates[-1][1])
    assert count(changes[3][2], CommandRecord) == 1


def test_revocation_between_open_and_submission_prevents_admission(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    with changes[3][2].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == changes[3][3]["alice"]))
    submit(changes, form)
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])
    assert count(changes[3][2], CommandRecord) == 1


@pytest.mark.parametrize("operation", ["cancel", "release"])
def test_missing_hardware_labels_do_not_prevent_native_capacity_reduction(changes, operation):
    reservation_id = booked(changes)
    if operation == "release":
        changes[3][4]["now"] += timedelta(hours=1)
    with changes[3][2].begin() as session:
        session.execute(delete(GpuDevice))
    open_change(changes, operation, reservation_id)
    form = changes[2].updates[-1][1]
    assert "GPU information unavailable" in json.dumps(form)
    submit(changes, form)
    review = changes[2].updates[-1][1]
    assert CONFIRM_ACTION in actions(review)
    click(changes, actions(review)[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"


@pytest.mark.parametrize(
    "field,value", [("reason", " "), ("reason", "x" * 257), ("end", True), ("end", -1)]
)
def test_invalid_native_fields_report_inline_errors_without_admission(changes, field, value):
    reservation_id = booked(changes)
    open_change(changes, "extend", reservation_id)
    form = changes[2].updates[-1][1]

    def edit(payload):
        entry = payload["view"]["state"]["values"][field]["aris.change." + field]
        entry["value" if field == "reason" else "selected_date_time"] = value

    response = submit(changes, form, edit=edit)
    assert response.status_code == 200 and field in response.json()["errors"]
    assert count(changes[3][2], CommandRecord) == 1


@pytest.mark.parametrize("extra", ["actor_id", "scope", "expected_version"])
def test_client_cannot_supply_authority_or_version_in_selection(changes, extra):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]

    def edit(payload):
        selection = json.loads(payload["view"]["private_metadata"])
        selection[extra] = "forged"
        payload["view"]["private_metadata"] = json.dumps(selection)

    assert submit(changes, form, edit=edit).status_code == 422
    assert count(changes[3][2], CommandRecord) == 1


def test_unconfirmed_native_request_is_not_executed_and_cancelled_row_has_no_buttons(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    submit(changes, form)
    assert changes[1].run_once("all")["commands"] is None
    with changes[3][2]() as session:
        assert session.get(Reservation, reservation_id).status == "confirmed"
    click(changes, actions(changes[2].updates[-1][1])[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    assert actions(slash(changes[0], "mine").json()) == {}


def test_replayed_pending_form_keeps_original_version_after_another_change(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    submit(changes, form)
    with changes[3][2]() as session:
        original = session.scalar(
            select(CommandRecord).where(CommandRecord.command_type == "reservation.cancel")
        )
        command_id, expiry = original.id, original.confirmation_expires_at
    slash_change(changes, "extend", reservation_id, end=changes[3][4]["now"] + timedelta(hours=3))
    click(changes, actions(changes[2].updates[-1][1])[CONFIRM_ACTION])
    assert changes[1].run_once("all")["commands"]["state"] == "succeeded"
    submit(changes, form)
    assert CONFIRM_ACTION not in actions(changes[2].updates[-1][1])
    assert "has changed" in json.dumps(changes[2].updates[-1][1])
    with changes[3][2]() as session:
        original = session.get(CommandRecord, command_id)
        assert original.parameters["expected_version"] == 1
        assert original.confirmation_expires_at == expiry
    assert count(changes[3][2], CommandRecord) == 3


@pytest.mark.parametrize("kind", ["missing_trigger", "multiple_actions", "unknown_operation"])
def test_malformed_button_context_cannot_start_a_form(changes, kind):
    reservation_id = booked(changes)
    before = len(changes[2].opens)

    def edit(payload):
        if kind == "missing_trigger":
            payload.pop("trigger_id")
        elif kind == "multiple_actions":
            payload["actions"].append(payload["actions"][0].copy())
        else:
            payload["actions"][0]["action_id"] = CHANGE_ACTION_PREFIX + "cancel_any"

    assert open_change(changes, "cancel", reservation_id, edit=edit).status_code in {403, 422}
    assert len(changes[2].opens) == before
    assert count(changes[3][2], CommandRecord) == 1


def test_closing_native_form_does_not_withdraw_or_admit_a_command(changes):
    reservation_id = booked(changes)
    open_change(changes, "cancel", reservation_id)
    form = changes[2].updates[-1][1]
    response = send(
        changes,
        {
            "type": "view_closed",
            "team": {"id": "T111"},
            "user": {"id": "U111"},
            "view": {
                "type": "modal",
                "id": "VCHANGE1",
                "callback_id": CHANGE_FORM_CALLBACK,
                "private_metadata": form["private_metadata"],
            },
        },
    )
    assert response.status_code == 200 and count(changes[3][2], CommandRecord) == 1
