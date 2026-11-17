import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import sessionmaker

from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.commands.models import CommandRecord
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.reservation_forms import FORM_CALLBACK
from aris.interfaces.slack.user_commands import CONFIRM_ACTION
from aris.modules.nova.models import Reservation
from aris.modules.vega.models import GpuDevice
from aris.modules.vega.target_schemas import GpuCatalogQuery
from tests.test_reservation_service import booking as booking
from tests.test_slack import signed, slash
from tests.test_slack import slack_settings as slack_settings
from tests.test_slack_reservation_drafts import actions, click, count
from tests.test_slack_reservation_drafts import drafts as drafts


def form_submission(state, *, user="U111", view_id="VFORM1", edit=None, kind="view_submission"):
    now = state[3][4]["now"]
    values = {
        "gpu": {
            "aris.reserve.gpu": {
                "type": "static_select",
                "selected_option": {
                    "value": "GPU-target",
                    "text": {"type": "plain_text", "text": "Client supplied label"},
                },
            }
        },
        "purpose": {
            "aris.reserve.purpose": {"type": "plain_text_input", "value": "Private form purpose"}
        },
    }
    for key, hour in (("start", 1), ("end", 2)):
        values[key] = {
            "aris.reserve." + key: {
                "type": "datetimepicker",
                "selected_date_time": int((now + timedelta(hours=hour)).timestamp()),
            }
        }
    payload = {
        "type": kind,
        "team": {"id": "T111"},
        "user": {"id": user},
        "view": {
            "type": "modal",
            "id": view_id,
            "hash": "old-form-hash",
            "callback_id": FORM_CALLBACK,
            "state": {"values": values},
        },
    }
    if edit:
        edit(payload)
    body = urlencode({"payload": json.dumps(payload)}).encode()
    return state[0].post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )


def open_form(state, **changes):
    return slash(state[0], "reserve", trigger_id="fixture-form-trigger", **changes)


def choices(state):
    return state[1].slack_drafts.presenter.queries


def actor(user="U111"):
    return ExternalCommandActor(
        subject={"provider": "slack", "namespace": "T111", "external_id": user},
        request_id="fixture-form-query",
    )


def test_graphical_form_ack_then_canonical_review_then_explicit_confirmation(drafts, monkeypatch):
    response = open_form(drafts)
    assert response.status_code == 200 and "Opening your reservation form" in response.text
    form = drafts[2].updates[-1][1]
    assert form["callback_id"] == FORM_CALLBACK and form["submit"]["text"] == "Review request"
    gpu = next(block for block in form["blocks"] if block.get("block_id") == "gpu")
    assert [(option["text"]["text"], option["value"]) for option in gpu["element"]["options"]] == [
        ("node-a · GPU 0 · Fixture", "GPU-target")
    ]
    assert count(drafts[3][2], CommandRecord) == 0
    gateway = drafts[1].slack_drafts.gateway
    submit = gateway.submit
    before = len(drafts[2].events)

    def after_ack(*args, **kwargs):
        assert len(drafts[2].events) == before + 1
        return submit(*args, **kwargs)

    monkeypatch.setattr(gateway, "submit", after_ack)
    response = form_submission(drafts)
    assert response.status_code == 200 and response.json()["response_action"] == "update"
    assert "No reservation has been made" in response.text
    view = drafts[2].updates[-1][1]
    assert "node-a" in json.dumps(view) and "Client supplied label" not in json.dumps(view)
    assert "Private form purpose" in json.dumps(view)
    assert drafts[2].updates[-1][0].hash is None
    assert count(drafts[3][2], Reservation) == 0
    monkeypatch.setattr(gateway, "submit", submit)
    click(drafts, actions(view)[CONFIRM_ACTION])
    assert drafts[1].run_once("all")["commands"]["state"] == "succeeded"
    assert count(drafts[3][2], Reservation) == 1
    assert count(drafts[3][2], SlackReceipt) == count(drafts[3][2], SlackHomeJob) == 0


def test_same_form_replay_does_not_renew_or_duplicate_and_changed_choices_conflict(drafts):
    form_submission(drafts)
    first = drafts[2].updates[-1][1]
    with drafts[3][2]() as session:
        record = session.scalar(select(CommandRecord))
        original_id, expiry, params = (
            record.id,
            utc(record.confirmation_expires_at),
            record.parameters,
        )
    form_submission(drafts)
    assert drafts[2].updates[-1][1] == first and count(drafts[3][2], CommandRecord) == 1
    form_submission(
        drafts,
        edit=lambda p: p["view"]["state"]["values"]["purpose"]["aris.reserve.purpose"].update(
            value="Different choices"
        ),
    )
    assert CONFIRM_ACTION not in actions(drafts[2].updates[-1][1])
    with drafts[3][2]() as session:
        record = session.get(CommandRecord, original_id)
        assert record.parameters == params and utc(record.confirmation_expires_at) == expiry
    assert count(drafts[3][2], CommandRecord) == 1
    click(drafts, actions(first)[CONFIRM_ACTION])
    assert drafts[1].run_once("all")["commands"]["state"] == "succeeded"
    form_submission(drafts)
    assert "has completed" in json.dumps(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandRecord) == count(drafts[3][2], Reservation) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("start", None),
        ("start", True),
        ("start", "1234567890"),
        ("end", -1),
        ("end", 10**20),
        ("purpose", "   "),
        ("purpose", "x" * 257),
        ("gpu", ""),
    ],
)
def test_invalid_fields_return_block_errors_without_network_or_admission(drafts, field, value):
    def edit(payload):
        entry = payload["view"]["state"]["values"][field]["aris.reserve." + field]
        if field in {"start", "end"}:
            entry["selected_date_time"] = value
        elif field == "purpose":
            entry["value"] = value
        else:
            entry["selected_option"]["value"] = value

    response = form_submission(drafts, edit=edit)
    assert response.status_code == 200 and field in response.json()["errors"]
    assert drafts[2].updates == [] and count(drafts[3][2], CommandRecord) == 0


def test_invalid_window_returns_end_error_without_admission(drafts):
    def edit(payload):
        values = payload["view"]["state"]["values"]
        values["end"]["aris.reserve.end"] = values["start"]["aris.reserve.start"].copy()

    response = form_submission(drafts, edit=edit)
    assert response.status_code == 200 and set(response.json()["errors"]) == {"end"}
    assert count(drafts[3][2], CommandRecord) == 0


def test_forged_gpu_choice_uses_real_scope_and_never_trusts_option_text(drafts):
    with drafts[3][2].begin() as session:
        session.add(
            GpuDevice(
                gpu_uuid="GPU-other",
                node_id="node-b",
                index=0,
                model="Other node",
                memory_total_mib=100,
                memory_used_mib=0,
                utilization_percent=0,
                present=True,
            )
        )
    open_form(drafts)
    assert "GPU-other" not in json.dumps(drafts[2].updates[-1][1])

    def edit(payload):
        payload["view"]["state"]["values"]["gpu"]["aris.reserve.gpu"]["selected_option"][
            "value"
        ] = "GPU-other"
        payload["view"]["private_metadata"] = json.dumps(
            {"scope": "node-a", "owner": drafts[3][3]["alice"]}
        )

    form_submission(drafts, edit=edit)
    assert CONFIRM_ACTION not in actions(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandRecord) == 0


@pytest.mark.parametrize("stage", ["choices", "submission"])
def test_current_create_permission_is_required_even_with_mine_read_grant(drafts, stage):
    if stage == "submission":
        open_form(drafts)
    with drafts[3][2].begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == drafts[3][3]["alice"],
                RoleBinding.role_id == drafts[3][3]["creator"],
            )
        )
    if stage == "choices":
        assert choices(drafts).create_choices(actor()).labels == ()
    else:
        form_submission(drafts)
        assert CONFIRM_ACTION not in actions(drafts[2].updates[-1][1])
    assert count(drafts[3][2], CommandRecord) == 0


@pytest.mark.parametrize("change", ["revoke", "remap", "move"])
def test_choice_query_rechecks_current_identity_grants_and_resource_node(
    drafts, monkeypatch, change
):
    queries = choices(drafts)
    original = queries.labels.labels_in_transaction

    def changed(session, query):
        with drafts[3][2].begin() as other:
            if change == "revoke":
                other.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == drafts[3][3]["alice"],
                        RoleBinding.role_id == drafts[3][3]["creator"],
                    )
                )
            elif change == "remap":
                other.execute(
                    update(ExternalIdentity)
                    .where(ExternalIdentity.external_id == "U111")
                    .values(user_id=drafts[3][3]["bob"])
                )
            else:
                other.execute(update(GpuDevice).values(node_id="node-b"))
        return original(session, query)

    monkeypatch.setattr(queries.labels, "labels_in_transaction", changed)
    if change == "remap":
        with pytest.raises(DomainError) as denied:
            queries.create_choices(actor())
        assert denied.value.code == "PERMISSION_DENIED"
    else:
        assert queries.create_choices(actor()).labels == ()


def test_choices_are_bounded_present_inventory_and_not_an_availability_promise(drafts):
    with drafts[3][2].begin() as session:
        for index in range(1, 102):
            session.add(
                GpuDevice(
                    gpu_uuid=f"GPU-{index}",
                    node_id="node-a",
                    index=index,
                    model="Long model " + "x" * 120,
                    memory_total_mib=100,
                    memory_used_mib=0,
                    utilization_percent=0,
                    present=index != 1,
                )
            )
    page = choices(drafts).create_choices(actor())
    assert len(page.labels) == 100 and page.truncated
    assert "GPU-1" not in {label.gpu_uuid for label in page.labels}
    open_form(drafts)
    form = drafts[2].updates[-1][1]
    options = next(block for block in form["blocks"] if block.get("block_id") == "gpu")["element"][
        "options"
    ]
    assert all(len(option["text"]["text"]) <= 75 for option in options)
    assert "first 100" in json.dumps(form) and "does not reserve" in json.dumps(form)
    assert count(drafts[3][2], CommandRecord) == 0


@pytest.mark.parametrize("kind", ["view_closed", "block_actions"])
def test_closed_and_unsupported_form_interactions_never_enter_home_queue(drafts, kind):
    response = form_submission(drafts, kind=kind)
    assert response.status_code == (200 if kind == "view_closed" else 422)
    assert count(drafts[3][2], SlackReceipt) == count(drafts[3][2], SlackHomeJob) == 0
    assert count(drafts[3][2], CommandRecord) == 0


def test_form_catalog_is_a_read_only_no_flush_query_on_postgres(drafts):
    sessions = drafts[3][2]
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL read-only inventory form contract")
    targets = drafts[1].slack_drafts.presenter.targets
    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        device = session.get(GpuDevice, "GPU-target")
        device.model = "Uncommitted label"
        page = targets.catalog_in_transaction(session, GpuCatalogQuery())
        assert page.labels[0].model == "Fixture" and device in session.dirty
        session.rollback()
    assert choices(drafts).create_choices(actor()).labels[0].model == "Fixture"
    engine = sessions.kw["bind"].execution_options(postgresql_readonly=True)
    readonly = sessionmaker(bind=engine)
    query = choices(drafts)
    guarded = PersonalReservationQueries(
        readonly, query.reservations, query.namespace, query.limits, labels=query.labels
    )
    assert guarded.create_choices(actor()).labels[0].model == "Fixture"


def test_postgres_form_catalog_timeout_returns_unavailable_instead_of_empty_choices(
    drafts, monkeypatch
):
    sessions = drafts[3][2]
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL inventory form query budget")

    def slow(session, query):
        session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(choices(drafts).labels, "catalog_in_transaction", slow)
    with pytest.raises(DomainError) as unavailable:
        choices(drafts).create_choices(actor())
    assert unavailable.value.code == "QUERY_UNAVAILABLE"


@pytest.mark.parametrize("change", ["extra", "missing", "action"])
def test_unexpected_form_blocks_and_action_ids_are_rejected_before_admission(drafts, change):
    def edit(payload):
        values = payload["view"]["state"]["values"]
        if change == "extra":
            values["owner"] = {"owner": {"value": drafts[3][3]["bob"]}}
        elif change == "missing":
            values.pop("purpose")
        else:
            values["gpu"]["other.action"] = values["gpu"].pop("aris.reserve.gpu")

    assert form_submission(drafts, edit=edit).status_code == 422
    assert drafts[2].updates == [] and count(drafts[3][2], CommandRecord) == 0


def test_unknown_identity_and_modal_failures_do_not_enroll_or_log_private_inputs(drafts, caplog):
    open_form(drafts, user_id="U999")
    assert "could not be verified" in json.dumps(drafts[2].updates[-1][1])
    drafts[2].fail_update = True
    form_submission(drafts)
    assert count(drafts[3][2], CommandRecord) == 1
    assert "Private form purpose" not in caplog.text and "fixture-form-trigger" not in caplog.text
    proof = json.loads(actions(drafts[2].updates[-1][1])[CONFIRM_ACTION]["value"])["proof"]
    assert proof not in caplog.text


@pytest.mark.parametrize("limit", [0, 101, True])
def test_inventory_query_rejects_unbounded_or_ambiguous_limits(limit):
    with pytest.raises(ValidationError):
        GpuCatalogQuery(limit=limit)
