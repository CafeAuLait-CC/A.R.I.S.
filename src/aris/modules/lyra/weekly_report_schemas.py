"""Immutable weekly artifacts of recorded use; they never assert complete usage."""

from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from aris.core.access.schemas import AccessContract, UserPrincipal
from aris.modules.lyra.ledger_schemas import canonical_ledger, ledger_fingerprint
from aris.modules.lyra.usage_history import UsageHistory
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_WEEKLY_REPORT_BYTES = 4194304


class WeeklyReportRequest(AccessContract):
    mode: Literal["mine", "node"]
    week: date
    timezone: str = Field(min_length=1, max_length=128)
    start_at: AwareDatetime
    end_at: AwareDatetime

    @model_validator(mode="after")
    def calendar(self) -> "WeeklyReportRequest":
        UserPrincipal.iana_timezone(self.timezone)
        zone = ZoneInfo(self.timezone)
        if self.week.weekday() != 0 or (self.start_at, self.end_at) != (
            datetime.combine(self.week, time.min, zone).astimezone(UTC),
            datetime.combine(self.week + timedelta(days=7), time.min, zone).astimezone(UTC),
        ):
            raise ValueError("weekly report must cover one complete local Monday week")
        return self


class StoredWeeklyReport(IntervalContract):
    id: UUID
    event_id: UUID
    requested_by: UUID
    task_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    request: WeeklyReportRequest
    created_at: AwareDatetime
    history: UsageHistory
    labels: dict[str, str] = Field(max_length=129)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> "StoredWeeklyReport":
        history = self.history
        owners = {str(owner.owner_id) for owner in history.owners}
        if history.owner_id is not None:
            owners.add(str(history.owner_id))
        if (
            (history.start_at, history.end_at, history.timezone)
            != (self.request.start_at, self.request.end_at, self.request.timezone)
            or history.owner_id != (self.requested_by if self.request.mode == "mine" else None)
            or self.created_at < self.request.end_at
            or history.read_at > self.created_at
            or not self.labels.keys() <= owners
            or any(len(value) > 128 for value in self.labels.values())
        ):
            raise ValueError("weekly report differs from its scope and period")
        return self

    def content_fingerprint(self) -> str:
        return ledger_fingerprint("weekly_report", canonical_ledger(self, exclude={"fingerprint"}))


class WeeklyReportSummary(AccessContract):
    id: UUID
    node_id: str
    owner_id: UUID | None
    week: date
    timezone: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    created_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0)
    ledger_revision: int = Field(ge=0)
    assessment_scope: Literal["recorded_usage_only"] = "recorded_usage_only"
    source_coverage: Literal["unknown"] = "unknown"


class WeeklyReportCursor(AccessContract):
    created_at: AwareDatetime
    id: UUID


class WeeklyReportPage(AccessContract):
    items: tuple[WeeklyReportSummary, ...]
    next_cursor: WeeklyReportCursor | None
