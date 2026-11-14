import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from urllib.parse import urlencode

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aris.apps.api.main import create_app
from aris.core.audit import AuditEntry
from aris.core.db import session_factory
from aris.interfaces.slack.client import SlackClient, SlackDeliveryError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.dispatch import SlackDispatcher
from aris.interfaces.slack.models import SlackHomeJob, SlackReceipt
from aris.interfaces.slack.repo import SlackRepository
from aris.interfaces.slack.schemas import HomeJobClaim
from aris.modules.vega.service import VegaService
from tests.test_telemetry import batch, post

SIGNING_SECRET = "slack-test-signing-secret-only"


@pytest.fixture
def slack_settings():
    return SlackSettings(
        _env_file=None,
        signing_secret=SIGNING_SECRET,
        bot_token="xoxb-test-token-only",
        team_id="T111",
    )


@pytest.fixture
def slack_client(settings, slack_settings):
    with TestClient(create_app(settings, slack_settings), raise_server_exceptions=False) as client:
        yield client


def signed(body, content_type="application/json", timestamp=None):
    timestamp = str(int(time.time())) if timestamp is None else str(timestamp)
    signature = hmac.new(
        SIGNING_SECRET.encode(), b"v0:" + timestamp.encode() + b":" + body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": content_type,
        "X-Slack-Request-Timestamp": timestamp,
        "X-Slack-Signature": "v0=" + signature,
    }


def home_event(event_id="Ev111", **changes):
    return {
        "type": "event_callback",
        "team_id": "T111",
        "event_id": event_id,
        "event": {"type": "app_home_opened", "user": "U111", "tab": "home"},
        **changes,
    }


def send_event(client, payload, **header_changes):
    body = json.dumps(payload, ensure_ascii=False).encode()
    return client.post("/slack/events", content=body, headers={**signed(body), **header_changes})


def slash(client, text="status", **changes):
    body = urlencode(
        {"team_id": "T111", "user_id": "U111", "command": "/gpu", "text": text, **changes}
    ).encode()
    return client.post(
        "/slack/commands", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )


def test_slack_is_fail_closed_when_not_configured(client):
    response = send_event(client, {"type": "url_verification", "challenge": "hello"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"


def test_url_verification_accepts_original_signed_utf8_bytes(slack_client):
    response = send_event(slack_client, {"type": "url_verification", "challenge": "验证-hello"})
    assert response.status_code == 200
    assert response.json() == {"challenge": "验证-hello"}
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackReceipt)) == 0


@pytest.mark.parametrize("kind", ["missing", "wrong", "unicode", "old", "future", "tampered"])
def test_signatures_expiry_and_tampering_are_rejected(slack_client, kind):
    body = json.dumps(home_event()).encode()
    headers = signed(body)
    if kind == "missing":
        del headers["X-Slack-Signature"]
    elif kind == "wrong":
        headers["X-Slack-Signature"] = "v0=" + "0" * 64
    elif kind == "unicode":
        headers["X-Slack-Signature"] = "v0=invalid-signature"
        headers["X-Slack-Request-Timestamp"] = "not-a-timestamp"
    elif kind in {"old", "future"}:
        headers = signed(body, timestamp=int(time.time()) + (-600 if kind == "old" else 600))
    else:
        body = body.replace(b"U111", b"U222")
    response = slack_client.post("/slack/events", content=body, headers=headers)
    assert response.status_code == 401
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackReceipt)) == 0


def test_workspace_is_scoped_on_commands_and_events(slack_client):
    assert slash(slack_client, team_id="T222").status_code == 403
    assert send_event(slack_client, home_event(team_id="T222")).status_code == 403
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 0


def test_status_is_ephemeral_and_uses_fresh_observations(slack_client):
    assert post(slack_client, batch()).status_code == 200
    response = slash(slack_client)
    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"
    text = response.json()["text"]
    assert "node-a" in text and "ONLINE" in text and "观测空闲" in text
    assert "预约可用性尚未评估" in text
    assert "尚未上线" in slash(slack_client, "reserve 30").json()["text"]


def test_slack_status_does_not_present_stale_metrics_as_current(slack_client):
    assert post(slack_client, batch(seconds_ago=90)).status_code == 200
    text = slash(slack_client).json()["text"]
    assert "STALE" in text and "未知" in text
    assert "观测空闲" not in text and "显存 0/" not in text


def test_home_receipt_is_durable_idempotent_and_does_not_store_private_payload(slack_client):
    payload = home_event(token="private-deprecated-token", private_text="private-message-body")
    assert send_event(slack_client, payload).status_code == 200
    assert send_event(slack_client, dict(reversed(list(payload.items())))).status_code == 200
    changed = {**payload, "event": {**payload["event"], "user": "U222"}}
    assert send_event(slack_client, changed).status_code == 409
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackReceipt)) == 1
        assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEntry)) == 1
        receipt = session.scalars(select(SlackReceipt)).one()
        job = session.scalars(select(SlackHomeJob)).one()
        stored = repr(receipt.__dict__) + repr(job.__dict__)
        assert "private-deprecated-token" not in stored and "private-message-body" not in stored
        assert job.user_id == "U111" and job.state == "pending"


def test_interactivity_uses_same_events_url_and_only_refresh_creates_work(slack_client):
    payload = {
        "type": "block_actions",
        "team": {"id": "T111"},
        "user": {"id": "U111"},
        "actions": [{"action_id": "refresh_home"}],
        "response_url": "https://hooks.slack.com/private-value",
    }
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = signed(body, "application/x-www-form-urlencoded")
    assert slack_client.post("/slack/events", content=body, headers=headers).status_code == 200
    assert slack_client.post("/slack/events", content=body, headers=headers).status_code == 200
    ignored = home_event(
        "Ev222", event={"type": "app_home_opened", "tab": "messages", "user": "U111"}
    )
    assert send_event(slack_client, ignored).status_code == 200
    with Session(slack_client.app.state.engine) as session:
        assert session.scalar(select(func.count()).select_from(SlackReceipt)) == 2
        assert session.scalar(select(func.count()).select_from(SlackHomeJob)) == 1


def test_bad_signed_payload_and_duplicate_form_fields_do_not_leak_input(slack_client):
    body = b"payload=private-marker&payload=second"
    response = slack_client.post(
        "/slack/events", content=body, headers=signed(body, "application/x-www-form-urlencoded")
    )
    assert response.status_code == 422 and "private-marker" not in response.text
    body = b"[]"
    assert slack_client.post("/slack/events", content=body, headers=signed(body)).status_code == 422
    body = b"x" * 262145
    assert slack_client.post("/slack/events", content=body, headers=signed(body)).status_code == 413


def test_receipt_and_job_rollback_together_on_failure(slack_client, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("private-dependency-detail")

    monkeypatch.setattr(SlackRepository, "add_home_job", fail)
    response = send_event(slack_client, home_event())
    assert response.status_code == 500 and "private-dependency-detail" not in response.text
    with Session(slack_client.app.state.engine) as session:
        for model in (SlackReceipt, SlackHomeJob, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_home_ack_does_not_call_external_slack_api(slack_client, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("ingress must commit work and ACK without outbound calls")

    monkeypatch.setattr(SlackClient, "publish_home", forbidden)
    assert send_event(slack_client, home_event()).status_code == 200


def test_status_escapes_slack_mentions_in_resource_names(slack_client):
    payload = batch()
    payload["resources"]["gpu"]["gpus"][0]["model"] = "GPU <@U999> & model"
    assert post(slack_client, payload).status_code == 200
    text = slash(slack_client).json()["text"]
    assert "<@U999>" not in text and "&lt;@U999&gt; &amp;" in text


class Publisher:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = []

    def publish_home(self, user_id, view):
        self.calls.append((user_id, view))
        if self.failure:
            raise self.failure


def dispatcher(client, settings, slack_settings, publisher):
    sessions = session_factory(client.app.state.engine)
    return SlackDispatcher(sessions, slack_settings, VegaService(sessions, settings), publisher)


def test_home_job_only_completes_after_publish_receipt(slack_client, settings, slack_settings):
    assert post(slack_client, batch()).status_code == 200
    assert send_event(slack_client, home_event()).status_code == 200
    publisher = Publisher()
    worker = dispatcher(slack_client, settings, slack_settings, publisher)
    assert worker.run_once().outcome == "delivered"
    assert worker.run_once().outcome == "idle"
    assert len(publisher.calls) == 1 and publisher.calls[0][0] == "U111"
    assert "观测空闲" in publisher.calls[0][1]["blocks"][0]["text"]["text"]
    with Session(slack_client.app.state.engine) as session:
        job = session.scalars(select(SlackHomeJob)).one()
        assert job.state == "completed" and job.attempts == 1
        assert job.completed_at is not None and job.lease_token is None


def test_rate_limit_retry_and_permanent_failure_are_explicit(
    slack_client, settings, slack_settings
):
    send_event(slack_client, home_event())
    publisher = Publisher(SlackDeliveryError("rate_limited", retry_after=60))
    worker = dispatcher(slack_client, settings, slack_settings, publisher)
    assert worker.run_once().outcome == "retry"
    assert worker.run_once().outcome == "idle"
    with Session(slack_client.app.state.engine) as session:
        job = session.scalars(select(SlackHomeJob)).one()
        future = job.available_at.replace(tzinfo=UTC)
        assert future > datetime.now(UTC) + timedelta(seconds=50)
        job.available_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    publisher.failure = SlackDeliveryError("missing_scope", retryable=False)
    assert worker.run_once().outcome == "failed"
    with Session(slack_client.app.state.engine) as session:
        job = session.scalars(select(SlackHomeJob)).one()
        assert job.state == "failed" and job.attempts == 2
        assert job.completed_at is None and job.last_error_code == "missing_scope"


def test_expired_lease_is_reclaimed_and_old_owner_cannot_complete(
    slack_client, settings, slack_settings
):
    send_event(slack_client, home_event())
    worker = dispatcher(slack_client, settings, slack_settings, Publisher())
    first = worker.claim()
    assert isinstance(first, HomeJobClaim)
    with Session(slack_client.app.state.engine) as session:
        job = session.scalars(select(SlackHomeJob)).one()
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    second = worker.claim()
    assert isinstance(second, HomeJobClaim) and second.lease_token != first.lease_token
    assert worker.finish(first, None).outcome == "lease_lost"
    assert worker.finish(second, None).outcome == "delivered"


def test_crashed_last_attempt_becomes_failed_without_another_publish(
    slack_client, settings, slack_settings
):
    send_event(slack_client, home_event())
    config = slack_settings.model_copy(update={"job_max_attempts": 1})
    publisher = Publisher()
    worker = dispatcher(slack_client, settings, config, publisher)
    assert isinstance(worker.claim(), HomeJobClaim)
    with Session(slack_client.app.state.engine) as session:
        job = session.scalars(select(SlackHomeJob)).one()
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
    assert worker.run_once().outcome == "failed"
    assert publisher.calls == []


@pytest.mark.parametrize(
    "status,payload,headers,code,retryable",
    [
        (429, {}, {"retry-after": "20"}, "rate_limited", True),
        (503, {}, {}, "upstream_unavailable", True),
        (200, {"ok": False, "error": "invalid_auth"}, {}, "invalid_auth", False),
        (200, [], {}, "invalid_response", True),
    ],
)
def test_slack_api_errors_are_sanitized(slack_settings, status, payload, headers, code, retryable):
    def respond(request):
        assert request.url == "https://slack.com/api/views.publish"
        assert request.headers["authorization"] == "Bearer xoxb-test-token-only"
        return httpx.Response(status, json=payload, headers=headers)

    client = SlackClient(slack_settings, httpx.MockTransport(respond))
    with pytest.raises(SlackDeliveryError) as error:
        client.publish_home("U111", {"type": "home", "blocks": []})
    assert error.value.code == code and error.value.retryable is retryable
    assert "xoxb-test-token-only" not in str(error.value)


def test_postgres_concurrent_event_retries_create_one_job(slack_client):
    if slack_client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL row locking")
    start = Barrier(2)

    def submit(_):
        start.wait(timeout=10)
        return send_event(slack_client, home_event())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert [r.status_code for r in results] == [200, 200]
    with Session(slack_client.app.state.engine) as session:
        for model in (SlackReceipt, SlackHomeJob, AuditEntry):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_postgres_workers_do_not_claim_same_active_job(slack_client, settings, slack_settings):
    if slack_client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL SKIP LOCKED")
    send_event(slack_client, home_event())
    publisher = Publisher()
    start = Barrier(2)

    def run(_):
        start.wait(timeout=10)
        return dispatcher(slack_client, settings, slack_settings, publisher).run_once().outcome

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(run, range(2)))
    assert sorted(outcomes) == ["delivered", "idle"]
    assert len(publisher.calls) == 1
