from typing import Literal

from pydantic import AwareDatetime

from aris.core.contracts import Contract


class MessageStorage(Contract):
    message_kind: str
    count: int
    first_received_at: AwareDatetime
    last_received_at: AwareDatetime
    before_cutoff: int | None = None


class ObservationStorage(Contract):
    generated_at: AwareDatetime
    cutoff: AwareDatetime | None = None
    messages: list[MessageStorage]
    deletion_enabled: Literal[False] = False
    blockers: list[str]
