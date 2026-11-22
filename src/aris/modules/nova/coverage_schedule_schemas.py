"""An explicit calendar cursor advances only after a linked historical snapshot exists."""

import hashlib
from datetime import date
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from aris.core.access.schemas import AccessContract
from aris.core.scheduler.schemas import canonical
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.weekly_from_daily import WeeklyFromDaily
from aris.modules.nova.report_calendar import WeeklyReportPlan, weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target
from aris.modules.nova.weekly_plan_schemas import plan_key


def schedule_canonical(value: AccessContract, *, exclude: set[str] | None = None) -> str:
    return canonical(value.model_dump(mode="json", exclude=exclude))


def schedule_fingerprint(kind: str, raw: str) -> str:
    return hashlib.sha256(("aris.nova." + kind + "\x00" + raw).encode()).hexdigest()


class CoverageScheduleDefinition(AccessContract):
    calendar: WeeklyReportPlan
    configuration: ProcessingCoverageConfiguration
    daily_rollups: Literal[True] | None = None

    @model_validator(mode="after")
    def one_explicit_source(self) -> "CoverageScheduleDefinition":
        first = weekly_report_period(self.calendar, self.calendar.first_week)
        weekly_coverage_target(self.calendar, first, self.configuration, now=first.scheduled_for)
        if self.daily_rollups and self.calendar.report_type != "usage.node":
            raise ValueError("Daily rollups require a node-wide coverage definition")
        return self

    @model_serializer(mode="wrap")
    def serialize_source(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        values = cast(dict[str, Any], handler(self))
        if self.daily_rollups is None:
            values.pop("daily_rollups", None)
        return values

    @property
    def id(self) -> UUID:
        method = "weekly-from-daily/" if self.daily_rollups else "weekly-coverage/"
        return uuid5(NAMESPACE_URL, "aris.nova." + method + plan_key(self.calendar))

    @property
    def fingerprint(self) -> str:
        return schedule_fingerprint("weekly_coverage_definition", schedule_canonical(self))


class CoverageScheduleStatus(AccessContract):
    definition: CoverageScheduleDefinition
    next_week: date
    revision: int = Field(ge=1, strict=True)
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_result_id: UUID | None = None
    cursor_meaning: Literal["coverage_snapshots_saved"] = "coverage_snapshots_saved"

    @model_validator(mode="after")
    def calendar_progress(self) -> "CoverageScheduleStatus":
        weekly_report_period(self.definition.calendar, self.next_week)
        if self.updated_at < self.created_at or (self.last_result_id is None) != (
            self.next_week == self.definition.calendar.first_week
        ):
            raise ValueError("coverage schedule progress is inconsistent")
        return self


class CoveragePeriodResult(AccessContract):
    id: UUID
    schedule_id: UUID
    definition_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    week: date
    snapshot_id: UUID
    snapshot_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage: Literal["complete", "partial", "unknown"]
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)
    created_at: AwareDatetime
    event_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_quota_authorization: Literal[False] = False

    @model_validator(mode="after")
    def honest_totals(self) -> "CoveragePeriodResult":
        if (
            self.week.weekday() != 0
            or self.verified_usage_microseconds > self.recorded_microseconds
            or self.total_usage_microseconds
            != (self.verified_usage_microseconds if self.coverage == "complete" else None)
            or self.fingerprint != self.content_fingerprint()
        ):
            raise ValueError("coverage period result differs from its fixed snapshot")
        return self

    def content_fingerprint(self) -> str:
        return schedule_fingerprint(
            "weekly_coverage_result", schedule_canonical(self, exclude={"fingerprint"})
        )


class DailyCoveragePeriodResult(AccessContract):
    id: UUID
    schedule_id: UUID
    definition_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    week: date
    report: WeeklyFromDaily
    created_at: AwareDatetime
    event_id: UUID
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_quota_authorization: Literal[False] = False

    @model_validator(mode="after")
    def exact_report(self) -> "DailyCoveragePeriodResult":
        if (
            self.id != uuid5(self.schedule_id, self.week.isoformat())
            or self.week != self.report.query.week
            or self.report.end_at > self.created_at
            or self.report.missing_days
            or self.report.fingerprint != self.report.content_fingerprint()
            or self.fingerprint != self.content_fingerprint()
        ):
            raise ValueError("Weekly result requires the exact closed daily report")
        return self

    def content_fingerprint(self) -> str:
        return schedule_fingerprint(
            "weekly_daily_coverage_result", schedule_canonical(self, exclude={"fingerprint"})
        )
