"""Sample-weighted GPU statistics; no interpolation, continuous use, or retention action."""

import hashlib
import json
from collections import Counter
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.core.contracts import DomainError
from aris.modules.lyra.activity_preview import (
    MAX_PREVIEW_CHARACTERS,
    MAX_PREVIEW_GROUPS,
    ActivityPreview,
)
from aris.modules.lyra.reporting_gaps import ReportingGapPreview
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.gpu_hardware_schemas import GpuHardwareBatch, GpuHardwareObservation
from aris.modules.vega.schemas import GpuSample
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference, UsageArchivePage


class SampleStatistic(IntervalContract):
    samples: int = Field(ge=0, le=1024)
    sum: int
    minimum: int | None
    maximum: int | None

    @model_validator(mode="after")
    def observed_values(self) -> "SampleStatistic":
        if self.samples == 0:
            if self.sum != 0 or self.minimum is not None or self.maximum is not None:
                raise ValueError("absent sample statistics cannot contain observed values")
        elif (
            self.minimum is None
            or self.maximum is None
            or self.minimum > self.maximum
            or not self.minimum * self.samples <= self.sum <= self.maximum * self.samples
        ):
            raise ValueError("sample statistic is inconsistent")
        return self


class GpuMetricSamplePoint(IntervalContract):
    seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    gpu_observed_at: AwareDatetime
    utilization_percent: int = Field(ge=0, le=100)
    memory_used_mib: int = Field(ge=0)
    temperature_c: int | None = Field(ge=-50, le=150)
    break_before: bool


class GpuSampleSeries(IntervalContract):
    gpu_uuid: str
    index: int
    model: str
    memory_total_mib: int = Field(gt=0)
    reported_envelopes: int = Field(ge=1, le=1024)
    independent_samples: int = Field(ge=0, le=1024)
    cached_envelopes: int = Field(ge=0, le=1024)
    rejected_envelopes: int = Field(ge=0, le=1024)
    missing_temperature_samples: int = Field(ge=0, le=1024)
    first_source: ArchivedUsageReference | None
    last_source: ArchivedUsageReference | None
    first_gpu_observed_at: AwareDatetime | None
    last_gpu_observed_at: AwareDatetime | None
    utilization_percent: SampleStatistic
    memory_used_mib: SampleStatistic
    temperature_c: SampleStatistic
    points: tuple[GpuMetricSamplePoint, ...] = Field(max_length=1024)

    @model_validator(mode="after")
    def sample_counts(self) -> "GpuSampleSeries":
        if (
            self.independent_samples + self.cached_envelopes + self.rejected_envelopes
            != self.reported_envelopes
            or self.utilization_percent.samples != self.independent_samples
            or self.memory_used_mib.samples != self.independent_samples
            or self.temperature_c.samples + self.missing_temperature_samples
            != self.independent_samples
            or len(self.points) != self.independent_samples
            or (bool(self.points) and not self.points[0].break_before)
            or any(point.memory_used_mib > self.memory_total_mib for point in self.points)
            or any(
                left.seq >= right.seq or left.gpu_observed_at >= right.gpu_observed_at
                for left, right in zip(self.points, self.points[1:], strict=False)
            )
            or (
                self.independent_samples == 0
                and any(
                    value is not None
                    for value in (
                        self.first_source,
                        self.last_source,
                        self.first_gpu_observed_at,
                        self.last_gpu_observed_at,
                    )
                )
            )
            or (
                self.independent_samples > 0
                and any(
                    value is None
                    for value in (
                        self.first_source,
                        self.last_source,
                        self.first_gpu_observed_at,
                        self.last_gpu_observed_at,
                    )
                )
            )
        ):
            raise ValueError("GPU series differs from its independent source samples")
        if self.points and (
            self.first_source is None
            or self.last_source is None
            or self.first_source.seq != self.points[0].seq
            or self.last_source.seq != self.points[-1].seq
            or self.first_gpu_observed_at != self.points[0].gpu_observed_at
            or self.last_gpu_observed_at != self.points[-1].gpu_observed_at
            or self.utilization_percent
            != _stat([point.utilization_percent for point in self.points])
            or self.memory_used_mib != _stat([point.memory_used_mib for point in self.points])
            or self.temperature_c
            != _stat(
                [point.temperature_c for point in self.points if point.temperature_c is not None]
            )
        ):
            raise ValueError("GPU summary and original chart points differ")
        return self


class GpuHardwareSummary(IntervalContract):
    assessment_scope: Literal["reported_gpu_sample_statistics"] = "reported_gpu_sample_statistics"
    aggregation: Literal["independent_sample_weighted"] = "independent_sample_weighted"
    time_weighted: Literal[False] = False
    continuous_usage_established: Literal[False] = False
    quota_usage_available: Literal[False] = False
    source_preview_fingerprint: str
    source_batch_fingerprints: tuple[str, ...] = Field(max_length=8)
    source_characters: int = Field(ge=0, le=MAX_PREVIEW_CHARACTERS)
    telemetry_frames: int = Field(ge=0, le=1024)
    independent_frames: int = Field(ge=0, le=1024)
    cached_frames: int = Field(ge=0, le=1024)
    rejected_frames: int = Field(ge=0, le=1024)
    no_target_frames: int = Field(ge=0, le=1024)
    rejection_reasons: dict[str, int]
    gpu_quality_frames: dict[str, int]
    series: tuple[GpuSampleSeries, ...] = Field(max_length=MAX_PREVIEW_GROUPS)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def coherent_counts(self) -> "GpuHardwareSummary":
        if (
            self.independent_frames + self.cached_frames + self.rejected_frames
            != self.telemetry_frames
            or sum(self.rejection_reasons.values()) != self.rejected_frames
            or sum(self.gpu_quality_frames.values()) != self.telemetry_frames
            or self.fingerprint != hardware_summary_fingerprint(self)
        ):
            raise ValueError("hardware summary differs from its classified source frames")
        return self


class SamplingIncidentPoint(IntervalContract):
    source_seq: str = Field(pattern=r"^[1-9][0-9]{0,18}$")
    source_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    sampled_at: AwareDatetime


class GpuSamplingIncident(IntervalContract):
    problem: Literal["gpu_sampling_error"] = "gpu_sampling_error"
    first_error: SamplingIncidentPoint
    last_error: SamplingIncidentPoint
    independent_errors: int = Field(ge=1, le=1024)
    outcome: Literal["recovered", "observation_gap", "range_end"]
    recovery: SamplingIncidentPoint | None = None

    @model_validator(mode="after")
    def observed_boundaries(self) -> "GpuSamplingIncident":
        if (
            self.first_error.sampled_at > self.last_error.sampled_at
            or int(self.first_error.source_seq) > int(self.last_error.source_seq)
            or (self.outcome == "recovered") != (self.recovery is not None)
            or (
                self.recovery is not None
                and (
                    self.recovery.sampled_at <= self.last_error.sampled_at
                    or int(self.recovery.source_seq) <= int(self.last_error.source_seq)
                )
            )
        ):
            raise ValueError("Incident does not match its observed error and recovery bounds")
        return self


class ActivityHardwarePreview(IntervalContract):
    preview: ActivityPreview
    hardware: GpuHardwareSummary
    sampling_incidents: tuple[GpuSamplingIncident, ...] | None = None
    reporting: ReportingGapPreview | None = None

    @model_validator(mode="after")
    def same_source(self) -> "ActivityHardwarePreview":
        if self.hardware.source_preview_fingerprint != self.preview.fingerprint:
            raise ValueError("hardware statistics refer to another archive preview")
        if self.reporting is not None and (
            self.reporting.source_preview_fingerprint != self.preview.fingerprint
            or self.reporting.source_range_complete != self.preview.range_complete
        ):
            raise ValueError("reporting gaps refer to another archive preview")
        return self


def hardware_summary_fingerprint(value: GpuHardwareSummary) -> str:
    raw = json.dumps(
        value.model_dump(mode="json", exclude={"fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.lyra.hardware.sample.summary\x00" + raw.encode()).hexdigest()


def _signature(value: GpuHardwareObservation) -> tuple[object, ...]:
    return (
        value.gpu_quality,
        tuple(
            (
                gpu.gpu_uuid,
                gpu.index,
                gpu.model,
                gpu.memory_total_mib,
                gpu.memory_used_mib,
                gpu.utilization_percent,
                gpu.temperature_c,
            )
            for gpu in sorted(value.gpus, key=lambda gpu: gpu.gpu_uuid)
        ),
    )


def _classify(values: tuple[GpuHardwareObservation, ...], gap: timedelta) -> list[str]:
    states: list[str] = []
    high_gpu = high_envelope = None
    high_signature: tuple[object, ...] | None = None
    at_high: list[int] = []
    quarantine = conflict = False
    for index, value in enumerate(values):
        component, envelope = value.gpu_observed_at, value.reference.observed_at
        signature = _signature(value)
        repeated = high_gpu is not None and component == high_gpu
        if (high_gpu is not None and component < high_gpu) or (
            high_envelope is not None and envelope < high_envelope
        ):
            quarantine = True
        if (high_gpu is None or component > high_gpu) and (
            high_envelope is None or envelope > high_envelope
        ):
            quarantine = conflict = False
        if repeated and signature != high_signature:
            conflict = True
            for previous in at_high:
                if states[previous] != "SOURCE_CLOCK_REGRESSION":
                    states[previous] = "SAME_TIME_HARDWARE_CONFLICT"
        reason = (
            "SOURCE_CLOCK_REGRESSION"
            if quarantine
            else "SAME_TIME_HARDWARE_CONFLICT"
            if conflict
            else "GPU_COMPONENT_STALE"
            if envelope - component > gap
            else "GPU_COMPONENT_UNAVAILABLE"
            if value.gpu_quality != "ok"
            else "cached"
            if repeated
            else "independent"
        )
        states.append(reason)
        if high_gpu is None or component > high_gpu:
            high_gpu, high_signature, at_high = component, signature, [index]
        elif repeated:
            at_high.append(index)
        if high_envelope is None or envelope > high_envelope:
            high_envelope = envelope
    return states


def sampling_incidents(
    preview: ActivityPreview, batches: tuple[GpuHardwareBatch, ...]
) -> tuple[GpuSamplingIncident, ...]:
    """Use verified hardware batches already selected by the hardware preview."""
    values = tuple(value for batch in batches for value in batch.observations)
    gap = timedelta(seconds=preview.rule.max_gap_seconds)
    incidents: list[GpuSamplingIncident] = []
    errors: list[SamplingIncidentPoint] = []
    last_time = None

    def finish(
        outcome: Literal["recovered", "observation_gap", "range_end"],
        recovery: SamplingIncidentPoint | None = None,
    ) -> None:
        if errors:
            incidents.append(
                GpuSamplingIncident(
                    first_error=errors[0],
                    last_error=errors[-1],
                    independent_errors=len(errors),
                    outcome=outcome,
                    recovery=recovery,
                )
            )
            errors.clear()

    for value, state in zip(values, _classify(values, gap), strict=True):
        bad = state == "GPU_COMPONENT_UNAVAILABLE" and value.gpu_quality == "error"
        if state == "cached":
            continue
        if not bad and state != "independent":
            finish("observation_gap")
            last_time = None
            continue
        sample_time = value.gpu_observed_at
        if sample_time == last_time:
            continue
        if last_time is not None and sample_time - last_time > gap:
            finish("observation_gap")
        last_time = sample_time
        point = SamplingIncidentPoint(
            source_seq=str(value.reference.seq),
            source_fingerprint=value.reference.fingerprint,
            sampled_at=sample_time,
        )
        if bad:
            errors.append(point)
        else:
            finish("recovered", point)
    finish("range_end" if preview.range_complete else "observation_gap")
    return tuple(incidents)


def _stat(values: list[int]) -> SampleStatistic:
    return SampleStatistic(
        samples=len(values),
        sum=sum(values),
        minimum=min(values) if values else None,
        maximum=max(values) if values else None,
    )


def summarize_gpu_samples(
    preview: ActivityPreview,
    pages: tuple[UsageArchivePage, ...],
    batches: tuple[GpuHardwareBatch, ...],
) -> GpuHardwareSummary:
    preview = ActivityPreview.model_validate(preview.model_dump(mode="json"))
    pages = tuple(UsageArchivePage.model_validate(page.model_dump(mode="json")) for page in pages)
    batches = tuple(
        GpuHardwareBatch.model_validate(batch.model_dump(mode="json")) for batch in batches
    )
    expected = tuple(
        item.reference for page in pages for item in page.items if item.observation is not None
    )
    values = tuple(value for batch in batches for value in batch.observations)
    if (
        len(batches) > 8
        or len(values) > 1024
        or tuple(value.reference for value in values) != expected
        or len(expected) != preview.telemetry_packets
        or tuple(page.fingerprint for page in pages)
        != tuple(page.fingerprint for page in preview.source_pages)
        or sum(batch.source_characters for batch in batches) > MAX_PREVIEW_CHARACTERS
    ):
        raise DomainError("CONFLICT", "Hardware samples differ from the fixed archive preview", 409)
    states = _classify(values, timedelta(seconds=preview.rule.max_gap_seconds))
    series: dict[
        tuple[str, int, str, int], list[tuple[GpuSample, GpuHardwareObservation, str, bool]]
    ] = {}
    last_keys: dict[str, tuple[str, int, str, int]] = {}
    barriers: dict[str, bool] = {}
    for value, state in zip(values, states, strict=True):
        present = {gpu.gpu_uuid for gpu in value.gpus}
        for known in last_keys:
            if known not in present or state not in ("independent", "cached"):
                barriers[known] = True
        for gpu in value.gpus:
            key = (gpu.gpu_uuid, gpu.index, gpu.model, gpu.memory_total_mib)
            changed = last_keys.get(gpu.gpu_uuid) != key
            series.setdefault(key, []).append(
                (gpu, value, state, changed or barriers.get(gpu.gpu_uuid, True))
            )
            last_keys[gpu.gpu_uuid] = key
            if state == "independent":
                barriers[gpu.gpu_uuid] = False
            elif state != "cached":
                barriers[gpu.gpu_uuid] = True
            if len(series) > MAX_PREVIEW_GROUPS:
                raise DomainError(
                    "ACTIVITY_PREVIEW_TOO_LARGE", "GPU sample series exceed preview budget", 503
                )
    output = []
    for key, samples in sorted(series.items()):
        accepted = [
            (gpu, value, broken) for gpu, value, state, broken in samples if state == "independent"
        ]
        temperatures = [
            gpu.temperature_c for gpu, _, _ in accepted if gpu.temperature_c is not None
        ]
        points = tuple(
            GpuMetricSamplePoint(
                seq=value.reference.seq,
                gpu_observed_at=value.gpu_observed_at,
                utilization_percent=gpu.utilization_percent,
                memory_used_mib=gpu.memory_used_mib,
                temperature_c=gpu.temperature_c,
                break_before=index == 0
                or broken
                or (
                    value.gpu_observed_at - accepted[index - 1][1].gpu_observed_at
                    > timedelta(seconds=preview.rule.max_gap_seconds)
                ),
            )
            for index, (gpu, value, broken) in enumerate(accepted)
        )
        output.append(
            GpuSampleSeries(
                gpu_uuid=key[0],
                index=key[1],
                model=key[2],
                memory_total_mib=key[3],
                reported_envelopes=len(samples),
                independent_samples=len(accepted),
                cached_envelopes=sum(state == "cached" for _, _, state, _ in samples),
                rejected_envelopes=sum(
                    state not in ("independent", "cached") for _, _, state, _ in samples
                ),
                missing_temperature_samples=len(accepted) - len(temperatures),
                first_source=accepted[0][1].reference if accepted else None,
                last_source=accepted[-1][1].reference if accepted else None,
                first_gpu_observed_at=accepted[0][1].gpu_observed_at if accepted else None,
                last_gpu_observed_at=accepted[-1][1].gpu_observed_at if accepted else None,
                utilization_percent=_stat([gpu.utilization_percent for gpu, _, _ in accepted]),
                memory_used_mib=_stat([gpu.memory_used_mib for gpu, _, _ in accepted]),
                temperature_c=_stat(temperatures),
                points=points,
            )
        )
    rejected = Counter(state for state in states if state not in ("independent", "cached"))
    draft = GpuHardwareSummary.model_construct(
        source_preview_fingerprint=preview.fingerprint,
        source_batch_fingerprints=tuple(batch.fingerprint for batch in batches),
        source_characters=sum(batch.source_characters for batch in batches),
        telemetry_frames=len(values),
        independent_frames=states.count("independent"),
        cached_frames=states.count("cached"),
        rejected_frames=sum(rejected.values()),
        no_target_frames=sum(not value.gpus for value in values),
        rejection_reasons=dict(sorted(rejected.items())),
        gpu_quality_frames=dict(sorted(Counter(value.gpu_quality for value in values).items())),
        series=tuple(output),
        fingerprint="0" * 64,
    )
    return GpuHardwareSummary.model_validate(
        {**draft.model_dump(mode="json"), "fingerprint": hardware_summary_fingerprint(draft)}
    )
