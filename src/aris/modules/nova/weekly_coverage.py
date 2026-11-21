"""Bind an explicit due calendar period to Lyra; no scheduling or usage authority here."""

from datetime import UTC, datetime
from uuid import uuid5

from aris.modules.lyra.coverage_build_schemas import AdmitCoverageWindow
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.source_prefix_schemas import SourcePrefixReference, prefix_excludes_window
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.nova.report_calendar import (
    WeeklyReportPeriod,
    WeeklyReportPlan,
    weekly_report_period,
)


def weekly_coverage_target(
    plan: WeeklyReportPlan,
    period: WeeklyReportPeriod,
    configuration: ProcessingCoverageConfiguration,
    *,
    now: datetime,
    prefix_checkpoint: SourcePrefixReference | None = None,
) -> AdmitCoverageWindow:
    """A caller must still authorize the owner and admit this target through Lyra."""
    plan = WeeklyReportPlan.model_validate(plan.model_dump(mode="json"))
    period = WeeklyReportPeriod.model_validate(period.model_dump(mode="json"))
    configuration = ProcessingCoverageConfiguration.model_validate(
        configuration.model_dump(mode="json")
    )
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("weekly coverage requires an aware scheduling clock")
    now = now.astimezone(UTC)
    if (
        plan.report_type not in ("usage.mine", "usage.node")
        or plan.scope.type != "node"
        or plan.scope.id != configuration.trace.node_id
        or period != weekly_report_period(plan, period.week)
        or period.scheduled_for > now
    ):
        raise ValueError("weekly coverage differs from its explicit due plan and source")
    if prefix_checkpoint is not None:
        prefix_checkpoint = SourcePrefixReference.model_validate(
            prefix_checkpoint.model_dump(mode="json")
        )
        # Only structural context here; admission must load and check the committed reference.
        if not prefix_excludes_window(
            prefix_checkpoint,
            configuration_fingerprint=configuration.trace.fingerprint,
            start_at=period.start_at,
            ceiling=prefix_checkpoint.last_seq,
            read_at=now,
        ):
            raise ValueError("weekly source prefix does not exclude the selected period")
    return AdmitCoverageWindow(
        id=uuid5(period.id, "aris.lyra.weekly-sampled-coverage"),
        query=UsageWindowQuery(
            node_id=plan.scope.id,
            owner_id=plan.owner_id if plan.report_type == "usage.mine" else None,
            start_at=period.start_at,
            end_at=period.end_at,
            timezone=plan.timezone,
        ),
        expected_configuration_fingerprint=configuration.fingerprint,
        prefix_checkpoint=prefix_checkpoint,
    )
