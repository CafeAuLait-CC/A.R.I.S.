"""Group reported compute observations, without inferring continuous use or deleting sources."""

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_archive_schemas import (
    ArchivedUsageItem,
    ArchivedUsageReference,
    UsageArchiveHead,
    UsageArchivePage,
    UsageArchiveQuery,
)
from aris.modules.vega.usage_schemas import GpuUsageFact, UsageObservationFact

MAX_PREVIEW_MESSAGES = 1024
MAX_PREVIEW_CHARACTERS = 8 * 1024 * 1024
MAX_PREVIEW_TARGETS = 32
MAX_PREVIEW_GROUPS = 4096
ActivityState = Literal["reported_busy", "reported_idle", "unknown"]


class ActivityPreviewRule(IntervalContract):
    mode: Literal["diagnostic_display_only"] = "diagnostic_display_only"
    source_ref: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    max_gap_seconds: int = Field(ge=1, le=3600, strict=True)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(b"aris.lyra.activity.preview.rule\x00" + raw.encode()).hexdigest()


class ActivityPageReference(IntervalContract):
    query: UsageArchiveQuery
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_characters: int = Field(ge=0)


class ComputeObservationGroup(IntervalContract):
    gpu_uuid: str
    index: int | None
    model: str | None
    inventory_present: bool
    gpu_quality: Literal["ok", "error", "unsupported"]
    compute_quality: Literal["ok", "error", "unsupported", "unknown"]
    compute_counts_consistent: bool
    state: ActivityState
    reason: str = Field(pattern=r"^[A-Z_]+$", max_length=64)
    boundary: str = Field(pattern=r"^[A-Z_]+$", max_length=64)
    ownership_quality: Literal["complete", "partial", "unknown"]
    known_linux_owners: int | None = Field(ge=0)
    reported_processes: int | None = Field(ge=0)
    first_source: ArchivedUsageReference
    last_source: ArchivedUsageReference
    first_compute_observed_at: AwareDatetime
    last_compute_observed_at: AwareDatetime
    envelopes: int = Field(ge=1)
    distinct_compute_samples: int = Field(ge=0)
    repeated_compute_envelopes: int = Field(ge=0)

    @model_validator(mode="after")
    def positive_source_order(self) -> "ComputeObservationGroup":
        if (
            self.last_source.seq < self.first_source.seq
            or self.last_source.observed_at < self.first_source.observed_at
            or self.last_compute_observed_at < self.first_compute_observed_at
            or self.distinct_compute_samples + self.repeated_compute_envelopes > self.envelopes
        ):
            raise ValueError("activity group reverses its original source or component times")
        return self


class ObservationGap(IntervalContract):
    before: ArchivedUsageReference
    after: ArchivedUsageReference
    basis: Literal["sample_interval_exceeds_diagnostic_rule"] = (
        "sample_interval_exceeds_diagnostic_rule"
    )


class ActivityPreview(IntervalContract):
    assessment_scope: Literal["reported_compute_observation_groups"] = (
        "reported_compute_observation_groups"
    )
    continuous_usage_established: Literal[False] = False
    quota_usage_available: Literal[False] = False
    total_usage_microseconds: None = None
    retention_enabled: Literal[False] = False
    source_packets_preserved: Literal[True] = True
    prior_source_context: Literal["not_loaded"] = "not_loaded"
    requested_range_only: Literal[True] = True
    agent_id: str
    node_id: str
    rule: ActivityPreviewRule
    rule_fingerprint: str
    source_ceiling_fingerprint: str
    highest_committed_seq: int
    requested_after_seq: int
    last_read_seq: int
    missing_seq: int | None
    range_complete: bool
    source_pages: tuple[ActivityPageReference, ...] = Field(max_length=8)
    source_packets: int = Field(ge=0, le=MAX_PREVIEW_MESSAGES)
    source_characters: int = Field(ge=0, le=MAX_PREVIEW_CHARACTERS)
    heartbeat_packets: int = Field(ge=0)
    telemetry_packets: int = Field(ge=0)
    input_gpu_facts: int = Field(ge=0)
    missing_inventory_markers: int = Field(ge=0)
    distinct_compute_frames: int = Field(ge=0)
    repeated_compute_frames: int = Field(ge=0)
    no_target_telemetry: int = Field(ge=0)
    groups: tuple[ComputeObservationGroup, ...] = Field(max_length=MAX_PREVIEW_GROUPS)
    gaps: tuple[ObservationGap, ...] = Field(max_length=MAX_PREVIEW_MESSAGES)
    read_at: AwareDatetime
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent_summary(self) -> "ActivityPreview":
        if (
            self.heartbeat_packets + self.telemetry_packets != self.source_packets
            or self.input_gpu_facts + self.missing_inventory_markers
            != sum(group.envelopes for group in self.groups)
            or self.source_characters != sum(page.source_characters for page in self.source_pages)
            or not 0 <= self.requested_after_seq <= self.last_read_seq <= self.highest_committed_seq
            or self.range_complete != (self.last_read_seq == self.highest_committed_seq)
            or self.distinct_compute_frames + self.repeated_compute_frames > self.telemetry_packets
            or self.rule_fingerprint != self.rule.fingerprint
            or self.fingerprint != preview_fingerprint(self)
        ):
            raise ValueError("activity summary differs from its diagnostic source or groups")
        return self


def preview_fingerprint(value: ActivityPreview) -> str:
    raw = json.dumps(
        value.model_dump(mode="json", exclude={"fingerprint", "read_at"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.lyra.activity.preview\x00" + raw.encode()).hexdigest()


def _limit() -> DomainError:
    return DomainError(
        "ACTIVITY_PREVIEW_TOO_LARGE",
        "Activity preview exceeds its bounded source or output budget",
        503,
    )


def _gpu_signature(gpu: GpuUsageFact) -> tuple[object, ...]:
    return (
        gpu.inventory_present,
        gpu.index,
        gpu.model,
        gpu.reported_processes,
        gpu.unattributed_processes,
        gpu.ownership_quality,
        tuple(
            sorted((owner.uid, owner.username, owner.reported_processes) for owner in gpu.owners)
        ),
    )


def _state(
    fact: UsageObservationFact, gpu: GpuUsageFact | None, time_reason: str | None, gap: timedelta
) -> tuple[ActivityState, str]:
    if time_reason is not None:
        return "unknown", time_reason
    if (
        fact.observed_at - fact.compute_observed_at > gap
        or fact.observed_at - fact.gpu_observed_at > gap
        or abs(fact.compute_observed_at - fact.gpu_observed_at) > gap
    ):
        return "unknown", "COMPONENT_TIME_STALE"
    if fact.gpu_quality != "ok":
        return "unknown", "GPU_OBSERVATION_UNAVAILABLE"
    if gpu is None or not gpu.inventory_present:
        return "unknown", "NOT_IN_REPORTED_INVENTORY"
    if (
        fact.compute_quality != "ok"
        or not fact.compute_counts_consistent
        or gpu.reported_processes is None
    ):
        return "unknown", "COMPUTE_OBSERVATION_UNAVAILABLE"
    return (
        ("reported_busy", "PROCESSES_REPORTED")
        if gpu.reported_processes > 0
        else ("reported_idle", "NO_COMPUTE_PROCESSES_REPORTED")
    )


def group_compute_observations(
    ceiling: UsageArchiveHead,
    pages: tuple[UsageArchivePage, ...],
    rule: ActivityPreviewRule,
    *,
    max_messages: int,
) -> ActivityPreview:
    ceiling = UsageArchiveHead.model_validate(ceiling.model_dump(mode="json"))
    rule = ActivityPreviewRule.model_validate(rule.model_dump(mode="json"))
    if (
        type(max_messages) is not int
        or not 1 <= max_messages <= MAX_PREVIEW_MESSAGES
        or len(pages) > 8
    ):
        raise _limit()
    after = max(0, ceiling.highest_committed_seq - max_messages)
    cursor = after
    source_characters = 0
    items: list[ArchivedUsageItem] = []
    references = []
    missing = None
    read_at = ceiling.read_at
    for original in pages:
        page = UsageArchivePage.model_validate(original.model_dump(mode="json"))
        if missing is not None or (
            page.query.agent_id,
            page.query.node_id,
            page.query.after_seq,
            page.query.through_seq,
        ) != (ceiling.query.agent_id, ceiling.query.node_id, cursor, ceiling.highest_committed_seq):
            raise DomainError(
                "VALIDATION_ERROR",
                "Activity pages do not form the requested fixed source range",
                422,
            )
        source_characters += page.source_characters
        if (
            source_characters > MAX_PREVIEW_CHARACTERS
            or len(items) + len(page.items) > max_messages
        ):
            raise _limit()
        items.extend(page.items)
        cursor = page.next_after_seq
        missing = page.missing_seq
        read_at = max(read_at, page.read_at)
        # A gap can contain zero rows; its exact query and fingerprint are still retained.
        references.append(
            ActivityPageReference(
                query=page.query,
                fingerprint=page.fingerprint,
                source_characters=page.source_characters,
            )
        )
    if (
        ceiling.highest_committed_seq > 0
        and not pages
        or ceiling.highest_committed_seq == 0
        and pages
    ):
        raise DomainError(
            "VALIDATION_ERROR",
            "Activity source range is absent or differs from its empty ceiling",
            422,
        )
    groups: list[ComputeObservationGroup] = []
    keys: dict[str, tuple[object, ...]] = {}
    positions: dict[str, int] = {}
    targets: set[str] = set()
    gaps = []
    high_compute: datetime | None = None
    high_envelope: datetime | None = None
    high_signature: tuple[object, ...] | None = None
    quarantine = False
    conflicted = False
    previous: UsageObservationFact | None = None
    previous_ref: ArchivedUsageReference | None = None
    heartbeat = telemetry = input_facts = markers = distinct_frames = repeated_frames = (
        no_target
    ) = 0
    max_gap = timedelta(seconds=rule.max_gap_seconds)
    for item in items:
        fact = item.observation
        if fact is None:
            heartbeat += 1
            continue
        telemetry += 1
        input_facts += len(fact.gpus)
        if not fact.gpus:
            no_target += 1
        by_id = {gpu.gpu_uuid: gpu for gpu in fact.gpus}
        targets.update(by_id)
        if len(targets) > MAX_PREVIEW_TARGETS:
            raise _limit()
        signature = (
            fact.gpu_quality,
            fact.compute_quality,
            fact.compute_counts_consistent,
            tuple(
                (gpu.gpu_uuid, *_gpu_signature(gpu))
                for gpu in sorted(fact.gpus, key=lambda gpu: gpu.gpu_uuid)
            ),
        )
        repeated = high_compute is not None and fact.compute_observed_at == high_compute
        if repeated:
            repeated_frames += 1
        regressed = (high_compute is not None and fact.compute_observed_at < high_compute) or (
            high_envelope is not None and fact.observed_at < high_envelope
        )
        if regressed:
            quarantine = True
        progressed = (high_compute is None or fact.compute_observed_at > high_compute) and (
            high_envelope is None or fact.observed_at > high_envelope
        )
        if progressed:
            quarantine = False
            conflicted = False
        if repeated and signature != high_signature:
            conflicted = True
        reason = (
            "SOURCE_CLOCK_REGRESSION"
            if quarantine
            else "SAME_TIME_FACT_CONFLICT"
            if conflicted
            else None
        )
        fresh = (
            (high_compute is None or fact.compute_observed_at > high_compute)
            and fact.compute_quality == "ok"
            and fact.compute_counts_consistent
            and reason is None
        )
        if fresh:
            distinct_frames += 1
        gap_before = previous is not None and (
            fact.compute_observed_at - previous.compute_observed_at > max_gap
            or fact.observed_at - previous.observed_at > max_gap
        )
        if gap_before:
            assert previous_ref is not None
            gaps.append(ObservationGap(before=previous_ref, after=item.reference))
        for target in sorted(targets):
            gpu = by_id.get(target)
            if gpu is None:
                markers += 1
            state, state_reason = _state(fact, gpu, reason, max_gap)
            key = (
                state,
                state_reason,
                _gpu_signature(gpu) if gpu is not None else None,
                fact.gpu_quality,
                fact.compute_quality,
                fact.compute_counts_consistent,
            )
            index = positions.get(target)
            prior = groups[index] if index is not None else None
            merge = (
                prior is not None
                and keys[target] == key
                and not gap_before
                and item.reference.observed_at >= prior.last_source.observed_at
                and fact.compute_observed_at >= prior.last_compute_observed_at
            )
            sample = int(fresh and gpu is not None and gpu.reported_processes is not None)
            if merge:
                assert index is not None and prior is not None
                groups[index] = prior.model_copy(
                    update={
                        "last_source": item.reference,
                        "last_compute_observed_at": fact.compute_observed_at,
                        "envelopes": prior.envelopes + 1,
                        "distinct_compute_samples": prior.distinct_compute_samples + sample,
                        "repeated_compute_envelopes": prior.repeated_compute_envelopes
                        + int(repeated),
                    }
                )
            else:
                if len(groups) >= MAX_PREVIEW_GROUPS:
                    raise _limit()
                boundary = (
                    "RANGE_START"
                    if prior is None
                    else "SAMPLE_GAP"
                    if gap_before
                    else "CLOCK_OR_FACT_CONFLICT"
                    if reason
                    else "OBSERVATION_CHANGED"
                )
                value = ComputeObservationGroup(
                    gpu_uuid=target,
                    index=gpu.index if gpu else None,
                    model=gpu.model if gpu else None,
                    inventory_present=gpu.inventory_present if gpu else False,
                    gpu_quality=fact.gpu_quality,
                    compute_quality=fact.compute_quality,
                    compute_counts_consistent=fact.compute_counts_consistent,
                    state=state,
                    reason=state_reason,
                    boundary=boundary,
                    ownership_quality=gpu.ownership_quality if gpu else "unknown",
                    known_linux_owners=len(gpu.owners)
                    if gpu and gpu.reported_processes is not None
                    else None,
                    reported_processes=gpu.reported_processes if gpu else None,
                    first_source=item.reference,
                    last_source=item.reference,
                    first_compute_observed_at=fact.compute_observed_at,
                    last_compute_observed_at=fact.compute_observed_at,
                    envelopes=1,
                    distinct_compute_samples=sample,
                    repeated_compute_envelopes=int(repeated),
                )
                positions[target] = len(groups)
                keys[target] = key
                groups.append(value)
        if high_compute is None or fact.compute_observed_at > high_compute:
            high_compute = fact.compute_observed_at
            high_signature = signature
        if high_envelope is None or fact.observed_at > high_envelope:
            high_envelope = fact.observed_at
        previous, previous_ref = fact, item.reference
    draft = ActivityPreview.model_construct(
        agent_id=ceiling.query.agent_id,
        node_id=ceiling.query.node_id,
        rule=rule,
        rule_fingerprint=rule.fingerprint,
        source_ceiling_fingerprint=ceiling.fingerprint,
        highest_committed_seq=ceiling.highest_committed_seq,
        requested_after_seq=after,
        last_read_seq=cursor,
        missing_seq=missing,
        range_complete=cursor == ceiling.highest_committed_seq,
        source_pages=tuple(references),
        source_packets=len(items),
        source_characters=source_characters,
        heartbeat_packets=heartbeat,
        telemetry_packets=telemetry,
        input_gpu_facts=input_facts,
        missing_inventory_markers=markers,
        distinct_compute_frames=distinct_frames,
        repeated_compute_frames=repeated_frames,
        no_target_telemetry=no_target,
        groups=tuple(sorted(groups, key=lambda value: (value.first_source.seq, value.gpu_uuid))),
        gaps=tuple(gaps),
        read_at=read_at,
        fingerprint="0" * 64,
    )
    return ActivityPreview.model_validate(
        {**draft.model_dump(mode="json"), "fingerprint": preview_fingerprint(draft)}
    )
