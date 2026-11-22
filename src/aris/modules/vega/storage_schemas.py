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


class ObservedWindowStorage(Contract):
    messages: int
    late_arrivals: int
    first_seq: int | None
    last_seq: int | None


RetentionPrefixStop = Literal[
    "payload_end",
    "batch_limit",
    "unverified_prior_sequences",
    "sequence_gap",
    "source_binding_differs",
    "outside_observation_window",
    "received_after_cutoff",
    "protected_sequence",
]


class RetentionPrefixPreview(Contract):
    first_seq: int | None = None
    through_seq: int | None = None
    stop_reason: RetentionPrefixStop
    stopped_before_seq: int | None = None
