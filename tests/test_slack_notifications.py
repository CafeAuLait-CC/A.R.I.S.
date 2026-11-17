import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from aris.core.notifications.schemas import (
    NoticeContent,
    NoticeField,
    NotificationDeliveryError,
    PreparedNotification,
)
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.notifications import SlackNotificationTransport


def prepared():
    return PreparedNotification(
        notification_id=uuid4(),
        attempt_id=uuid4(),
        target={"provider": "slack", "namespace": "TFIXTURE", "external_id": "UFIXTURE"},
        content=NoticeContent(
            title="Reservation updated",
            summary="Literal <!channel> and <@UOTHER>",
            fields=(NoticeField(label="GPU", value="Fixture GPU 0"),),
        ),
    )


def config(**changes):
    return SlackSettings(
        _env_file=None,
        signing_secret=SecretStr(""),
        bot_token=SecretStr("fixture-only-bot-token"),
        team_id="TFIXTURE",
        **changes,
    )


def test_slack_notification_uses_only_fixed_user_target_plain_text_and_real_receipt():
    notice = prepared()
    calls = []

    def send(request):
        body = json.loads(request.content)
        calls.append(body)
        assert str(request.url) == "https://slack.com/api/chat.postMessage"
        assert request.headers["Authorization"] == "Bearer fixture-only-bot-token"
        assert body["channel"] == "UFIXTURE"
        assert body["mrkdwn"] is body["link_names"] is body["unfurl_links"] is False
        assert body["parse"] == "none" and body["unfurl_media"] is False
        assert body["text"] == notice.content.fallback()
        assert all(
            block.get("text", {}).get("type", "plain_text") == "plain_text"
            for block in body["blocks"]
        )
        assert body["metadata"]["event_payload"] == {"notification_id": str(notice.notification_id)}
        assert "client_msg_id" not in body and "username" not in body
        return httpx.Response(
            200, json={"ok": True, "channel": "DFIXTURE", "ts": "1731800000.000001"}
        )

    receipt = SlackNotificationTransport(config(), transport=httpx.MockTransport(send)).send(notice)
    assert receipt.channel_ref == "DFIXTURE" and receipt.message_ref == "1731800000.000001"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response,code,retry,unknown",
    [
        (httpx.Response(429, headers={"Retry-After": "12"}), "SLACK_RATE_LIMITED", True, False),
        (httpx.Response(500), "SLACK_UPSTREAM_UNCERTAIN", False, True),
        (httpx.Response(401), "SLACK_HTTP_REJECTED", False, False),
        (
            httpx.Response(302, headers={"Location": "https://unsafe.invalid"}),
            "SLACK_HTTP_REJECTED",
            False,
            False,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "missing_scope"}),
            "SLACK_MISSING_SCOPE",
            False,
            False,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "internal_error"}),
            "SLACK_RESPONSE_UNCERTAIN",
            False,
            True,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": ["invalid"]}),
            "SLACK_RESPONSE_UNCERTAIN",
            False,
            True,
        ),
        (
            httpx.Response(200, json={"ok": False, "error": "ratelimited"}),
            "SLACK_RATE_LIMITED",
            True,
            False,
        ),
        (httpx.Response(200, json={"ok": True}), "SLACK_RECEIPT_UNCERTAIN", False, True),
        (
            httpx.Response(200, json={"ok": True, "channel": "CPUBLIC", "ts": "1.0001"}),
            "SLACK_RECEIPT_UNCERTAIN",
            False,
            True,
        ),
        (
            httpx.Response(200, json={"ok": True, "channel": "DFIXTURE", "ts": "bad"}),
            "SLACK_RECEIPT_UNCERTAIN",
            False,
            True,
        ),
        (httpx.Response(200, text="private-invalid-html"), "SLACK_RESPONSE_UNCERTAIN", False, True),
        (httpx.Response(200, json=[]), "SLACK_RESPONSE_UNCERTAIN", False, True),
        (httpx.Response(200, content=b"x" * 65537), "SLACK_RESPONSE_UNCERTAIN", False, True),
    ],
)
def test_slack_notification_classifies_known_rejection_separately_from_uncertainty(
    response, code, retry, unknown
):
    with pytest.raises(NotificationDeliveryError) as error:
        SlackNotificationTransport(
            config(), transport=httpx.MockTransport(lambda request: response)
        ).send(prepared())
    assert (error.value.code, error.value.retryable, error.value.outcome_unknown) == (
        code,
        retry,
        unknown,
    )
    assert "private-invalid" not in str(error.value) and "fixture-only-bot-token" not in repr(
        error.value
    )
    if response.status_code == 429:
        assert error.value.retry_after == 12


@pytest.mark.parametrize(
    "exception,retry,unknown",
    [
        (httpx.ConnectError("private-connect-detail"), True, False),
        (httpx.ConnectTimeout("private-connect-detail"), True, False),
        (httpx.ReadTimeout("private-read-detail"), False, True),
        (httpx.WriteTimeout("private-write-detail"), False, True),
    ],
)
def test_transport_timeout_does_not_claim_a_message_was_definitely_not_sent(
    exception, retry, unknown
):
    def fail(request):
        raise exception

    with pytest.raises(NotificationDeliveryError) as error:
        SlackNotificationTransport(config(), transport=httpx.MockTransport(fail)).send(prepared())
    assert error.value.retryable is retry and error.value.outcome_unknown is unknown
    assert "private-" not in str(error.value)


@pytest.mark.parametrize(
    "target",
    [
        {"provider": "slack", "namespace": "T_OTHER", "external_id": "UFIXTURE"},
        {"provider": "slack", "namespace": "TFIXTURE", "external_id": "CPUBLIC"},
        {"provider": "slack", "namespace": "TFIXTURE", "external_id": "#general"},
        {"provider": "other", "namespace": "TFIXTURE", "external_id": "UFIXTURE"},
    ],
)
def test_arbitrary_workspace_channel_or_provider_cannot_be_used_as_notification_target(target):
    called = []
    notice = prepared().model_copy(update={"target": prepared().target.model_validate(target)})
    with pytest.raises(NotificationDeliveryError) as error:
        SlackNotificationTransport(
            config(), transport=httpx.MockTransport(lambda request: called.append(request))
        ).send(notice)
    assert error.value.code == "SLACK_RECIPIENT_INVALID" and not called
