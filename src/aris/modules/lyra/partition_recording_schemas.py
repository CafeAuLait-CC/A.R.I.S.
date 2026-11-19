"""Closed-parent partitions use the same immutable measured ledger authority."""

import hashlib
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid5

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.history_schemas import HistoricalNodeUserResolution
from aris.modules.lyra.ledger_schemas import (
    UsageLedgerReceipt,
    UsagePostingRequest,
    canonical_ledger,
    ledger_fingerprint,
)
from aris.modules.lyra.residency_partition_schemas import (
    ResidencyPartitionReference,
    VerifiedResidencyPartition,
)
from aris.modules.lyra.run_recording_schemas import MAX_RUN_RECORD_BYTES, canonical_run_recording
from aris.modules.lyra.usage_intervals import IntervalContract

_NAMESPACE = UUID("0584395e-15d4-48a0-a776-321d17539b09")


class PartitionRecordingRequest(IntervalContract):
    id: UUID
    partition: ResidencyPartitionReference
    expected_recording_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)

    @property
    def derivation_id(self) -> UUID:
        return uuid5(_NAMESPACE, str(self.id))


class PartitionRecordingReceipt(IntervalContract):
    assessment_scope: Literal["partition_recording_receipt"] = "partition_recording_receipt"
    quota_usage_available: Literal[False] = False
    id: UUID
    event_id: UUID
    duplicate: bool = Field(strict=True)
    node_id: str
    run_id: str
    partition_index: int = Field(ge=0, strict=True)
    partition_count: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    proof_fingerprint: str
    derivation_id: UUID
    derivation_result_fingerprint: str
    ledger: UsageLedgerReceipt
    fingerprint: str


class StoredPartitionRecording(IntervalContract):
    request: PartitionRecordingRequest
    actor: Literal["system:lyra"] = "system:lyra"
    proof: VerifiedResidencyPartition = Field(repr=False)
    parent_identity: HistoricalNodeUserResolution = Field(repr=False)
    derivation_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    session_index: int = Field(ge=0, lt=128, strict=True)
    ledger: UsageLedgerReceipt
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("created_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bound_partition_result(self) -> "StoredPartitionRecording":
        run = self.proof.parent_run
        identity = self.parent_identity
        query = identity.query
        if (
            self.proof.request != self.request.partition
            or identity.status != "resolved"
            or identity.user_id is None
            or query.node_id != run.first.node_id
            or query.os_username != run.username
            or query.start_at != run.first.compute_observed_at
            or query.end_at != run.last.compute_observed_at
            or query.as_of != self.request.partition.reference.source_cutoff
            or query.through_revision != self.request.identity_revision
            or identity.directory_revision != self.request.identity_revision
            or self.ledger.id != self.request.id
            or self.ledger.actor != self.actor
            or self.ledger.operation != "post_measurement"
            or self.ledger.duplicate
            or self.ledger.head.node_id != run.first.node_id
            or not self.proof.read_at <= self.ledger.created_at <= self.created_at
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
            or self.fingerprint != partition_recording_fingerprint(self)
            or len(canonical_run_recording(self).encode()) > MAX_RUN_RECORD_BYTES
        ):
            raise ValueError(
                "partition recording differs from its original proof, identity or ledger result"
            )
        return self

    def receipt(self, *, duplicate: bool) -> PartitionRecordingReceipt:
        return PartitionRecordingReceipt(
            id=self.request.id,
            event_id=self.event_id,
            duplicate=duplicate,
            node_id=self.ledger.head.node_id,
            run_id=self.proof.parent_run.id,
            partition_index=self.request.partition.partition_index,
            partition_count=self.proof.partition_count,
            created_at=self.created_at,
            proof_fingerprint=self.proof.fingerprint,
            derivation_id=self.request.derivation_id,
            derivation_result_fingerprint=self.derivation_result_fingerprint,
            ledger=self.ledger,
            fingerprint=self.fingerprint,
        )


def partition_recording_fingerprint(value: StoredPartitionRecording) -> str:
    return hashlib.sha256(
        b"aris.lyra.partition.record\x00"
        + canonical_run_recording(value, exclude={"fingerprint"}).encode()
    ).hexdigest()
