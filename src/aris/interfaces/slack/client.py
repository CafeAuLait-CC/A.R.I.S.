import re
from typing import Any, Literal, Protocol

import httpx

from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.schemas import ModalHandle


class SlackDeliveryError(Exception):
    def __init__(self, code: str, retryable: bool = True, retry_after: int = 0) -> None:
        self.code = code if re.fullmatch(r"[a-z_]{1,64}", code) else "invalid_response"
        self.retryable = retryable
        self.retry_after = max(0, min(retry_after, 3600))
        super().__init__(self.code)


class HomePublisher(Protocol):
    def publish_home(self, user_id: str, view: dict[str, Any]) -> None: ...


class ModalPublisher(Protocol):
    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> ModalHandle: ...
    def update_modal(self, handle: ModalHandle, view: dict[str, Any]) -> None: ...


class SlackClient:
    def __init__(
        self, settings: SlackSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.transport = transport

    def publish_home(self, user_id: str, view: dict[str, Any]) -> None:
        self._request("views.publish", {"user_id": user_id, "view": view})

    def open_modal(self, trigger_id: str, view: dict[str, Any]) -> ModalHandle:
        payload = self._request("views.open", {"trigger_id": trigger_id, "view": view})
        result = payload.get("view")
        if not isinstance(result, dict) or not isinstance(result.get("id"), str):
            raise SlackDeliveryError("invalid_response", retryable=False)
        view_id, view_hash = result["id"], result.get("hash")
        if not 1 <= len(view_id) <= 128 or (
            view_hash is not None and (not isinstance(view_hash, str) or len(view_hash) > 256)
        ):
            raise SlackDeliveryError("invalid_response", retryable=False)
        return ModalHandle(view_id, view_hash)

    def update_modal(self, handle: ModalHandle, view: dict[str, Any]) -> None:
        payload: dict[str, Any] = {"view_id": handle.view_id, "view": view}
        if handle.hash is not None:
            payload["hash"] = handle.hash
        self._request("views.update", payload)

    def _request(
        self, method: Literal["views.publish", "views.open", "views.update"], body: dict[str, Any]
    ) -> dict[str, Any]:
        timeout = (
            httpx.Timeout(1.0, connect=0.5, write=0.5, pool=0.1)
            if method == "views.open"
            else httpx.Timeout(10)
        )
        try:
            with httpx.Client(
                timeout=timeout, trust_env=False, follow_redirects=False, transport=self.transport
            ) as client:
                response = client.post(
                    "https://slack.com/api/" + method,
                    headers={
                        "Authorization": "Bearer " + self.settings.bot_token.get_secret_value(),
                    },
                    json=body,
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
                "invalid_trigger_id",
                "expired_trigger_id",
                "trigger_exchanged",
                "view_not_found",
                "hash_conflict",
            }
            raise SlackDeliveryError(code, retryable=code not in permanent)
        return payload
