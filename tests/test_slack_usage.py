from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update

from aris.apps.api.main import create_app
from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.commands.models import CommandIntakeReceipt, CommandJob
from aris.core.contracts import DomainError
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.usage import SlackUsageQueries, recorded_duration
from aris.interfaces.usage.personal import PersonalUsageQueries
from aris.modules.lyra.models import UsageLedgerEntryRow
from tests.test_administration import admin_state
from tests.test_m2_composition import config
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_slack import signed, slack_settings, slash
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import counts, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings", "slack_settings"]
NAMESPACE = IdentityNamespace(provider="slack", namespace="TFIXTURE")
CONTEXT = ExternalCommandActor(
    subject={"provider": "slack", "namespace": "TFIXTURE", "external_id": "UFIRST"},
    request_id="fixture-slack-usage",
)


@pytest.fixture
def usage_slack(readings, settings, slack_settings, monkeypatch):
    state, (original, actor, other, ids), _ = readings
    queries = PersonalUsageQueries(
        original.sessions, NAMESPACE, ("node-a", "node-b"), clock=original.clock
    )
    adapter = SlackUsageQueries(queries)
    slack = slack_settings.model_copy(update={"team_id": "TFIXTURE"})

    def no_external_send(*args, **kwargs):
        raise AssertionError("usage query attempted outbound HTTP")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_external_send)
    with TestClient(create_app(settings, slack, slack_usage=adapter)) as client:
        yield client, queries, readings, slack


def request(client, operation="usage node-a week", **changes):
    return slash(client, operation, team_id="TFIXTURE", user_id="UFIRST", **changes)


def queue_counts(queries):
    with queries.sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (CommandIntakeReceipt, CommandJob, SlackReceipt, SlackHomeJob)
        )


def test_signed_usage_is_current_subject_only_ephemeral_and_never_writes_or_sends(
    usage_slack, monkeypatch
):
    client, queries, readings, _ = usage_slack
    state, (_, actor, other, _), _ = readings
    before, queues = counts(state), queue_counts(queries)
    help = request(client, "usage")
    assert help.status_code == 200 and "node-a" in help.text and "node-b" not in help.text
    seen = []
    original = queries.usage.mine_in_transaction

    def observe(session, context, query):
        seen.append(context.actor_id)
        return original(session, context, query)

    monkeypatch.setattr(queries.usage, "mine_in_transaction", observe)
    response = request(client, owner_id=str(other), actor_id=str(other), node_id="node-b")
    assert response.status_code == 200 and response.json()["response_type"] == "ephemeral"
    assert seen == [actor.actor_id] and "30 GPU seconds recorded" in response.text
    assert (
        "Source coverage is unknown" in response.text
        and "remaining quota are unavailable" in response.text
    )
    assert str(other) not in response.text and "no-store" in response.headers["cache-control"]
    assert counts(state) == before and queue_counts(queries) == queues
    invalid = request(client, "usage node-a month")
    assert "Use /gpu usage" in invalid.text and len(seen) == 1


def test_usage_empty_is_distinct_from_unknown_user_revocation_or_unauthorized_node(usage_slack):
    client, queries, readings, _ = usage_slack
    _, (_, actor, _, ids), _ = readings
    assert "not authorized" in request(client, "usage node-b").text
    with queries.sessions.begin() as session:
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(actor.actor_id),
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-b",
            )
        )
    empty = request(client, "usage node-b today")
    assert (
        "No entries have been recorded" in empty.text
        and "Actual usage remains unknown" in empty.text
    )
    with queries.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
    denied = request(client)
    assert "not authorized" in denied.text and "0 GPU seconds" not in denied.text
    with queries.sessions.begin() as session:
        session.execute(delete(ExternalIdentity).where(ExternalIdentity.external_id == "UFIRST"))
    assert "not authorized" in request(client, "usage").text


def test_usage_signature_and_workspace_validation_precede_query_and_default_is_disabled(
    usage_slack, settings, slack_settings, monkeypatch
):
    client, queries, _, _ = usage_slack

    def forbidden(*args, **kwargs):
        raise AssertionError("untrusted Slack body reached usage adapter")

    monkeypatch.setattr(queries, "mine", forbidden)
    cases = []
    for changes, status in (({"team_id": "OTHER"}, 403), ({"user_id": "bad user"}, 422)):
        body = urlencode(
            {
                "team_id": "TFIXTURE",
                "user_id": "UFIRST",
                "command": "/gpu",
                "text": "usage node-a",
                **changes,
            }
        ).encode()
        cases.append((body, signed(body, "application/x-www-form-urlencoded"), status))
    body = urlencode(
        {"team_id": "TFIXTURE", "user_id": "UFIRST", "command": "/gpu", "text": "usage node-a"}
    ).encode()
    bad = signed(body, "application/x-www-form-urlencoded")
    bad["X-Slack-Signature"] = "v0=" + "0" * 64
    cases.append((body, bad, 401))
    duplicate = body + b"&user_id=USECOND"
    cases.append((duplicate, signed(duplicate, "application/x-www-form-urlencoded"), 422))
    for body, headers, status in cases:
        assert client.post("/slack/commands", content=body, headers=headers).status_code == status
    with TestClient(create_app(settings, slack_settings)) as default:
        response = slash(default, "usage node-a")
        assert response.status_code == 503 and "not enabled" in response.text


def test_usage_reporting_window_uses_user_timezone_and_rechecks_identity(usage_slack, monkeypatch):
    _, queries, readings, _ = usage_slack
    _, (_, actor, _, ids), _ = readings
    with queries.sessions.begin() as session:
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(actor.actor_id),
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-b",
            )
        )
        session.execute(
            update(User).where(User.id == str(actor.actor_id)).values(timezone="America/New_York")
        )
    clock = {"now": datetime(2026, 3, 8, 17, tzinfo=UTC)}
    native = PersonalUsageQueries(
        queries.sessions, NAMESPACE, ("node-b",), clock=lambda: clock["now"]
    )
    result = native.mine(CONTEXT, "node-b", "today")
    assert (
        result.start_at == datetime(2026, 3, 8, 5, tzinfo=UTC)
        and (result.end_at - result.start_at).total_seconds() == 12 * 3600
    )
    assert result.total_usage_microseconds is None and not result.quota_usage_available
    clock["now"] = datetime(2026, 3, 9, 4, tzinfo=UTC)
    with pytest.raises(DomainError) as error:
        native.mine(CONTEXT, "node-b", "week")
    assert error.value.code == "WINDOW_NOT_STARTED"
    resolve = native.access.resolve_external_in_transaction
    calls = 0

    def revoke(session, subject):
        nonlocal calls
        calls += 1
        return resolve(session, subject) if calls == 1 else None

    monkeypatch.setattr(native.access, "resolve_external_in_transaction", revoke)
    with pytest.raises(DomainError) as error:
        native.choices(CONTEXT)
    assert error.value.code == "PERMISSION_DENIED"
    assert recorded_duration(1) == "0.000001 GPU seconds"


def test_usage_composes_without_http_tokens_or_reservation_callbacks(usage_slack, settings):
    _, _, _, slack = usage_slack
    cfg = config(
        namespace=NAMESPACE,
        commands=("reservation.cancel",),
        rules=None,
        maintenance=(),
        slack_callbacks=False,
        user_tokens=(),
        slack_usage_nodes=("node-a",),
    )
    with TestClient(create_app(settings, slack, m2_config=cfg)) as client:
        assert client.app.state.m2.user_api is None and client.app.state.m2.slack_usage is not None
        assert "node-a" in request(client, "usage").text
        assert client.get("/internal/usage/ui").status_code == 404


def test_postgresql_personal_usage_is_read_only_and_recovers_after_ledger_lock(
    usage_slack, monkeypatch
):
    client, queries, readings, _ = usage_slack
    engine = queries.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only and real lock wait")
    before = counts(readings[0])
    original = queries.usage.mine_in_transaction

    def checked(session, actor, query):
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        return original(session, actor, query)

    monkeypatch.setattr(queries.usage, "mine_in_transaction", checked)
    assert "30 GPU seconds recorded" in request(client).text
    with queries.sessions() as blocker:
        blocker.execute(
            text("LOCK TABLE " + UsageLedgerEntryRow.__tablename__ + " IN ACCESS EXCLUSIVE MODE")
        )
        failed = request(client)
        assert "temporarily unavailable" in failed.text and "0 GPU seconds" not in failed.text
        blocker.rollback()
    assert "30 GPU seconds recorded" in request(client).text and counts(readings[0]) == before
