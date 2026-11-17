import json
import time
from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from aris.apps.api.main import create_app
from aris.apps.composition.m2 import build_m2_components
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.commands.models import CommandJob, CommandRecord
from aris.core.notifications.models import NotificationOutbox
from aris.interfaces.slack.client import SlackDeliveryError
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.schemas import ModalHandle, SlashCommand
from aris.interfaces.slack.user_commands import CONFIRM_ACTION, STATUS_ACTION, WITHDRAW_ACTION
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.vega.models import GpuDevice
from tests.test_availability import calendar
from tests.test_m2_composition import config
from tests.test_reservation_service import booking as booking
from tests.test_slack import signed, slash
from tests.test_slack import slack_settings as slack_settings

NAMESPACE = IdentityNamespace(provider="slack", namespace="T111")


class AckObserved:
    def __init__(self, app, events):
        self.app, self.events = app, events

    async def __call__(self, scope, receive, send):
        async def observed(message):
            await send(message)
            if (
                scope["type"] == "http"
                and scope["path"] == "/slack/commands"
                and message["type"] == "http.response.body"
                and not message.get("more_body", False)
            ):
                self.events.append("ack")

        await self.app(scope, receive, observed)


class FakeModal:
    def __init__(self, sessions, events):
        self.sessions, self.events = sessions, events
        self.opens, self.updates, self.states = [], [], []
        self.fail_open = self.fail_update = False

    def open_modal(self, trigger, view):
        assert "ack" in self.events, "modal I/O must start after the response body"
        with self.sessions() as session:
            self.states.append(
                ("open", session.scalar(select(func.count()).select_from(CommandRecord)))
            )
        self.opens.append((trigger, view))
        if self.fail_open:
            raise SlackDeliveryError("transport_error")
        return ModalHandle("VMODAL1", "fixture-open-hash")

    def update_modal(self, handle, view):
        # A separate connection can take the command and resource locks here:
        # all UI I/O is outside the admission/review/label transactions.
        with self.sessions.begin() as session:
            session.execute(select(CommandRecord).with_for_update(nowait=True)).all()
            session.execute(select(GpuDevice).with_for_update(nowait=True)).all()
            self.states.append(
                ("update", session.scalar(select(func.count()).select_from(Reservation)))
            )
        self.updates.append((handle, view))
        if self.fail_update:
            raise SlackDeliveryError("upstream_unavailable")


@pytest.fixture
def drafts(booking, settings, slack_settings):
    _, _, sessions, ids, clock, _ = booking
    with sessions.begin() as session:
        for user, external_id in (("alice", "U111"), ("bob", "U222")):
            session.add(
                ExternalIdentity(
                    id=str(uuid4()),
                    user_id=ids[user],
                    provider="slack",
                    namespace="T111",
                    external_id=external_id,
                )
            )
    events = []
    modal = FakeModal(sessions, events)
    cfg = config(
        namespace=NAMESPACE, slack_callbacks=True, maintenance=(calendar(),), user_tokens=()
    )
    graph = build_m2_components(
        sessions, settings, slack_settings, cfg, clock=lambda: clock["now"], modal_publisher=modal
    )
    app = create_app(
        settings,
        slack_settings,
        slack_commands=graph.slack_callbacks,
        slack_reservations=graph.slack_reservations,
        slack_drafts=graph.slack_drafts,
    )
    with TestClient(AckObserved(app, events), raise_server_exceptions=False) as client:
        yield client, graph, modal, booking


def reserve(state, text=None, **changes):
    client, _, _, booking = state
    now = booking[4]["now"]
    text = text or (
        f"reserve GPU-target {(now + timedelta(hours=1)).isoformat()} "
        f"{(now + timedelta(hours=2)).isoformat()} Private draft purpose"
    )
    return slash(client, text, trigger_id="fixture-short-lived-trigger", **changes)


def actions(view):
    return {
        element["action_id"]: element
        for block in view["blocks"]
        if block["type"] == "actions"
        for element in block["elements"]
    }


def click(state, action, *, user="U111", callback="aris.command.confirmation"):
    payload = {
        "type": "block_actions",
        "team": {"id": "T111"},
        "user": {"id": user},
        "view": {
            "type": "modal",
            "id": "VMODAL1",
            "hash": "fixture-current-hash",
            "callback_id": callback,
        },
        "actions": [{"action_id": action["action_id"], "value": action["value"]}],
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return state[0].post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_slash_review_confirm_worker_notice_and_mine_form_a_real_database_workflow(drafts):
    client, graph, modal, state = drafts
    response = reserve(drafts)
    assert (
        response.status_code == 200
        and response.json()["text"] == "Opening your reservation review."
    )
    assert modal.states == [("open", 0), ("update", 0)]
    assert count(state[2], Reservation) == count(state[2], CommandJob) == 0
    view = modal.updates[-1][1]
    serialized = json.dumps(view)
    assert (
        "node-a" in serialized and "GPU 0" in serialized and "Private draft purpose" in serialized
    )
    assert "<!date^" in serialized and "not guaranteed" in serialized
    confirmation = actions(view)[CONFIRM_ACTION]
    assert len(json.loads(confirmation["value"])["proof"]) == 64
    assert click(drafts, confirmation).status_code == 200
    assert count(state[2], Reservation) == 0 and count(state[2], CommandJob) == 1
    report = graph.run_once("all")
    assert (
        report["commands"]["state"] == "succeeded" and not report["notification_delivery_enabled"]
    )
    assert (
        count(state[2], Reservation)
        == count(state[2], ReservationEvent)
        == count(state[2], NotificationOutbox)
        == 1
    )
    assert click(drafts, confirmation).status_code == 200
    assert count(state[2], CommandJob) == 1 and count(state[2], ReservationEvent) == 1
    mine = slash(client, "mine")
    assert mine.status_code == 200 and "Private draft purpose" in mine.text
    with state[2]() as session:
        assert session.scalar(select(Reservation.owner_id)) == state[3]["alice"]
        assert session.scalar(select(NotificationOutbox.state)) == "queued"


def test_duplicate_slash_keeps_one_pending_intent_and_never_executes_implicitly(drafts):
    reserve(drafts)
    first = actions(drafts[2].updates[-1][1])[CONFIRM_ACTION]["value"]
    reserve(drafts)
    assert actions(drafts[2].updates[-1][1])[CONFIRM_ACTION]["value"] == first
    assert count(drafts[3][2], CommandRecord) == 1
    assert count(drafts[3][2], CommandJob) == count(drafts[3][2], Reservation) == 0


@pytest.mark.parametrize(
    "text",
    [
        "reserve",
        "reserve GPU-target bad-date bad-date purpose",
        "reserve GPU-target 2026-11-17T01:00:00 2026-11-17T02:00:00 purpose",
        "reserve GPU-target 2026-11-17T01:00:00Z 2026-11-17T01:00:00Z purpose",
    ],
)
def test_invalid_syntax_or_times_only_return_usage(drafts, text):
    response = reserve(drafts, text)
    assert response.status_code == 200 and "Both times must include a timezone" in response.text
    assert drafts[2].opens == [] and count(drafts[3][2], CommandRecord) == 0


def test_open_failure_does_not_admit_a_command_but_update_failure_retains_pending(drafts, caplog):
    _, _, modal, state = drafts
    modal.fail_open = True
    assert reserve(drafts).status_code == 200 and count(state[2], CommandRecord) == 0
    modal.fail_open = False
    modal.fail_update = True
    assert reserve(drafts).status_code == 200
    assert count(state[2], CommandRecord) == 1 and count(state[2], Reservation) == 0
    proof = json.loads(actions(modal.updates[-1][1])[CONFIRM_ACTION]["value"])["proof"]
    assert proof not in caplog.text and "Private draft purpose" not in caplog.text
    assert "fixture-short-lived-trigger" not in caplog.text


def test_resource_label_missing_or_changed_never_offers_a_blind_confirmation(drafts, monkeypatch):
    presenter = drafts[1].slack_drafts.presenter
    monkeypatch.setattr(presenter.targets, "label_in_transaction", lambda *args: None)
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    assert CONFIRM_ACTION not in actions(view)
    assert STATUS_ACTION in actions(view) and WITHDRAW_ACTION in actions(view)
    assert "cannot be identified" in json.dumps(view)


def test_refresh_recovers_full_canonical_preview_and_preserves_original_bounds(drafts):
    reserve(drafts)
    original = drafts[2].updates[-1][1]
    action = actions(original)[STATUS_ACTION]
    assert click(drafts, action).status_code == 200
    updated = drafts[2].updates[-1][1]
    assert updated == original
    assert drafts[2].updates[-1][0].hash == "fixture-current-hash"


def test_refresh_after_expiry_removes_confirmation_without_running_business(drafts):
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    with drafts[3][2]() as session:
        expiry = session.scalar(select(CommandRecord.confirmation_expires_at))
    from aris.core.time import utc

    drafts[3][4]["now"] = utc(expiry) + timedelta(seconds=1)
    assert click(drafts, actions(view)[STATUS_ACTION]).status_code == 200
    assert CONFIRM_ACTION not in actions(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandJob) == count(drafts[3][2], Reservation) == 0


def test_other_user_cannot_use_the_original_review_proof(drafts):
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    assert click(drafts, actions(view)[CONFIRM_ACTION], user="U222").status_code == 200
    assert "not authorized" in json.dumps(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandJob) == count(drafts[3][2], Reservation) == 0


def test_revocation_blocks_confirm_but_still_allows_withdrawing_original_pending_request(drafts):
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    with drafts[3][2].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == drafts[3][3]["alice"]))
    assert click(drafts, actions(view)[CONFIRM_ACTION]).status_code == 200
    assert count(drafts[3][2], CommandJob) == 0
    assert click(drafts, actions(view)[WITHDRAW_ACTION]).status_code == 200
    with drafts[3][2]() as session:
        assert session.scalar(select(CommandRecord.state)) == "cancelled"


def test_confirmed_request_with_later_stale_facts_does_not_create_a_reservation_or_notice(drafts):
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    assert click(drafts, actions(view)[CONFIRM_ACTION]).status_code == 200
    drafts[3][4]["now"] += timedelta(seconds=61)
    report = drafts[1].run_once("all")
    assert report["commands"]["state"] == "failed"
    assert count(drafts[3][2], Reservation) == count(drafts[3][2], NotificationOutbox) == 0


def test_signed_unknown_user_is_not_automatically_registered(drafts):
    response = reserve(drafts, user_id="U999")
    assert response.status_code == 200
    assert "not authorized" in json.dumps(drafts[2].updates[-1][1])
    assert count(drafts[3][2], User) == 2 and count(drafts[3][2], CommandRecord) == 0


def test_purpose_mentions_remain_plain_text_in_confirmation(drafts):
    now = drafts[3][4]["now"]
    hostile = "<!channel> <@U222> *private*"
    reserve(
        drafts,
        f"reserve GPU-target {(now + timedelta(hours=1)).isoformat()} "
        f"{(now + timedelta(hours=2)).isoformat()} {hostile}",
    )
    view = drafts[2].updates[-1][1]
    matches = [
        block["text"]
        for block in view["blocks"]
        if isinstance(block.get("text"), dict) and hostile in block["text"].get("text", "")
    ]
    assert len(matches) == 1 and matches[0]["type"] == "plain_text"


def test_owned_view_close_or_submission_never_enters_home_queue_with_proof(drafts):
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    for kind, status in (("view_closed", 200), ("view_submission", 422)):
        payload = {
            "type": kind,
            "team": {"id": "T111"},
            "user": {"id": "U111"},
            "view": {**view, "id": "VMODAL1", "hash": "fixture-hash"},
        }
        body = urlencode({"payload": json.dumps(payload)}).encode()
        response = drafts[0].post(
            "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
        )
        assert response.status_code == status
    assert count(drafts[3][2], SlackReceipt) == count(drafts[3][2], SlackHomeJob) == 0
    with drafts[3][2]() as session:
        assert session.scalar(select(CommandRecord.state)) == "awaiting_confirmation"


def test_default_app_keeps_reservation_creation_disabled(settings, slack_settings):
    with TestClient(create_app(settings, slack_settings)) as client:
        assert slash(client, "reserve").status_code == 503


def test_expired_trigger_skips_modal_and_admission(drafts):
    now = drafts[3][4]["now"]
    command = SlashCommand(
        team_id="T111",
        user_id="U111",
        command="/gpu",
        trigger_id="fixture-trigger",
        text=f"reserve GPU-target {(now + timedelta(hours=1)).isoformat()} "
        f"{(now + timedelta(hours=2)).isoformat()} purpose",
    )
    reply = drafts[1].slack_drafts.start(
        command, b"fixture-body", "fixture-expired-trigger", time.monotonic() - 3
    )
    assert reply.after_ack is not None
    reply.after_ack()
    assert drafts[2].opens == [] and count(drafts[3][2], CommandRecord) == 0


def test_missing_trigger_or_invalid_signature_never_opens_a_review(drafts):
    now = drafts[3][4]["now"]
    text = f"reserve GPU-target {(now + timedelta(hours=1)).isoformat()} "
    text += f"{(now + timedelta(hours=2)).isoformat()} purpose"
    assert slash(drafts[0], text).status_code == 422
    body = urlencode(
        {
            "team_id": "T111",
            "user_id": "U111",
            "command": "/gpu",
            "text": text,
            "trigger_id": "fixture-trigger",
        }
    ).encode()
    auth = signed(body, "application/x-www-form-urlencoded")
    auth["X-Slack-Signature"] = "v0:" + "0" * 64
    assert drafts[0].post("/slack/commands", content=body, headers=auth).status_code == 401
    assert drafts[2].opens == [] and count(drafts[3][2], CommandRecord) == 0


def test_changed_label_node_never_relabels_original_intent_with_a_confirm_button(
    drafts, monkeypatch
):
    presenter = drafts[1].slack_drafts.presenter
    original = presenter.targets.label_in_transaction

    def moved_label(session, target):
        return original(session, target).model_copy(update={"node_id": "node-b"})

    monkeypatch.setattr(presenter.targets, "label_in_transaction", moved_label)
    reserve(drafts)
    view = drafts[2].updates[-1][1]
    assert CONFIRM_ACTION not in actions(view) and "node-b" not in json.dumps(view)


def test_key_rotation_prevents_refresh_from_issuing_a_new_confirmation(drafts):
    from pydantic import SecretStr

    reserve(drafts)
    view = drafts[2].updates[-1][1]
    drafts[1].intake.commands.confirmation_key = SecretStr(
        "fixture-rotated-key-12345678901234567890"
    )
    assert click(drafts, actions(view)[STATUS_ACTION]).status_code == 200
    assert CONFIRM_ACTION not in actions(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandJob) == 0


def test_composition_does_not_offer_create_when_only_reductions_are_exposed(
    booking, settings, slack_settings
):
    cfg = config(
        namespace=NAMESPACE,
        commands=("reservation.cancel",),
        slack_callbacks=True,
        rules=None,
        maintenance=(),
    )
    graph = build_m2_components(booking[2], settings, slack_settings, cfg)
    assert graph.slack_drafts is None and graph.slack_callbacks is not None
