"""Explicit source bindings and current-subject presentation of archive diagnostics."""

from typing import TYPE_CHECKING, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope, ExternalSubject
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.activity_preview import ActivityPreview, ActivityPreviewRule, ActivityState
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.hardware_preview import GpuHardwareSummary, SampleStatistic
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference, UsageArchiveHeadQuery

if TYPE_CHECKING:
    from aris.interfaces.usage.service import UsageHistorySurface


class ActivityPreviewConfig(AccessContract):
    sources: tuple[UsageArchiveHeadQuery, ...] = Field(min_length=1, max_length=32)
    rule: ActivityPreviewRule
    hardware_metrics: bool = False

    @model_validator(mode="after")
    def unique_sources(self) -> "ActivityPreviewConfig":
        if len({source.node_id for source in self.sources}) != len(self.sources) or len(
            {source.agent_id for source in self.sources}
        ) != len(self.sources):
            raise ValueError("activity preview needs one explicit archive source per node")
        return self


class ActivityPreviewAccess:
    def __init__(self, reader: ActivityPreviewService, config: ActivityPreviewConfig) -> None:
        self.reader = reader
        self.config = ActivityPreviewConfig.model_validate(config.model_dump(mode="json"))
        if self.config.hardware_metrics and reader.hardware_source is None:
            raise ValueError("configured hardware metrics require an explicit source reader")
        self.node_ids = tuple(source.node_id for source in self.config.sources)


class ActivityPreviewInput(AccessContract):
    node_id: str = Field(min_length=1, max_length=128, pattern=r"^\S+$")
    max_messages: int = Field(default=128, ge=1, le=1024, strict=True)


class ActivityReferenceView(AccessContract):
    seq: str = Field(pattern=r"^[1-9][0-9]*$")
    message_id: UUID
    fingerprint: str
    observed_at: AwareDatetime
    received_at: AwareDatetime


class ActivityGroupView(AccessContract):
    gpu_uuid: str
    index: int | None
    model: str | None
    state: ActivityState
    reason: str
    boundary: str
    inventory_present: bool
    gpu_quality: str
    compute_quality: str
    compute_counts_consistent: bool
    ownership_quality: str
    known_linux_owners: int | None
    reported_processes: int | None
    first_source: ActivityReferenceView
    last_source: ActivityReferenceView
    first_compute_observed_at: AwareDatetime
    last_compute_observed_at: AwareDatetime
    envelopes: int
    distinct_compute_samples: int
    repeated_compute_envelopes: int


class ActivityGapView(AccessContract):
    before: ActivityReferenceView
    after: ActivityReferenceView


class SampleStatisticView(AccessContract):
    samples: int
    sum: str
    minimum: str | None
    maximum: str | None
    mean_numerator: str | None
    mean_denominator: str | None


class GpuSampleSeriesView(AccessContract):
    gpu_uuid: str
    index: int
    model: str
    memory_total_mib: str
    reported_envelopes: int
    independent_samples: int
    cached_envelopes: int
    rejected_envelopes: int
    missing_temperature_samples: int
    first_source: ActivityReferenceView | None
    last_source: ActivityReferenceView | None
    first_gpu_observed_at: AwareDatetime | None
    last_gpu_observed_at: AwareDatetime | None
    utilization_percent: SampleStatisticView
    memory_used_mib: SampleStatisticView
    temperature_c: SampleStatisticView
    points: tuple["GpuMetricSamplePointView", ...]


class GpuMetricSamplePointView(AccessContract):
    seq: str
    gpu_observed_at: AwareDatetime
    utilization_percent: int
    memory_used_mib: str
    temperature_c: int | None
    break_before: bool


class GpuHardwareSummaryView(AccessContract):
    assessment_scope: Literal["reported_gpu_sample_statistics"]
    aggregation: Literal["independent_sample_weighted"]
    time_weighted: Literal[False]
    continuous_usage_established: Literal[False]
    quota_usage_available: Literal[False]
    source_preview_fingerprint: str
    source_batch_fingerprints: tuple[str, ...]
    source_characters: int
    telemetry_frames: int
    independent_frames: int
    cached_frames: int
    rejected_frames: int
    no_target_frames: int
    rejection_reasons: dict[str, int]
    gpu_quality_frames: dict[str, int]
    series: tuple[GpuSampleSeriesView, ...]
    fingerprint: str


class ActivityPreviewView(AccessContract):
    assessment_scope: Literal["reported_compute_observation_groups"]
    continuous_usage_established: Literal[False]
    quota_usage_available: Literal[False]
    retention_enabled: Literal[False]
    source_packets_preserved: Literal[True]
    prior_source_context: Literal["not_loaded"]
    requested_range_only: Literal[True]
    node_id: str
    agent_id: str
    rule: ActivityPreviewRule
    rule_fingerprint: str
    source_ceiling_fingerprint: str
    highest_committed_seq: str
    requested_after_seq: str
    last_read_seq: str
    missing_seq: str | None
    range_complete: bool
    source_page_fingerprints: tuple[str, ...]
    source_packets: int
    source_characters: int
    telemetry_packets: int
    heartbeat_packets: int
    input_gpu_facts: int
    missing_inventory_markers: int
    distinct_compute_frames: int
    repeated_compute_frames: int
    no_target_telemetry: int
    groups: tuple[ActivityGroupView, ...]
    gaps: tuple[ActivityGapView, ...]
    read_at: AwareDatetime
    source_result_fingerprint: str
    hardware: GpuHardwareSummaryView | None = None


def statistic_view(value: SampleStatistic) -> SampleStatisticView:
    return SampleStatisticView(
        samples=value.samples,
        sum=str(value.sum),
        minimum=str(value.minimum) if value.minimum is not None else None,
        maximum=str(value.maximum) if value.maximum is not None else None,
        mean_numerator=str(value.sum) if value.samples else None,
        mean_denominator=str(value.samples) if value.samples else None,
    )


def hardware_view(value: GpuHardwareSummary) -> GpuHardwareSummaryView:
    return GpuHardwareSummaryView(
        **value.model_dump(exclude={"series"}),
        series=tuple(
            GpuSampleSeriesView(
                **series.model_dump(
                    exclude={
                        "memory_total_mib",
                        "first_source",
                        "last_source",
                        "utilization_percent",
                        "memory_used_mib",
                        "temperature_c",
                        "points",
                    }
                ),
                memory_total_mib=str(series.memory_total_mib),
                first_source=reference_view(series.first_source) if series.first_source else None,
                last_source=reference_view(series.last_source) if series.last_source else None,
                utilization_percent=statistic_view(series.utilization_percent),
                memory_used_mib=statistic_view(series.memory_used_mib),
                temperature_c=statistic_view(series.temperature_c),
                points=tuple(
                    GpuMetricSamplePointView(
                        seq=str(point.seq),
                        gpu_observed_at=point.gpu_observed_at,
                        utilization_percent=point.utilization_percent,
                        memory_used_mib=str(point.memory_used_mib),
                        temperature_c=point.temperature_c,
                        break_before=point.break_before,
                    )
                    for point in series.points
                ),
            )
            for series in value.series
        ),
    )


def reference_view(value: ArchivedUsageReference) -> ActivityReferenceView:
    return ActivityReferenceView(
        seq=str(value.seq),
        message_id=value.message_id,
        fingerprint=value.fingerprint,
        observed_at=value.observed_at,
        received_at=value.received_at,
    )


def activity_view(value: ActivityPreview) -> ActivityPreviewView:
    return ActivityPreviewView(
        **value.model_dump(
            exclude={
                "highest_committed_seq",
                "requested_after_seq",
                "last_read_seq",
                "missing_seq",
                "source_pages",
                "groups",
                "gaps",
                "fingerprint",
                "total_usage_microseconds",
            }
        ),
        highest_committed_seq=str(value.highest_committed_seq),
        requested_after_seq=str(value.requested_after_seq),
        last_read_seq=str(value.last_read_seq),
        missing_seq=str(value.missing_seq) if value.missing_seq is not None else None,
        source_page_fingerprints=tuple(page.fingerprint for page in value.source_pages),
        groups=tuple(
            ActivityGroupView(
                **group.model_dump(exclude={"first_source", "last_source"}),
                first_source=reference_view(group.first_source),
                last_source=reference_view(group.last_source),
            )
            for group in value.groups
        ),
        gaps=tuple(
            ActivityGapView(before=reference_view(gap.before), after=reference_view(gap.after))
            for gap in value.gaps
        ),
        source_result_fingerprint=value.fingerprint,
    )


def _current_subject(
    surface: "UsageHistorySurface", subject: ExternalSubject, node_id: str
) -> UUID:
    with surface.history.sessions() as session, session.no_autoflush:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        transaction_budget(session, 100, 750, 1500)
        principal = surface.history.access.resolve_external_in_transaction(session, subject)
        if principal is None:
            raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
        surface.history.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=principal.user_id,
                permission="usage.read_all",
                scope=AccessScope(type="node", id=node_id),
            ),
        )
        return principal.user_id


def activity_usage(
    surface: "UsageHistorySurface",
    authorization: str | None,
    request_id: str,
    query: ActivityPreviewInput,
) -> ActivityPreviewView:
    subject = surface.authenticator.authenticate(authorization)
    query = ActivityPreviewInput.model_validate(query.model_dump(mode="json"))
    access = surface.activity
    if access is None:
        raise DomainError("USAGE_NOT_CONFIGURED", "Archive preview is not enabled", 503)
    if query.node_id not in surface.node_ids or query.node_id not in access.node_ids:
        raise DomainError("PERMISSION_DENIED", "Node is outside this archive interface", 403)
    try:
        user = _current_subject(surface, subject, query.node_id)
        source = next(item for item in access.config.sources if item.node_id == query.node_id)
        actor = ActorContext(actor_id=user, request_id=request_id)
        if access.config.hardware_metrics:
            combined = access.reader.recent_with_hardware(
                actor, source, access.config.rule, max_messages=query.max_messages
            )
            result = activity_view(combined.preview).model_copy(
                update={"hardware": hardware_view(combined.hardware)}
            )
        else:
            result = activity_view(
                access.reader.recent(
                    actor, source, access.config.rule, max_messages=query.max_messages
                )
            )
        if _current_subject(surface, subject, query.node_id) != user:
            raise DomainError("PERMISSION_DENIED", "Current identity differs", 403)
        return result
    except DBAPIError as error:
        code = database_failure(error)
        raise DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Activity identity database is unavailable",
            503,
        ) from None
