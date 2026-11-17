import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import (
    AwareDatetime,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from aris.core.access.schemas import AccessContract
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.interfaces.slack.config import SlackSettings


class SlackRosterQuery(AccessContract):
    cursor: str | None = Field(
        default=None, min_length=1, max_length=2048, pattern=r"^\S+$", repr=False
    )


class SlackRosterMember(AccessContract):
    external_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    display_name: str = Field(min_length=1, max_length=256)
    timezone: str | None = Field(default=None, max_length=128)
    home_team_id: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    deleted: StrictBool | None = None
    is_bot: StrictBool | None = None
    is_app_user: StrictBool | None = None

    @field_validator("display_name")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Slack display name must not be blank")
        return value.strip()

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ValueError, ZoneInfoNotFoundError):
                raise ValueError("Slack timezone must be known or missing") from None
        return value


class SlackRosterPage(AccessContract):
    team_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    fetched_at: AwareDatetime
    members: tuple[SlackRosterMember, ...] = Field(max_length=50)
    next_cursor: str | None = Field(
        default=None, min_length=1, max_length=2048, pattern=r"^\S+$", repr=False
    )

    @model_validator(mode="after")
    def unique_members(self) -> "SlackRosterPage":
        if len({member.external_id for member in self.members}) != len(self.members):
            raise ValueError("Slack member IDs must be unique within a page")
        return self


class SlackDirectoryProvider(Protocol):
    def page(self, query: SlackRosterQuery) -> SlackRosterPage: ...


def directory_unavailable() -> DomainError:
    return DomainError("SLACK_DIRECTORY_UNAVAILABLE", "Slack directory could not be verified", 503)


class SlackDirectoryClient:
    """Explicit single-page reads; no automatic enrollment, email projection or retry."""

    def __init__(
        self,
        settings: SlackSettings,
        transport: httpx.BaseTransport | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not settings.configured:
            raise ValueError("Slack directory requires the configured workspace and bot")
        self.settings, self.transport, self.clock = settings, transport, clock

    def page(self, query: SlackRosterQuery) -> SlackRosterPage:
        identity = self._request("auth.test", {})
        if identity.get("team_id") != self.settings.team_id:
            raise directory_unavailable()
        params = {"limit": "50", "team_id": self.settings.team_id}
        if query.cursor is not None:
            params["cursor"] = query.cursor
        payload = self._request("users.list", params)
        try:
            raw = payload.get("members")
            if not isinstance(raw, list) or len(raw) > 50:
                raise ValueError("invalid bounded roster")
            metadata = payload.get("response_metadata", {})
            if not isinstance(metadata, dict):
                raise ValueError("invalid roster cursor metadata")
            cursor = metadata.get("next_cursor")
            if cursor == "":
                cursor = None
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("Slack directory clock requires a timezone")
            return SlackRosterPage(
                team_id=self.settings.team_id,
                fetched_at=utc(now),
                members=tuple(self._member(member) for member in raw),
                next_cursor=cursor,
            )
        except (ValidationError, ValueError, TypeError):
            raise directory_unavailable() from None

    @staticmethod
    def _member(raw: Any) -> SlackRosterMember:
        if not isinstance(raw, dict):
            raise ValueError("invalid Slack member")
        profile = raw.get("profile") or {}
        if not isinstance(profile, dict):
            raise ValueError("invalid Slack profile")
        candidates = (
            profile.get("display_name"),
            profile.get("real_name"),
            raw.get("real_name"),
            raw.get("name"),
            raw.get("id"),
        )
        name = next(
            (value.strip() for value in candidates if isinstance(value, str) and value.strip()),
            None,
        )
        zone = raw.get("tz")
        if zone is not None:
            if not isinstance(zone, str):
                raise ValueError("invalid Slack timezone")
            try:
                ZoneInfo(zone)
            except (ValueError, ZoneInfoNotFoundError):
                zone = None
        return SlackRosterMember(
            external_id=raw.get("id"),
            display_name=name,
            timezone=zone,
            home_team_id=raw.get("team_id"),
            deleted=raw.get("deleted"),
            is_bot=raw.get("is_bot"),
            is_app_user=raw.get("is_app_user"),
        )

    def _request(
        self, method: Literal["auth.test", "users.list"], params: dict[str, str]
    ) -> dict[str, Any]:
        deadline = time.monotonic() + 3
        try:
            with httpx.Client(
                timeout=httpx.Timeout(2, connect=1, write=1, pool=0.1),
                trust_env=False,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "GET" if method == "users.list" else "POST",
                    "https://slack.com/api/" + method,
                    headers={
                        "Authorization": "Bearer " + self.settings.bot_token.get_secret_value()
                    },
                    params=params if method == "users.list" else None,
                    data=params if method == "auth.test" else None,
                ) as response:
                    if response.status_code == 429:
                        raise DomainError(
                            "SLACK_DIRECTORY_RATE_LIMITED",
                            "Slack directory is rate limited; try again later",
                            429,
                        )
                    if response.status_code != 200:
                        raise directory_unavailable()
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=16384):
                        body.extend(chunk)
                        if len(body) > 524288 or time.monotonic() > deadline:
                            raise directory_unavailable()
            payload = json.loads(body)
        except (httpx.HTTPError, ValueError, TypeError):
            raise directory_unavailable() from None
        if not isinstance(payload, dict):
            raise directory_unavailable()
        if payload.get("ok") is not True:
            if payload.get("error") == "missing_scope":
                raise DomainError(
                    "SLACK_DIRECTORY_SCOPE_MISSING",
                    "Slack directory requires the users:read bot scope",
                    503,
                )
            if payload.get("error") == "invalid_cursor":
                raise DomainError(
                    "SLACK_DIRECTORY_CURSOR_INVALID",
                    "Slack directory cursor expired; refresh the list",
                    422,
                )
            raise directory_unavailable()
        return payload
