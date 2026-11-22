"""Explicitly constructed notification transport; never enabled by Home settings."""

import json
import re
import time
from typing import Any

import httpx

from aris.core.notifications.schemas import (
    NoticeContent,
    NotificationDeliveryError,
    PreparedNotification,
    TransportReceipt,
)
from aris.interfaces.slack.config import SlackSettings


def notice_blocks(content: NoticeContent) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": content.title, "emoji": False}},
        {
            "type": "section",
            "text": {"type": "plain_text", "text": content.summary, "emoji": False},
        },
    ]
    if content.fields:
        blocks.append(
            {
                "type": "section",
                "fields": [
                    {"type": "plain_text", "text": f"{item.label}\n{item.value}", "emoji": False}
                    for item in content.fields
                ],
            }
        )
    return blocks


def message_body(notification: PreparedNotification) -> dict[str, Any]:
    content = notification.content
    return {
        "channel": notification.target.external_id,
        "text": content.fallback(),
        "blocks": notice_blocks(content),
        "mrkdwn": False,
        "parse": "none",
        "link_names": False,
        "unfurl_links": False,
        "unfurl_media": False,
        "metadata": {
            "event_type": "aris_notification",
            "event_payload": {
                "notification_id": str(notification.notification_id),
            },
        },
    }


class SlackNotificationTransport:
    def __init__(
        self, config: SlackSettings, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.config, self.transport = config, transport

    def send(self, notification: PreparedNotification) -> TransportReceipt:
        if not self.config.bot_token.get_secret_value() or not self.config.team_id:
            raise NotificationDeliveryError("SLACK_UNCONFIGURED")
        if (
            notification.target.provider != "slack"
            or notification.target.namespace != self.config.team_id
            or not re.fullmatch(r"[UW][A-Z0-9]{1,127}", notification.target.external_id)
        ):
            raise NotificationDeliveryError("SLACK_RECIPIENT_INVALID")
        started = time.monotonic()
        try:
            with httpx.Client(
                timeout=httpx.Timeout(2.0, connect=1.0, write=2.0, pool=0.1),
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    "https://slack.com/api/chat.postMessage",
                    headers={"Authorization": "Bearer " + self.config.bot_token.get_secret_value()},
                    json=message_body(notification),
                ) as response:
                    if response.status_code == 429:
                        delay = response.headers.get("retry-after", "1")
                        seconds = int(delay) if re.fullmatch(r"[0-9]{1,6}", delay) else 1
                        raise NotificationDeliveryError(
                            "SLACK_RATE_LIMITED", retryable=True, retry_after=seconds
                        )
                    if response.status_code >= 500:
                        raise NotificationDeliveryError(
                            "SLACK_UPSTREAM_UNCERTAIN", outcome_unknown=True
                        )
                    if response.status_code != 200:
                        raise NotificationDeliveryError("SLACK_HTTP_REJECTED")
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 65536 or time.monotonic() - started > 10:
                            raise NotificationDeliveryError(
                                "SLACK_RESPONSE_UNCERTAIN", outcome_unknown=True
                            )
                    payload = json.loads(data)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            raise NotificationDeliveryError("SLACK_CONNECTION_FAILED", retryable=True) from None
        except httpx.HTTPError:
            raise NotificationDeliveryError(
                "SLACK_TRANSPORT_UNCERTAIN", outcome_unknown=True
            ) from None
        except (ValueError, TypeError):
            raise NotificationDeliveryError(
                "SLACK_RESPONSE_UNCERTAIN", outcome_unknown=True
            ) from None
        if not isinstance(payload, dict):
            raise NotificationDeliveryError("SLACK_RESPONSE_UNCERTAIN", outcome_unknown=True)
        if payload.get("ok") is not True:
            code = payload.get("error")
            if isinstance(code, str) and code in {"ratelimited", "rate_limited"}:
                raise NotificationDeliveryError("SLACK_RATE_LIMITED", retryable=True, retry_after=1)
            permanent = {
                "invalid_auth",
                "token_revoked",
                "not_authed",
                "missing_scope",
                "channel_not_found",
                "user_not_found",
                "account_inactive",
                "invalid_arguments",
                "invalid_blocks",
                "is_archived",
                "not_in_channel",
                "restricted_action",
            }
            if isinstance(code, str) and code in permanent:
                raise NotificationDeliveryError("SLACK_" + code.upper())
            raise NotificationDeliveryError("SLACK_RESPONSE_UNCERTAIN", outcome_unknown=True)
        channel, timestamp = payload.get("channel"), payload.get("ts")
        if (
            not isinstance(channel, str)
            or not re.fullmatch(r"D[A-Z0-9]{1,127}", channel)
            or not isinstance(timestamp, str)
            or not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,12}", timestamp)
        ):
            raise NotificationDeliveryError("SLACK_RECEIPT_UNCERTAIN", outcome_unknown=True)
        return TransportReceipt(channel_ref=channel, message_ref=timestamp)
