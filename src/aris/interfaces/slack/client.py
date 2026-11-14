import re
from typing import Any, Protocol

import httpx

from aris.interfaces.slack.config import SlackSettings


class SlackDeliveryError(Exception):
    def __init__(self, code: str, retryable: bool = True, retry_after: int = 0) -> None:
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_response"
        self.retryable = retryable
        self.retry_after = max(0, min(retry_after, 3600))
        super().__init__(self.code)


class HomePublisher(Protocol):
    def publish_home(self, user_id: str, view: dict[str, Any]) -> None: ...


class SlackClient:
    def __init__(
        self, settings: SlackSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    def publish_home(self, user_id: str, view: dict[str, Any]) -> None:
        try:
            with httpx.Client(
                timeout=10, trust_env=False, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(
                    "https://slack.com/api/views.publish",
                    headers={
                        "Authorization": "Bearer " + self.settings.bot_token.get_secret_value(),
                    },
                    json={"user_id": user_id, "view": view},
                )
        except httpx.HTTPError as exc:
            raise SlackDeliveryError("transport_error") from exc
        if response.status_code == 429:
            delay = response.headers.get("retry-after", "1")
            seconds = int(delay) if re.fullmatch(r"[0-9]{1,6}", delay) else 1
            raise SlackDeliveryError("rate_limited", retry_after=seconds)
        if response.status_code >= 500:
            raise SlackDeliveryError("upstream_unavailable")
        if response.status_code != 200:
            raise SlackDeliveryError("http_rejected", retryable=False)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SlackDeliveryError("invalid_response") from exc
        if not isinstance(payload, dict):
            raise SlackDeliveryError("invalid_response")
        if payload.get("ok") is not True:
            code = payload.get("error", "invalid_response")
            code = code if isinstance(code, str) else "invalid_response"
            permanent = {
                "invalid_auth",
                "token_revoked",
                "not_authed",
                "missing_scope",
                "user_not_found",
                "invalid_arguments",
                "invalid_view",
                "account_inactive",
            }
            raise SlackDeliveryError(code, retryable=code not in permanent)
