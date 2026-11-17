"""Pure sampled-use candidates, without platform attribution, charging or persistence."""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, ConfigDict, Field, model_validator

from aris.core.contracts import Contract, DomainError
from aris.modules.vega.usage_schemas import GpuUsageFact, UsageObservationFact


class IntervalContract(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationReference(IntervalContract):
    agent_id: str
    node_id: str
    message_id: UUID
    seq: int = Field(gt=0, strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    compute_observed_at: AwareDatetime


class GpuIntervalCandidate(IntervalContract):
    gpu_uuid: str
    state: Literal["stable_owner", "observed_idle", "needs_review"]
    reason: Literal[
        "SAME_LINUX_OWNER_AT_ENDPOINTS",
        "NO_COMPUTE_APPS_AT_ENDPOINTS",
        "OWNER_CHANGED",
        "OWNER_METADATA_CHANGED",
        "OWNERSHIP_INCOMPLETE",
        "MULTIPLE_OWNERS",
        "USAGE_TRANSITION",
        "GPU_INVENTORY_CHANGED",
        "GPU_MODEL_CHANGED",
    ]
    uid: int | None = Field(default=None, ge=0, le=4294967295, strict=True)
    username: str | None = None
    model: str | None = None

    @model_validator(mode="after")
    def honest_owner(self) -> "GpuIntervalCandidate":
        if self.state == "stable_owner":
            if (
                self.uid is None
                or self.username is None
                or self.model is None
                or self.reason != "SAME_LINUX_OWNER_AT_ENDPOINTS"
            ):
                raise ValueError(
                    "stable owner candidate requires its original Linux owner and model"
                )
        elif self.uid is not None or self.username is not None:
            raise ValueError("unattributed interval cannot contain an owner")
        if (self.state == "observed_idle") != (self.reason == "NO_COMPUTE_APPS_AT_ENDPOINTS"):
            raise ValueError("idle interval reason differs")
        return self


PairReason = Literal[
    "CANDIDATES_GENERATED",
    "EXACT_REPLAY",
    "CACHED_COMPUTE_OBSERVATION",
    "SOURCE_BINDING_CHANGED",
    "SOURCE_CONTENT_CONFLICT",
    "SEQUENCE_NOT_INCREASING",
    "CLOCK_ROLLBACK",
    "OBSERVATION_CHANGED_AT_SAME_TIME",
    "OBSERVATION_GAP",
    "COMPUTE_UNAVAILABLE",
]


class UsagePairAssessment(IntervalContract):
    assessment_scope: Literal["sample_interval_candidate"] = "sample_interval_candidate"
    state: Literal["same_observation", "interval", "unavailable"]
    reason: PairReason
    previous: ObservationReference
    current: ObservationReference
    duration_microseconds: int | None = Field(default=None, gt=0, strict=True)
    gpus: tuple[GpuIntervalCandidate, ...] = Field(default=(), max_length=256)

    @model_validator(mode="after")
    def honest_interval(self) -> "UsagePairAssessment":
        if self.state == "interval":
            if (
                self.duration_microseconds is None
                or self.current.compute_observed_at <= self.previous.compute_observed_at
                or self.reason != "CANDIDATES_GENERATED"
            ):
                raise ValueError("an interval requires a positive observed time window")
            delta = self.current.compute_observed_at - self.previous.compute_observed_at
            expected = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
            if self.duration_microseconds != expected:
                raise ValueError("interval duration differs from its source timestamps")
            if (self.previous.agent_id, self.previous.node_id) != (
                self.current.agent_id,
                self.current.node_id,
            ) or self.current.seq <= self.previous.seq:
                raise ValueError("an interval requires increasing source observations")
        elif self.duration_microseconds is not None or self.gpus:
            raise ValueError("unavailable or repeated observations cannot contain a usage interval")
        repeated = self.reason in {"EXACT_REPLAY", "CACHED_COMPUTE_OBSERVATION"}
        if (self.state == "same_observation") != repeated:
            raise ValueError("repeated observation state differs from its reason")
        if self.state == "unavailable" and self.reason == "CANDIDATES_GENERATED":
            raise ValueError("unavailable observations cannot claim generated candidates")
        if len({gpu.gpu_uuid for gpu in self.gpus}) != len(self.gpus):
            raise ValueError("interval targets must be unique")
        return self


def _reference(fact: UsageObservationFact) -> ObservationReference:
    return ObservationReference(
        agent_id=fact.agent_id,
        node_id=fact.node_id,
        message_id=fact.message_id,
        seq=fact.seq,
        fingerprint=fact.source_fingerprint,
        compute_observed_at=fact.compute_observed_at,
    )


def _compute_signature(fact: UsageObservationFact) -> tuple[object, ...]:
    # Inventory-only changes and newer GPU/system snapshots do not refresh the
    # cached compute measurement. Keep only the actual nonzero/unknown use facts.
    return (
        fact.compute_quality,
        fact.compute_counts_consistent,
        tuple(
            (
                gpu.gpu_uuid,
                gpu.reported_processes,
                gpu.unattributed_processes,
                gpu.ownership_quality,
                tuple(
                    (owner.uid, owner.username, owner.reported_processes) for owner in gpu.owners
                ),
            )
            for gpu in sorted(fact.gpus, key=lambda gpu: gpu.gpu_uuid)
            if gpu.reported_processes != 0
        ),
    )


def _gpu_candidate(
    previous: GpuUsageFact | None, current: GpuUsageFact | None
) -> GpuIntervalCandidate:
    if current is not None:
        uuid = current.gpu_uuid
    else:
        assert previous is not None  # caller supplies a union target
        uuid = previous.gpu_uuid
    reason = "GPU_INVENTORY_CHANGED"
    if (
        previous is not None
        and current is not None
        and previous.inventory_present
        and current.inventory_present
    ):
        if previous.model != current.model:
            reason = "GPU_MODEL_CHANGED"
        elif previous.ownership_quality != "complete" or current.ownership_quality != "complete":
            reason = "OWNERSHIP_INCOMPLETE"
        elif previous.reported_processes == current.reported_processes == 0:
            return GpuIntervalCandidate(
                gpu_uuid=uuid,
                state="observed_idle",
                reason="NO_COMPUTE_APPS_AT_ENDPOINTS",
                model=current.model,
            )
        elif previous.reported_processes == 0 or current.reported_processes == 0:
            reason = "USAGE_TRANSITION"
        elif len(previous.owners) != 1 or len(current.owners) != 1:
            reason = "MULTIPLE_OWNERS"
        elif previous.owners[0].uid != current.owners[0].uid:
            reason = "OWNER_CHANGED"
        elif previous.owners[0].username != current.owners[0].username:
            reason = "OWNER_METADATA_CHANGED"
        else:
            return GpuIntervalCandidate(
                gpu_uuid=uuid,
                state="stable_owner",
                reason="SAME_LINUX_OWNER_AT_ENDPOINTS",
                uid=current.owners[0].uid,
                username=current.owners[0].username,
                model=current.model,
            )
    return GpuIntervalCandidate(gpu_uuid=uuid, state="needs_review", reason=reason)


def assess_usage_pair(
    previous: UsageObservationFact, current: UsageObservationFact, *, max_gap_seconds: int
) -> UsagePairAssessment:
    if type(max_gap_seconds) is not int or not 1 <= max_gap_seconds <= 3600:
        raise DomainError(
            "VALIDATION_ERROR", "An explicit bounded continuity interval is required", 422
        )
    previous_ref, current_ref = _reference(previous), _reference(current)

    def unavailable(reason: PairReason) -> UsagePairAssessment:
        return UsagePairAssessment(
            previous=previous_ref, current=current_ref, state="unavailable", reason=reason
        )

    if (previous.agent_id, previous.node_id) != (current.agent_id, current.node_id):
        return unavailable("SOURCE_BINDING_CHANGED")
    if previous.message_id == current.message_id:
        if previous == current:
            return UsagePairAssessment(
                previous=previous_ref,
                current=current_ref,
                state="same_observation",
                reason="EXACT_REPLAY",
            )
        return unavailable("SOURCE_CONTENT_CONFLICT")
    if current.seq <= previous.seq:
        return unavailable("SEQUENCE_NOT_INCREASING")
    if current.compute_observed_at < previous.compute_observed_at:
        return unavailable("CLOCK_ROLLBACK")
    if current.compute_observed_at == previous.compute_observed_at:
        if _compute_signature(previous) == _compute_signature(current):
            return UsagePairAssessment(
                previous=previous_ref,
                current=current_ref,
                state="same_observation",
                reason="CACHED_COMPUTE_OBSERVATION",
            )
        return unavailable("OBSERVATION_CHANGED_AT_SAME_TIME")
    delta = current.compute_observed_at - previous.compute_observed_at
    microseconds = (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds
    if microseconds > max_gap_seconds * 1000000:
        return unavailable("OBSERVATION_GAP")
    if any(
        fact.compute_quality != "ok" or not fact.compute_counts_consistent
        for fact in (previous, current)
    ):
        return unavailable("COMPUTE_UNAVAILABLE")
    before = {gpu.gpu_uuid: gpu for gpu in previous.gpus}
    after = {gpu.gpu_uuid: gpu for gpu in current.gpus}
    return UsagePairAssessment(
        previous=previous_ref,
        current=current_ref,
        state="interval",
        reason="CANDIDATES_GENERATED",
        duration_microseconds=microseconds,
        gpus=tuple(
            _gpu_candidate(before.get(uuid), after.get(uuid))
            for uuid in sorted(set(before) | set(after))
        ),
    )
