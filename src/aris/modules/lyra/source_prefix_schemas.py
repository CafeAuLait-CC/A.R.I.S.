"""Previously verified source exclusion, never an activity or usage measurement."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from aris.modules.lyra.trace_attestation_schemas import StoredTraceAttestation
from aris.modules.lyra.trace_consumer_schemas import (
    UsageTraceConfiguration,
    canonical_trace,
    trace_fingerprint,
)
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_SOURCE_PREFIX_BYTES = 262144


class SourcePrefixReference(IntervalContract):
    id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    through_revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    last_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    attestation_id: UUID
    attestation_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    max_observed_at: AwareDatetime | None
    created_at: AwareDatetime


class AdvanceSourcePrefix(IntervalContract):
    id: UUID
    expected_configuration_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous: SourcePrefixReference | None = None
    limit: int = Field(default=8, ge=1, le=8, strict=True)


class StoredSourcePrefix(IntervalContract):
    request: AdvanceSourcePrefix
    configuration: UsageTraceConfiguration
    proofs: tuple[StoredTraceAttestation, ...] = Field(min_length=1, max_length=8)
    max_observed_at: AwareDatetime | None
    event_id: UUID
    created_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    def reference(self) -> SourcePrefixReference:
        tail = self.proofs[-1]
        return SourcePrefixReference(
            id=self.request.id,
            fingerprint=self.fingerprint,
            configuration_fingerprint=self.configuration.fingerprint,
            through_revision=tail.revision,
            last_seq=tail.last_seq,
            attestation_id=tail.request.id,
            attestation_fingerprint=tail.fingerprint,
            max_observed_at=self.max_observed_at,
            created_at=self.created_at,
        )

    @model_validator(mode="after")
    def bound_prefix(self) -> "StoredSourcePrefix":
        previous = self.request.previous
        revision = previous.through_revision if previous else 0
        after = previous.last_seq if previous else self.configuration.initial_after_seq
        id = previous.attestation_id if previous else None
        fingerprint = previous.attestation_fingerprint if previous else None
        if (
            self.request.expected_configuration_fingerprint != self.configuration.fingerprint
            or len(self.proofs) > self.request.limit
            or (
                previous is not None
                and (
                    previous.configuration_fingerprint != self.configuration.fingerprint
                    or previous.created_at > self.created_at
                    or (
                        previous.max_observed_at is not None
                        and (
                            self.max_observed_at is None
                            or self.max_observed_at < previous.max_observed_at
                        )
                    )
                )
            )
        ):
            raise ValueError("source prefix configuration or cumulative boundary differs")
        for proof in self.proofs:
            if (
                proof.configuration_fingerprint != self.configuration.fingerprint
                or proof.consumer_id != self.configuration.consumer_id
                or proof.revision != revision + 1
                or proof.first_seq != after + 1
                or proof.previous_id != id
                or proof.previous_fingerprint != fingerprint
                or proof.created_at > self.created_at
                or (proof.telemetry_count > 0 and self.max_observed_at is None)
            ):
                raise ValueError("source prefix proof chain is discontinuous")
            revision, after, id, fingerprint = (
                proof.revision,
                proof.last_seq,
                proof.request.id,
                proof.fingerprint,
            )
        if (
            self.fingerprint != source_prefix_fingerprint(self)
            or len(canonical_trace(self).encode()) > MAX_SOURCE_PREFIX_BYTES
        ):
            raise ValueError("source prefix content differs")
        return self


def source_prefix_fingerprint(value: StoredSourcePrefix) -> str:
    return trace_fingerprint(
        "source_prefix_checkpoint", canonical_trace(value, exclude={"fingerprint"})
    )


class SourcePrefixSelection(IntervalContract):
    """An absent optional extension must not change pre-checkpoint historical fingerprints."""

    prefix_checkpoint: SourcePrefixReference | None = None

    @model_serializer(mode="wrap")
    def serialize_prefix(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        result = cast(dict[str, Any], handler(self))
        if self.prefix_checkpoint is None:
            result.pop("prefix_checkpoint", None)
        return result


def prefix_excludes_window(
    prefix: SourcePrefixReference | None,
    *,
    configuration_fingerprint: str,
    start_at: datetime,
    ceiling: int,
    read_at: datetime,
) -> bool:
    return prefix is None or (
        prefix.configuration_fingerprint == configuration_fingerprint
        and prefix.last_seq <= ceiling
        and prefix.created_at <= read_at
        and (prefix.max_observed_at is None or prefix.max_observed_at < start_at)
    )
