from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, select, text
from sqlalchemy.orm import sessionmaker

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from aris.core.notifications.commands import RetryUncertainNotificationCommand
from aris.core.notifications.models import NotificationAttempt, NotificationOutbox
from aris.core.notifications.queries import NotificationOperatorQueries
from aris.core.notifications.query_schemas import NotificationQueryLimits
from aris.core.notifications.repo import NotificationRepository
from aris.core.notifications.schemas import NotificationDeliveryError
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.commands.service import UserCommandSurface
from aris.interfaces.notifications.service import NotificationAdminSurface
from aris.interfaces.slack.config import SlackSettings
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_command_intake import KEY, NAMESPACE, WORKER_LIMITS, subject
from tests.test_command_intake import LIMITS as INTAKE_LIMITS
from tests.test_m2_composition import config
from tests.test_notification_administration import all_rows, operation, uncertain
from tests.test_notification_administration import notification_admin as notification_admin
from tests.test_notifications import LIMITS, RECEIPT
from tests.test_notifications import command_state as command_state
from tests.test_notifications import durable_state as durable_state
from tests.test_notifications import notification_state as notification_state
from tests.test_notifications import relay_state as relay_state
from tests.test_user_commands import LIMITS as GATEWAY_LIMITS

ADMIN_TOKEN = "fixture-notification-admin-credential-123456789"
MEMBER_TOKEN = "fixture-notification-member-credential-123456789"
QUERY = "/internal/notifications/uncertain/query"


def headers(token=ADMIN_TOKEN, key="fixture-notification-review"):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key}


@pytest.fixture
def notification_api(notification_admin, settings):
    admin, state, _ = notification_admin
    service, _, sessions, _, _, _, clock, _ = state
    commands = CommandService(
        sessions,
        [RetryUncertainNotificationCommand(admin)],
        confirmation_key=KEY,
        clock=lambda: clock["now"],
    )
    intake = CommandIntakeService(
        commands, NAMESPACE, INTAKE_LIMITS, confirmation_key_id="fixture-key"
    )
    gateway = UserCommandGateway(intake, ("notification.retry_uncertain",), GATEWAY_LIMITS)
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(subject=subject("bob"), token=SecretStr(ADMIN_TOKEN)),
            UserTokenBinding(subject=subject("alice"), token=SecretStr(MEMBER_TOKEN)),
        ],
        reserved_tokens=[settings.read_token],
    )
    users = UserCommandSurface(gateway, auth)
    surface = NotificationAdminSurface(
        users, NotificationOperatorQueries(service, LIMITS, NotificationQueryLimits())
    )
    slack = SlackSettings(
        _env_file=None,
        bot_token="fixture-notification-bot",
        signing_secret="fixture-notification-signing",
        team_id=NAMESPACE.namespace,
    )
    with TestClient(
        create_app(settings, slack, user_commands=users, notification_admin=surface),
        raise_server_exceptions=False,
    ) as client:
        yield client, surface, CommandWorker(intake, WORKER_LIMITS), notification_admin


def test_operator_ui_is_default_off_and_static_assets_contain_no_credentials(
    notification_api, client
):
    assert client.get("/internal/notifications/ui").status_code == 404
    app = notification_api[0]
    for path in ("/ui", "/ui/assets/admin.css", "/ui/assets/admin.js"):
        response = app.get("/internal/notifications" + path)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert ADMIN_TOKEN not in response.text and MEMBER_TOKEN not in response.text
    script = app.get("/internal/notifications/ui/assets/admin.js").text
    assert all(
        value not in script for value in ("localStorage", "sessionStorage", ".innerHTML", "eval(")
    )


@pytest.mark.parametrize("token", [None, READ_TOKEN, AGENT_TOKEN, MEMBER_TOKEN])
def test_queries_require_current_global_notification_permission_and_user_credentials(
    notification_api, token
):
    client, _, _, (_, state, _) = notification_api
    uncertain(state)
    before = all_rows(state[2])
    response = client.post(QUERY, json={}, headers={} if token is None else headers(token))
    assert response.status_code == (403 if token == MEMBER_TOKEN else 401)
    assert response.headers["Cache-Control"] == "no-store"
    assert "items" not in response.json() and all_rows(state[2]) == before


def test_uncertain_pages_are_bounded_stable_and_read_only(notification_api):
    client, _, _, (_, state, _) = notification_api
    ids = [str(uncertain(state)[0].notification_id) for _ in range(3)]
    before = all_rows(state[2])
    after, found = None, []
    for _ in range(3):
        response = client.post(QUERY, json={"limit": 1, "after": after}, headers=headers())
        assert response.status_code == 200
        page = response.json()
        assert len(page["items"]) == 1
        item = page["items"][0]
        found.append(item["notification_id"])
        assert item["state"] == "uncertain" and item["retry"]["allowed"]
        assert len(item["attempts"]) == item["total_attempts"] == 1
        assert item["target"]["external_id"] == subject("alice").external_id
        after = page["next_cursor"]
    assert after is None and found == sorted(ids)
    assert all_rows(state[2]) == before and not state[5].sent


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 0},
        {"limit": 21},
        {"limit": True},
        {"actor_id": "injected"},
        {"namespace": "TOTHER"},
        {"recipient_user_id": str(uuid4())},
        {"state": "delivered"},
        {"after": {"created_at": "2026-11-17T00:00:00", "notification_id": str(uuid4())}},
    ],
)
def test_operator_query_rejects_context_overrides_and_invalid_cursor(notification_api, payload):
    response = notification_api[0].post(QUERY, json=payload, headers=headers())
    assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("change", ["namespace", "provider", "template"])
def test_foreign_or_unregistered_notification_ids_are_indistinguishable_from_missing(
    notification_api, change
):
    client, _, _, (_, state, _) = notification_api
    handle, _, _ = uncertain(state)
    with state[2].begin() as session:
        record = session.get(NotificationOutbox, str(handle.notification_id))
        setattr(
            record,
            {"namespace": "namespace", "provider": "provider", "template": "template_id"}[change],
            "other",
        )
    assert client.post(QUERY, json={}, headers=headers()).json()["items"] == []
    for identifier in (handle.notification_id, uuid4()):
        response = client.get("/internal/notifications/" + str(identifier), headers=headers())
        assert response.status_code == 404 and response.json()["error"]["code"] == "NOT_FOUND"


def test_all_attempts_expose_earlier_success_without_claiming_latest_attempt_succeeded(
    notification_api,
):
    client, _, _, (admin, state, _) = notification_api
    handle, earlier, request = uncertain(state)
    admin.retry(operation(state), request)
    later = state[1].claim()
    state[1].prepare(later)
    state[1].finish(later, error=NotificationDeliveryError("FIXTURE_UNKNOWN", outcome_unknown=True))
    state[1].finish(earlier, receipt=RECEIPT)
    before = all_rows(state[2])
    response = client.get(
        "/internal/notifications/" + str(handle.notification_id), headers=headers()
    )
    assert response.status_code == 200
    item = response.json()
    assert item["state"] == "uncertain" and item["known_successful_attempts"] == 1
    assert [attempt["state"] for attempt in item["attempts"]] == ["late_delivered", "uncertain"]
    assert item["retry"] == {"allowed": False, "code": "ALREADY_DELIVERED"}
    assert item["attempts"][0]["message_ref"] == RECEIPT.message_ref
    assert item["attempts"][1]["message_ref"] is None and all_rows(state[2]) == before


@pytest.mark.parametrize("change", ["revoked", "remapped"])
def test_operator_authority_is_rechecked_after_reading_evidence(
    notification_api, monkeypatch, change
):
    client, surface, _, (_, state, binding) = notification_api
    uncertain(state)
    original = surface.queries._views

    def changed(session, records):
        result = original(session, records)
        if change == "revoked":
            session.execute(delete(RoleBinding).where(RoleBinding.id == binding))
        else:
            row = session.scalar(
                select(ExternalIdentity).where(
                    ExternalIdentity.external_id == subject("bob").external_id
                )
            )
            row.user_id = state[3]["alice"]
            session.flush()
        return result

    monkeypatch.setattr(surface.queries, "_views", changed)
    response = client.post(QUERY, json={}, headers=headers())
    assert response.status_code == 403 and "items" not in response.json()


@pytest.mark.parametrize("change", ["content", "missing_attempt"])
def test_damaged_evidence_never_displays_unverified_text_or_a_fake_empty_result(
    notification_api, change
):
    client, _, _, (_, state, _) = notification_api
    handle, claim, _ = uncertain(state)
    with state[2].begin() as session:
        if change == "content":
            session.get(NotificationOutbox, str(handle.notification_id)).content = {
                "title": "Unverified secret",
                "summary": "Do not display",
            }
        else:
            session.delete(session.get(NotificationAttempt, str(claim.attempt_id)))
    response = client.post(QUERY, json={}, headers=headers())
    if change == "content":
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["content"] is None and item["retry"] == {
            "allowed": False,
            "code": "NOTIFICATION_CHANGED",
        }
        assert "Unverified secret" not in response.text
    else:
        assert response.status_code == 503 and "items" not in response.json()


def test_http_review_confirmation_and_worker_approve_one_retry_without_sending_in_request(
    notification_api,
):
    client, _, worker, (admin, state, _) = notification_api
    handle, _, request = uncertain(state)
    body = {
        "command": "notification.retry_uncertain",
        "parameters": request.model_dump(mode="json"),
    }
    submitted = client.post("/internal/commands", json=body, headers=headers())
    assert submitted.status_code == 202
    pending = submitted.json()["outcome"]
    review = client.get(
        "/internal/commands/" + pending["command_id"] + "/review", headers=headers()
    )
    assert (
        review.status_code == 200 and review.json()["request"]["parameters"] == body["parameters"]
    )
    assert state[1].run_once() is None
    response = client.post(
        "/internal/commands/" + pending["command_id"] + "/confirm",
        json={"proof": pending["proof"]},
        headers=headers(),
    )
    assert response.status_code == 202 and not state[5].sent
    assert worker.run_once().state == "succeeded"
    detail = client.get("/internal/notifications/" + str(handle.notification_id), headers=headers())
    assert detail.json()["state"] == "queued" and not state[5].sent
    assert state[1].run_once().state == "delivered"
    detail = client.get(
        "/internal/notifications/" + str(handle.notification_id), headers=headers()
    ).json()
    assert detail["state"] == "delivered" and detail["known_successful_attempts"] == 1
    replay = client.post("/internal/commands", json=body, headers=headers())
    assert (
        replay.status_code == 200
        and replay.json()["outcome"]["command_id"] == pending["command_id"]
    )
    assert state[1].run_once() is None and len(state[5].sent) == 1


def test_postgres_operator_queries_are_read_only_and_bounded(notification_api, monkeypatch):
    client, surface, _, (_, state, _) = notification_api
    if state[2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL readonly notification query and statement budget")
    uncertain(state)
    readonly = sessionmaker(bind=state[2].kw["bind"].execution_options(postgresql_readonly=True))
    monkeypatch.setattr(surface.queries, "sessions", readonly)
    assert client.post(QUERY, json={}, headers=headers()).status_code == 200

    def slow(repo, *args, **kwargs):
        repo.session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(NotificationRepository, "uncertain_page", slow)
    response = client.post(QUERY, json={}, headers=headers())
    assert (
        response.status_code == 503
        and response.json()["error"]["code"] == "NOTIFICATION_QUERY_UNAVAILABLE"
    )


@pytest.mark.parametrize("change", [{"user_tokens": ()}, {"commands": ("identity.map_users",)}])
def test_operator_ui_requires_explicit_authenticated_retry_registration(change):
    values = dict(
        commands=("notification.retry_uncertain",),
        rules=None,
        maintenance=(),
        notification_admin=True,
        user_tokens=(UserTokenBinding(subject=subject("bob"), token=SecretStr(ADMIN_TOKEN)),),
    )
    with pytest.raises(ValidationError):
        config(**{**values, **change})
