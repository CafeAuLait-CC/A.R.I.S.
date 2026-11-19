"""Small native-modal contract; metadata binds display context, never authority."""

from typing import Literal

from pydantic import Field

from aris.core.access.schemas import AccessContract

USAGE_PREFIX = "aris_usage_"
USAGE_HOME_ACTION = USAGE_PREFIX + "open"
USAGE_NODE_ACTION = USAGE_PREFIX + "node"
USAGE_PERIOD_ACTION = USAGE_PREFIX + "period"
USAGE_REFRESH_ACTION = USAGE_PREFIX + "refresh"
USAGE_CALLBACK = "aris_usage_modal"
USAGE_CONTROLS = "aris_usage_controls"


class UsageModalBinding(AccessContract):
    team_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    user_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    view_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    period: Literal["today", "week"]
