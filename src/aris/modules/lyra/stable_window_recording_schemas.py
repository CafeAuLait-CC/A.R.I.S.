"""A stable window uses one deterministic operation in the existing usage ledger."""

from typing import Literal
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from aris.modules.lyra.ledger_schemas import (
    UsageLedgerReceipt,
    UsagePostingRequest,
    canonical_ledger,
    ledger_fingerprint,
)
from aris.modules.lyra.stable_window_acceptance_schemas import AcceptedStableWindowReceipt
from aris.modules.lyra.usage_intervals import IntervalContract

_NAMESPACE = UUID("a45d60c0-bf4b-54aa-bd25-27f655bc78bc")


class RecordAcceptedStableWindow(IntervalContract):
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_acceptance_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def operation_id(self) -> UUID:
        return uuid5(_NAMESPACE, "ledger:" + self.key)

    @property
    def derivation_id(self) -> UUID:
        return uuid5(_NAMESPACE, "derivation:" + self.key)


class StableWindowRecordingReceipt(IntervalContract):
    assessment_scope: Literal["stable_window_recording_receipt"] = "stable_window_recording_receipt"
    quota_usage_available: Literal[False] = False
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    request: RecordAcceptedStableWindow
    acceptance: AcceptedStableWindowReceipt
    derivation_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_index: int = Field(ge=0, lt=128, strict=True)
    ledger: UsageLedgerReceipt
    duplicate: bool = Field(strict=True)

    @model_validator(mode="after")
    def bound_to_first_acceptance(self) -> "StableWindowRecordingReceipt":
        expected = UsagePostingRequest(
            id=self.request.operation_id,
            derivation_id=self.request.derivation_id,
            session_index=self.session_index,
            expected_result_fingerprint=self.derivation_result_fingerprint,
            expected_configuration_fingerprint=(
                self.acceptance.first_request.expected_recording_configuration_fingerprint
            ),
        )
        if (
            self.acceptance.key != self.request.key
            or self.acceptance.fingerprint != self.request.expected_acceptance_fingerprint
            or self.acceptance.duplicate
            or self.ledger.duplicate
            or self.ledger.id != self.request.operation_id
            or self.ledger.actor != "system:lyra"
            or self.ledger.operation != "post_measurement"
            or self.ledger.created_at < self.acceptance.created_at
            or self.ledger.request_fingerprint
            != ledger_fingerprint("request", canonical_ledger(expected))
        ):
            raise ValueError(
                "stable recording differs from its first acceptance and ledger operation"
            )
        return self
