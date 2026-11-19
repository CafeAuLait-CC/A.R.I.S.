"""Original GPU metrics, independent of compute attribution and current inventory."""

import hashlib
import json
from datetime import datetime

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.time import utc
from aris.modules.vega.resource_schemas import Quality
from aris.modules.vega.schemas import GpuSample
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_MESSAGES,
    MAX_ARCHIVE_SOURCE_CHARACTERS,
    ArchivedUsageReference,
)
from aris.modules.vega.usage_schemas import UsageFactContract


class GpuHardwareBatchQuery(UsageFactContract):
    references: tuple[ArchivedUsageReference, ...] = Field(
        min_length=1, max_length=MAX_ARCHIVE_MESSAGES
    )
    source_character_budget: int = Field(
        default=MAX_ARCHIVE_SOURCE_CHARACTERS,
        ge=1,
        le=MAX_ARCHIVE_SOURCE_CHARACTERS,
        strict=True,
    )

    @model_validator(mode="after")
    def fixed_original_sources(self) -> "GpuHardwareBatchQuery":
        if (
            len({(item.agent_id, item.node_id) for item in self.references}) != 1
            or any(item.kind != "telemetry" for item in self.references)
            or len({item.message_id for item in self.references}) != len(self.references)
            or any(
                left.seq >= right.seq
                for left, right in zip(self.references, self.references[1:], strict=False)
            )
            or self.references[-1].seq - self.references[0].seq > 8192
        ):
            raise ValueError("hardware batch requires distinct ordered telemetry of one source")
        return self


class GpuHardwareObservation(UsageFactContract):
    reference: ArchivedUsageReference
    gpu_observed_at: AwareDatetime
    gpu_quality: Quality
    gpus: tuple[GpuSample, ...] = Field(max_length=64)

    @field_validator("gpu_observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return utc(value)

    @model_validator(mode="after")
    def original_component(self) -> "GpuHardwareObservation":
        if (
            self.reference.kind != "telemetry"
            or self.gpu_observed_at > self.reference.observed_at
            or (self.gpu_quality != "ok" and self.gpus)
            or len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus)
            or len({gpu.index for gpu in self.gpus}) != len(self.gpus)
        ):
            raise ValueError("hardware observation differs from its original GPU component")
        return self


class GpuHardwareBatch(UsageFactContract):
    query: GpuHardwareBatchQuery
    observations: tuple[GpuHardwareObservation, ...] = Field(
        min_length=1, max_length=MAX_ARCHIVE_MESSAGES
    )
    source_characters: int = Field(ge=1, le=MAX_ARCHIVE_SOURCE_CHARACTERS)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def exact_batch(self) -> "GpuHardwareBatch":
        if (
            tuple(item.reference for item in self.observations) != self.query.references
            or self.source_characters > self.query.source_character_budget
            or self.fingerprint != hardware_batch_fingerprint(self)
        ):
            raise ValueError("hardware batch differs from its exact original references")
        return self


def hardware_batch_fingerprint(value: GpuHardwareBatch) -> str:
    raw = json.dumps(
        value.model_dump(mode="json", exclude={"fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.vega.hardware.original.batch\x00" + raw.encode()).hexdigest()
