from datetime import timedelta
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update

from aris.apps.api.main import create_app
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.commands.models import CommandIntakeReceipt, CommandJob
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.reservations import SlackReservationQueries
from tests.test_m2_composition import config
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import counts, create
from tests.test_slack import signed, slash
from tests.test_slack import slack_settings as slack_settings
from tests.test_user_commands import LIMITS

NAMESPACE = IdentityNamespace(provider="slack", namespace="T111")


@pytest.fixture
def slack_mine(booking, settings, slack_settings):
    service, _, sessions, ids, _, _ = booking
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
    queries = PersonalReservationQueries(sessions, service, NAMESPACE, LIMITS)
    adapter = SlackReservationQueries(queries)
    with TestClient(
        create_app(settings, slack_settings, slack_reservations=adapter),
        raise_server_exceptions=False,
    ) as client:
        yield client, queries, booking, adapter


def intake_counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (CommandIntakeReceipt, CommandJob, SlackReceipt, SlackHomeJob)
        )


def test_signed_mine_is_ephemeral_owner_only_and_needs_no_development_token(slack_mine):
    client, queries, state, _ = slack_mine
    alice = create(state, purpose="Alice only").result["reservation"]
    bob = create(
        state,
        user="bob",
        key="other-owner",
        purpose="Bob only",
        start=state[4]["now"] + timedelta(hours=2),
    ).result["reservation"]
    before, queues = counts(state), intake_counts(state[2])
    response = slash(client, "mine")
    assert response.status_code == 200 and response.json()["response_type"] == "ephemeral"
    assert alice["id"] in response.text and "Alice only" in response.text
    assert bob["id"] not in response.text and "Bob only" not in response.text
    assert "<!date^" in response.text and "UTC" in response.text
    assert response.headers["Cache-Control"] == "no-store"
    repeated = slash(client, "mine")
    assert repeated.json() == response.json()
    other = slash(client, "mine", user_id="U222")
    assert bob["id"] in other.text and alice["id"] not in other.text
    assert counts(state) == before and intake_counts(state[2]) == queues


def test_no_reservations_is_distinct_from_unknown_or_unpermitted_user(slack_mine):
    client, _, state, _ = slack_mine
    empty = slash(client, "mine")
    assert empty.status_code == 200 and empty.json()["text"] == "You have no reservations."
    unknown = slash(client, "mine", user_id="U999")
    assert unknown.status_code == 200 and "not authorized" in unknown.json()["text"]
    with state[2].begin() as session:
        session.execute(
            delete(RoleBinding).where(
                RoleBinding.user_id == state[3]["alice"], RoleBinding.role_id == state[3]["reader"]
            )
        )
    missing_grant = slash(client, "mine")
    assert "not authorized" in missing_grant.json()["text"]
    assert "no reservations" not in missing_grant.text


@pytest.mark.parametrize("change", ["mapping", "suspended"])
def test_signed_mine_rechecks_current_identity_each_time(slack_mine, change):
    client, _, state, _ = slack_mine
    create(state)
    assert "Private fixture purpose" in slash(client, "mine").text
    with state[2].begin() as session:
        if change == "mapping":
            session.execute(delete(ExternalIdentity).where(ExternalIdentity.external_id == "U111"))
        else:
            session.execute(
                update(User).where(User.id == state[3]["alice"]).values(status="suspended")
            )
    response = slash(client, "mine")
    assert response.status_code == 200 and "not authorized" in response.text
    assert "Private fixture purpose" not in response.text


def test_purposes_are_plain_text_and_never_reach_fallback_or_markdown(slack_mine):
    client, _, state, _ = slack_mine
    hostile = "<!channel> <@U222> *private* & <https://example.invalid|click>"
    create(state, purpose=hostile)
    data = slash(client, "mine").json()
    assert hostile not in data["text"]
    plain, markdown = [], []
    for block in data["blocks"]:
        elements = [block["text"]] if "text" in block else block.get("elements", [])
        for item in elements:
            (plain if item["type"] == "plain_text" else markdown).append(item["text"])
    assert any(hostile in value for value in plain)
    assert all(hostile not in value for value in markdown)


def test_more_than_five_is_explicitly_truncated_and_other_form_owners_are_ignored(slack_mine):
    client, _, state, _ = slack_mine
    for index in range(6):
        create(state, key=f"many-{index}", start=state[4]["now"] + timedelta(hours=index + 1))
    response = slash(client, "mine", owner_id=state[3]["bob"], actor_id=state[3]["bob"])
    assert response.status_code == 200
    assert response.text.count("Reservation: ") == 5
    assert "five most recently created" in response.text
    malformed = slash(client, "mine U222")
    assert "Private fixture purpose" not in malformed.text


@pytest.mark.parametrize(
    "case", ["bad_signature", "wrong_workspace", "duplicate_form", "invalid_user"]
)
def test_mine_requires_valid_unambiguous_signed_slash_context(slack_mine, case):
    client, _, state, _ = slack_mine
    create(state)
    data = {
        "team_id": "T222" if case == "wrong_workspace" else "T111",
        "user_id": "invalid user" if case == "invalid_user" else "U111",
        "command": "/gpu",
        "text": "mine",
    }
    body = urlencode(data).encode()
    if case == "duplicate_form":
        body += b"&user_id=U222"
    headers = signed(body, "application/x-www-form-urlencoded")
    if case == "bad_signature":
        headers["X-Slack-Signature"] = "v0=" + "0" * 64
    response = client.post("/slack/commands", content=body, headers=headers)
    assert (
        response.status_code
        == {
            "bad_signature": 401,
            "wrong_workspace": 403,
            "duplicate_form": 422,
            "invalid_user": 422,
        }[case]
    )
    assert "Private fixture purpose" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_default_signed_mine_reports_disabled_instead_of_fake_empty_list(settings, slack_settings):
    with TestClient(create_app(settings, slack_settings)) as client:
        response = slash(client, "mine")
        assert response.status_code == 503 and "not enabled" in response.text


def test_slack_mine_is_composed_without_http_user_bindings(slack_mine, settings, slack_settings):
    _, _, state, _ = slack_mine
    create(state)
    cfg = config(namespace=NAMESPACE, slack_callbacks=True, user_tokens=())
    with TestClient(create_app(settings, slack_settings, m2_config=cfg)) as client:
        assert client.app.state.m2.user_api is None
        assert client.app.state.m2.user_reservations is None
        assert client.app.state.m2.slack_reservations is not None
        assert client.get("/internal/reservations/mine").status_code == 404
        response = slash(client, "mine")
        assert response.status_code == 200 and "Private fixture purpose" in response.text


def test_slack_query_refuses_inconsistent_workspace_configuration(
    slack_mine, settings, slack_settings
):
    _, _, _, adapter = slack_mine
    with pytest.raises(ValueError, match="workspace"):
        create_app(
            settings,
            slack_settings.model_copy(update={"team_id": "T_OTHER"}),
            slack_reservations=adapter,
        )


def test_slash_mine_never_queries_gpu_or_publishes_views(slack_mine, monkeypatch):
    client, queries, state, _ = slack_mine
    create(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only mine must not query hardware or publish")

    monkeypatch.setattr(queries.reservations.availability.targets, "gpu_in_transaction", forbidden)
    from aris.interfaces.slack.client import SlackClient

    monkeypatch.setattr(SlackClient, "open_modal", forbidden)
    monkeypatch.setattr(SlackClient, "update_modal", forbidden)
    monkeypatch.setattr(SlackClient, "publish_home", forbidden)
    assert slash(client, "mine").status_code == 200


def test_slack_query_read_only_database_contract(slack_mine):
    client, _, state, _ = slack_mine
    create(state)
    engine = state[5]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL READ ONLY contract")

    def read_only(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(engine, "begin", read_only)
    try:
        response = slash(client, "mine")
    finally:
        event.remove(engine, "begin", read_only)
    assert response.status_code == 200 and "Private fixture purpose" in response.text


def test_slack_query_budget_failure_is_not_reported_as_empty(slack_mine, monkeypatch):
    client, queries, state, _ = slack_mine
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL lookup budget")

    def slow_lookup(session, subject):
        session.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("budget did not expire")

    monkeypatch.setattr(queries.access, "resolve_external_in_transaction", slow_lookup)
    response = slash(client, "mine")
    assert response.status_code == 200 and "temporarily unavailable" in response.text
    assert "no reservations" not in response.text
