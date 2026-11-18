"""One fixed closed-run proof to one atomic measured-ledger operation."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.ledger_schemas import (
    UsageLedgerReceipt,
    UsagePostingRequest,
    canonical_ledger,
    ledger_fingerprint,
)
from aris.modules.lyra.residency_evidence_schemas import (
    ResidencyRunReference,
    VerifiedResidencyWindow,
)
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_RUN_RECORD_BYTES = 524288
_DERIVATION_NAMESPACE = UUID("59a66cce-f894-4ec9-9f69-86d2deddb90e")


class ClosedRunRecordingRequest(IntervalContract):
    id: UUID
    reference: ResidencyRunReference
    expected_trace_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_recording_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)

    @property
    def derivation_id(self) -> UUID:
        return uuid5(_DERIVATION_NAMESPACE, str(self.id))


class ClosedRunRecordingReceipt(IntervalContract):
    assessment_scope: Literal["closed_run_recording_receipt"] = "closed_run_recording_receipt"
    quota_usage_available: Literal[False] = False
    id: UUID
    event_id: UUID
    duplicate: bool = Field(strict=True)
    node_id: str
    run_id: str
    created_at: AwareDatetime
    proof_fingerprint: str
    derivation_id: UUID
    derivation_result_fingerprint: str
    ledger: UsageLedgerReceipt
    fingerprint: str


class StoredClosedRunRecording(IntervalContract):
    request: ClosedRunRecordingRequest
    actor: Literal["system:lyra"] = "system:lyra"
    proof: VerifiedResidencyWindow = Field(repr=False)
    derivation_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_index: int = Field(ge=0, lt=16384, strict=True)
    ledger: UsageLedgerReceipt
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_result(self) -> "StoredClosedRunRecording":
        if (
            self.proof.reference != self.request.reference
            or self.proof.state_at_record != "closed"
            or self.proof.candidate_state != "stable_candidate"
            or self.proof.configuration_fingerprint
            != self.request.expected_trace_configuration_fingerprint
            or self.proof.run.first.node_id != self.ledger.head.node_id
            or self.ledger.id != self.request.id
            or self.ledger.actor != self.actor
            or self.ledger.operation != "post_measurement"
            or self.ledger.duplicate
            or self.ledger.request_fingerprint
            != ledger_fingerprint(
                "request",
                canonical_ledger(
                    UsagePostingRequest(
                        id=self.request.id,
                        derivation_id=self.request.derivation_id,
                        session_index=self.session_index,
                        expected_result_fingerprint=self.derivation_result_fingerprint,
                        expected_configuration_fingerprint=self.request.expected_recording_configuration_fingerprint,
                    )
                ),
            )
            or not self.proof.read_at <= self.ledger.created_at <= self.created_at
            or self.fingerprint != run_recording_fingerprint(self)
            or len(canonical_run_recording(self).encode()) > MAX_RUN_RECORD_BYTES
        ):
            raise ValueError(
                "closed-run receipt differs from its original proof or ledger operation"
            )
        return self

    def receipt(self, *, duplicate: bool) -> ClosedRunRecordingReceipt:
        return ClosedRunRecordingReceipt(
            id=self.request.id,
            event_id=self.event_id,
            duplicate=duplicate,
            node_id=self.ledger.head.node_id,
            run_id=self.proof.run.id,
            created_at=self.created_at,
            proof_fingerprint=self.proof.fingerprint,
            derivation_id=self.request.derivation_id,
            derivation_result_fingerprint=self.derivation_result_fingerprint,
            ledger=self.ledger,
            fingerprint=self.fingerprint,
        )


def canonical_run_recording(value: IntervalContract, *, exclude: set[str] | None = None) -> str:
    return json.dumps(
        value.model_dump(mode="json", exclude=exclude),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def run_recording_fingerprint(value: StoredClosedRunRecording) -> str:
    raw = canonical_run_recording(value, exclude={"fingerprint"})
    return hashlib.sha256(b"aris.lyra.closed_run.record\x00" + raw.encode()).hexdigest()
