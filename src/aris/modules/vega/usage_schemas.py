"""Historical compute facts; neither current availability nor a usage charge."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, field_validator, model_validator

from aris.core.contracts import Contract


class UsageFactContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)


class UsageObservationQuery(UsageFactContract):
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    message_id: UUID
    expected_seq: int = Field(gt=0, strict=True)


class UsageObservationBatchQuery(UsageFactContract):
    queries: tuple[UsageObservationQuery, ...] = Field(min_length=1, max_length=1024)

    @model_validator(mode="after")
    def one_source_binding(self) -> "UsageObservationBatchQuery":
        if len({(item.agent_id, item.node_id) for item in self.queries}) != 1:
            raise ValueError("observation batch requires one original source binding")
        by_id: dict[UUID, UsageObservationQuery] = {}
        by_seq: dict[int, UUID] = {}
        for query in self.queries:
            if query.message_id in by_id and by_id[query.message_id] != query:
                raise ValueError("observation batch repeats one ID with different input")
            if query.expected_seq in by_seq and by_seq[query.expected_seq] != query.message_id:
                raise ValueError("observation batch repeats one sequence with different IDs")
            by_id[query.message_id] = query
            by_seq[query.expected_seq] = query.message_id
        return self


class LinuxGpuOwner(UsageFactContract):
    uid: int = Field(ge=0, le=4294967295, strict=True)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    reported_processes: int = Field(gt=0, strict=True)


class GpuUsageFact(UsageFactContract):
    gpu_uuid: str = Field(min_length=1, max_length=128)
    inventory_present: bool = Field(strict=True)
    index: int | None = Field(default=None, ge=0, strict=True)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    reported_processes: int | None = Field(default=None, ge=0, strict=True)
    owners: tuple[LinuxGpuOwner, ...] = Field(default=(), max_length=256)
    unattributed_processes: int | None = Field(default=None, ge=0, strict=True)
    ownership_quality: Literal["complete", "partial", "unknown"]

    @model_validator(mode="after")
    def honest_counts(self) -> "GpuUsageFact":
        if (self.inventory_present and (self.index is None or self.model is None)) or (
            not self.inventory_present and (self.index is not None or self.model is not None)
        ):
            raise ValueError("historical inventory metadata must be complete or absent")
        if len({owner.uid for owner in self.owners}) != len(self.owners):
            raise ValueError("historical GPU owners must have distinct UIDs")
        if self.reported_processes is None:
            if (
                self.owners
                or self.unattributed_processes is not None
                or self.ownership_quality != "unknown"
            ):
                raise ValueError("unknown compute counts cannot contain current owner counts")
        else:
            if self.unattributed_processes is None or (
                sum(owner.reported_processes for owner in self.owners) + self.unattributed_processes
                != self.reported_processes
            ):
                raise ValueError("historical per-GPU owner counts differ")
            complete = self.unattributed_processes == 0 and all(
                owner.username is not None for owner in self.owners
            )
            if self.ownership_quality != ("complete" if complete else "partial"):
                raise ValueError("historical owner quality differs from coverage")
        return self


class UsageObservationFact(UsageFactContract):
    assessment_scope: Literal["historical_observation"] = "historical_observation"
    agent_id: str
    node_id: str
    message_id: UUID
    seq: int = Field(gt=0, strict=True)
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    observed_at: AwareDatetime
    received_at: AwareDatetime
    gpu_observed_at: AwareDatetime
    compute_observed_at: AwareDatetime
    gpu_quality: Literal["ok", "error", "unsupported"]
    compute_quality: Literal["ok", "error", "unsupported", "unknown"]
    compute_counts_consistent: bool = Field(strict=True)
    gpus: tuple[GpuUsageFact, ...] = Field(max_length=128)

    @field_validator("observed_at", "received_at", "gpu_observed_at", "compute_observed_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_fact_times_and_targets(self) -> "UsageObservationFact":
        if self.compute_counts_consistent and self.compute_quality != "ok":
            raise ValueError("known compute coverage requires an available observation")
        if self.gpu_observed_at > self.observed_at or self.compute_observed_at > self.observed_at:
            raise ValueError("component observations cannot follow their envelope")
        if len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("historical usage targets must be distinct")
        if not self.compute_counts_consistent and any(
            gpu.reported_processes is not None for gpu in self.gpus
        ):
            raise ValueError("inconsistent compute coverage cannot contain known per-GPU counts")
        return self
