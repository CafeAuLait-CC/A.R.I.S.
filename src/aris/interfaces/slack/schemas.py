from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class SlackPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)


class SlackIdentity(SlackPayload):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")


class SlashCommand(SlackPayload):
    team_id: str = Field(min_length=1, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    command: str = Field(min_length=1, max_length=128)
    text: str = Field(default="", max_length=8192)


class SlackEvent(SlackPayload):
    type: str = Field(min_length=1, max_length=128)
    user: str | None = Field(default=None, min_length=1, max_length=128)
    tab: str | None = Field(default=None, max_length=64)
    bot_id: str | None = Field(default=None, max_length=128)
    subtype: str | None = Field(default=None, max_length=128)


class EventEnvelope(SlackPayload):
    type: str
    team_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    event: SlackEvent


class SlackAction(SlackPayload):
    action_id: str = Field(min_length=1, max_length=128)
    value: str | None = Field(default=None, max_length=2000)


class Interaction(SlackPayload):
    type: str
    team: SlackIdentity
    user: SlackIdentity
    actions: list[SlackAction] = Field(default_factory=list, max_length=64)
    trigger_id: SecretStr | None = Field(default=None, repr=False, min_length=1, max_length=256)


class GpuSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    node_id: str = Field(min_length=1, max_length=128)
    gpu_uuid: str = Field(min_length=1, max_length=128)


@dataclass(frozen=True)
class SlackReply:
    payload: dict[str, Any]
    status_code: int = 200
    after_ack: Callable[[], None] | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ModalHandle:
    view_id: str
    hash: str | None = None


@dataclass(frozen=True)
class HomeJobClaim:
    job_id: str
    team_id: str
    user_id: str
    lease_token: str
    attempts: int
    lease_until: datetime
