"""Generate a fixed natural-week report from consolidated day evidence."""

from collections import defaultdict
from datetime import date, timedelta
from time import monotonic
from typing import Any, Literal, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import (
    AwareDatetime,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.access.schemas import UserPrincipal
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.modules.lyra.coverage_rollup import (
    CoverageDuration,
    CoverageState,
    SavedCoverageRollup,
    roll_up_saved_coverage,
)
from aris.modules.lyra.daily_rollup_repo import DailyUsageRollupRepository
from aris.modules.lyra.daily_session_occupancy import RetainedSessionGap
from aris.modules.lyra.daily_session_occupancy_service import DailySessionOccupancyService
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_schemas import MAX_WINDOW_CELLS, UsageWindowQuery, WindowCoverageCell
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository


class WeeklyFromDailyQuery(IntervalContract):
    node_id: str = Field(min_length=1, max_length=128)
    week: date
    timezone: str = Field(min_length=1, max_length=128)

    @field_validator("week")
    @classmethod
    def monday(cls, value: date) -> date:
        if value.weekday() != 0:
            raise ValueError("A report week must start on Monday")
        return value

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        return UserPrincipal.iana_timezone(value)


class DailyWindowReference(IntervalContract):
    snapshot_id: UUID
    snapshot_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class WeeklySourceDay(DailyWindowReference):
    day: date
    rollup_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    coverage: Literal["complete", "partial", "unknown"]


class SavedDailyTimeline(IntervalContract):
    reference: WeeklySourceDay
    query: UsageWindowQuery
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)
    intervals: tuple[WindowCoverageCell, ...] = Field(max_length=MAX_WINDOW_CELLS)
    verified_short_gaps: tuple[RetainedSessionGap, ...] | None = Field(default=None, max_length=64)
    quota_usage_available: Literal[False] = False


class WeeklyRetainedUsage(IntervalContract):
    gpu_uuid: str
    owner_id: UUID
    microseconds: int = Field(gt=0)


class WeeklySessionEvidence(IntervalContract):
    assessed_days: int = Field(ge=1, le=7)
    adjacent_context_days: int = Field(ge=0, le=7)
    unverified_gap_microseconds: int = Field(ge=0)
    retained_microseconds: int = Field(ge=0)
    verified_session_microseconds: int = Field(ge=0)
    retained: tuple[WeeklyRetainedUsage, ...] = Field(max_length=448)

    @model_validator(mode="after")
    def exact_retained_time(self) -> "WeeklySessionEvidence":
        if (
            self.adjacent_context_days > self.assessed_days
            or self.retained_microseconds != sum(item.microseconds for item in self.retained)
            or len({(item.gpu_uuid, item.owner_id) for item in self.retained}) != len(self.retained)
        ):
            raise ValueError("Weekly session summary differs from its verified daily gaps")
        return self


class WeeklyFromDaily(IntervalContract):
    assessment_scope: Literal["weekly_from_daily_coverage"] = "weekly_from_daily_coverage"
    quota_usage_available: Literal[False] = False
    query: WeeklyFromDailyQuery
    start_at: AwareDatetime
    end_at: AwareDatetime
    days: tuple[WeeklySourceDay, ...] = Field(max_length=7)
    missing_days: tuple[date, ...] = Field(max_length=7)
    coverage: Literal["complete", "partial", "unknown"]
    recorded_microseconds: int = Field(ge=0)
    verified_usage_microseconds: int = Field(ge=0)
    total_usage_microseconds: int | None = Field(ge=0)
    durations: tuple[CoverageDuration, ...]
    session_evidence: WeeklySessionEvidence | None = None
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_serializer(mode="wrap")
    def optional_sessions(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data = cast(dict[str, Any], handler(self))
        if self.session_evidence is None:
            data.pop("session_evidence", None)
        return data

    @model_validator(mode="after")
    def exact_session_time(self) -> "WeeklyFromDaily":
        summary = self.session_evidence
        if summary is not None and (
            summary.assessed_days > len(self.days)
            or summary.verified_session_microseconds
            != self.verified_usage_microseconds + summary.retained_microseconds
        ):
            raise ValueError("Verified session time must be residency plus verified gaps")
        return self

    def content_fingerprint(self) -> str:
        return trace_fingerprint(
            "weekly_from_daily_coverage", canonical_trace(self, exclude={"fingerprint"})
        )


def weekly_from_daily(
    query: WeeklyFromDailyQuery, days: tuple[SavedCoverageRollup, ...]
) -> WeeklyFromDaily:
    query = WeeklyFromDailyQuery.model_validate(query.model_dump())
    if len(days) > 7:
        raise ValueError("A week can reference at most seven daily results")
    boundaries = calendar_boundaries(query.week, query.week + timedelta(days=6), query.timezone)
    dates = {query.week + timedelta(days=i): (boundaries[i], boundaries[i + 1]) for i in range(7)}
    seen: dict[date, SavedCoverageRollup] = {}
    totals: dict[tuple[str, CoverageState, UUID | None], int] = defaultdict(int)
    for day in days:
        if (
            day.grain != "day"
            or day.detail_scope != "node"
            or day.bucket_coverage != "node_cells"
            or len(day.buckets) != 1
            or day.query.owner_id is not None
            or (day.query.node_id, day.query.timezone) != (query.node_id, query.timezone)
            or day.fingerprint != day.content_fingerprint()
        ):
            raise ValueError("Daily source has the wrong scope or fingerprint")
        (bucket,) = day.buckets
        key = bucket.local_period_start
        if (
            key in seen
            or dates.get(key) != (bucket.start_at, bucket.end_at)
            or (bucket.start_at, bucket.end_at) != (day.query.start_at, day.query.end_at)
        ):
            raise ValueError(
                "Daily sources must uniquely cover full local dates in the chosen week"
            )
        verified = sum(row.microseconds for row in bucket.durations if row.state == "recorded")
        if (
            day.verified_usage_microseconds != verified
            or verified > day.recorded_microseconds
            or day.total_usage_microseconds != (verified if day.coverage == "complete" else None)
            or (
                day.coverage == "complete"
                and (
                    not day.source_prefix_complete
                    or any(row.state in ("unknown", "uncovered") for row in bucket.durations)
                )
            )
        ):
            raise ValueError("Daily source coverage or usage totals are inconsistent")
        seen[key] = day
        for row in bucket.durations:
            totals[(row.gpu_uuid, row.state, row.owner_id)] += row.microseconds
    if len({day.snapshot_id for day in seen.values()}) != len(seen):
        raise ValueError("Each daily source requires a distinct immutable snapshot")
    missing = tuple(day for day, (start, end) in dates.items() if day not in seen and start < end)
    complete = (
        bool(seen) and not missing and all(day.coverage == "complete" for day in seen.values())
    )
    known = any(day.coverage != "unknown" for day in seen.values())
    recorded = sum(day.recorded_microseconds for day in seen.values())
    verified = sum(day.verified_usage_microseconds for day in seen.values())
    daily_sessions = tuple(
        day.daily_session for day in seen.values() if day.daily_session is not None
    )
    by_gpu_owner: dict[tuple[str, UUID], int] = defaultdict(int)
    for daily_session in daily_sessions:
        for gap in daily_session.retained_gaps:
            by_gpu_owner[(gap.gpu_uuid, gap.owner_id)] += gap.microseconds
    retained = sum(by_gpu_owner.values())
    sessions = (
        WeeklySessionEvidence(
            assessed_days=len(daily_sessions),
            adjacent_context_days=sum(day.adjacent_context_loaded for day in daily_sessions),
            unverified_gap_microseconds=sum(
                day.unverified_gap_microseconds for day in daily_sessions
            ),
            retained_microseconds=retained,
            verified_session_microseconds=verified + retained,
            retained=tuple(
                WeeklyRetainedUsage(gpu_uuid=gpu, owner_id=owner, microseconds=amount)
                for (gpu, owner), amount in sorted(by_gpu_owner.items())
            ),
        )
        if daily_sessions
        else None
    )
    result = WeeklyFromDaily(
        query=query,
        start_at=boundaries[0],
        end_at=boundaries[-1],
        days=tuple(
            WeeklySourceDay(
                day=key,
                snapshot_id=day.snapshot_id,
                snapshot_fingerprint=day.snapshot_fingerprint,
                rollup_fingerprint=day.fingerprint,
                coverage=day.coverage,
            )
            for key, day in sorted(seen.items())
        ),
        missing_days=missing,
        coverage="complete" if complete else "partial" if known else "unknown",
        recorded_microseconds=recorded,
        verified_usage_microseconds=verified,
        total_usage_microseconds=verified if complete else None,
        durations=tuple(
            CoverageDuration(gpu_uuid=gpu, state=state, owner_id=owner, microseconds=amount)
            for (gpu, state, owner), amount in sorted(
                totals.items(), key=lambda value: (value[0][0], value[0][1], str(value[0][2]))
            )
        ),
        session_evidence=sessions,
        fingerprint="0" * 64,
    )
    return result.model_copy(update={"fingerprint": result.content_fingerprint()})


class WeeklyFromDailyReader:
    def __init__(
        self,
        daily_windows: UsageWindowReader,
        session_occupancy: DailySessionOccupancyService | None = None,
    ) -> None:
        self.daily_windows = daily_windows
        self.session_occupancy = session_occupancy

    def _begin(self, db: Session, actor: ActorContext, node_id: str) -> None:
        timeout_ms = self.daily_windows.timeout_ms
        transaction_budget(db, 100, min(1000, timeout_ms), timeout_ms)
        self.daily_windows.require_in_transaction(db, actor, node_id, None, "node")

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Daily statistics database is unavailable",
            503,
        )

    def stored_references(
        self, actor: ActorContext, node_id: str, ids: tuple[UUID, ...]
    ) -> dict[UUID, str]:
        if not 1 <= len(ids) <= 7 or len(set(ids)) != len(ids):
            raise ValueError("Expected up to seven distinct day rollups")
        reader = self.daily_windows
        try:
            with reader.sessions() as db, db.no_autoflush:
                self._begin(db, actor, node_id)
                values = DailyUsageRollupRepository(db).fingerprints(node_id, ids)
                reader.require_in_transaction(db, actor, node_id, None, "node")
                return values
        except DBAPIError as error:
            raise self._failure(error) from None

    def timeline_in_transaction(
        self,
        db: Session,
        actor: ActorContext,
        query: UsageWindowQuery,
        source: WeeklySourceDay,
    ) -> SavedDailyTimeline:
        self._begin(db, actor, query.node_id)
        value = (
            DailyUsageRollupRepository(db)
            .get(query.node_id, (source.snapshot_id,))
            .get(source.snapshot_id)
        )
        if value is None:
            raise DomainError("NOT_FOUND", "Daily rollup has expired", 404)
        if (
            value.query != query
            or value.snapshot_fingerprint != source.snapshot_fingerprint
            or value.fingerprint != source.rollup_fingerprint
            or value.coverage != source.coverage
        ):
            raise DomainError("CONFLICT", "Daily timeline differs from the report", 409)
        intervals = self.daily_windows.read_in_transaction(
            db, actor, source.snapshot_id, node_id=query.node_id, mode="node"
        )
        if (
            intervals.query != query
            or intervals.snapshot_fingerprint != source.snapshot_fingerprint
        ):
            raise DomainError("CONFLICT", "Daily interval snapshot differs from the report", 409)
        return SavedDailyTimeline(
            reference=source,
            query=query,
            recorded_microseconds=value.recorded_microseconds,
            verified_usage_microseconds=value.verified_usage_microseconds,
            total_usage_microseconds=value.total_usage_microseconds,
            intervals=intervals.cells,
            verified_short_gaps=value.daily_session.retained_gaps if value.daily_session else None,
        )

    def save_day(
        self, actor: ActorContext, query: UsageWindowQuery, source: DailyWindowReference
    ) -> SavedCoverageRollup:
        day = query.start_at.astimezone(ZoneInfo(query.timezone)).date()
        if query.owner_id is not None or (query.start_at, query.end_at) != calendar_boundaries(
            day, day, query.timezone
        ):
            raise DomainError("VALIDATION_ERROR", "Daily rollup requires a full node day", 422)
        reader = self.daily_windows
        deadline = monotonic() + reader.timeout_ms / 1000
        try:
            with reader.sessions() as db, db.no_autoflush:
                self._begin(db, actor, query.node_id)
                repo = DailyUsageRollupRepository(db)
                saved = repo.get(query.node_id, (source.snapshot_id,)).get(source.snapshot_id)
                if saved is None:
                    saved = self._project_day_in_transaction(
                        db, actor, source.snapshot_id, node_id=query.node_id
                    )
                    repo.insert(saved)
                if (
                    saved.snapshot_fingerprint != source.snapshot_fingerprint
                    or saved.query != query
                    or (saved.daily_session is None) != (self.session_occupancy is None)
                ):
                    raise DomainError("CONFLICT", "Daily rollup snapshot or scope differs", 409)
                reader.require_in_transaction(db, actor, query.node_id, None, "node")
                reader._deadline(deadline)
                commit_owned_transaction(db)
                return saved
        except ValueError:
            raise DomainError("CONFLICT", "Daily interval snapshot is inconsistent", 409) from None
        except DBAPIError as error:
            raise self._failure(error) from None

    def project_day(
        self, actor: ActorContext, snapshot_id: UUID, *, node_id: str
    ) -> SavedCoverageRollup:
        reader = self.daily_windows
        deadline = monotonic() + reader.timeout_ms / 1000
        try:
            with reader.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                self._begin(db, actor, node_id)
                result = self._project_day_in_transaction(db, actor, snapshot_id, node_id=node_id)
                reader.require_in_transaction(db, actor, node_id, None, "node")
                reader._deadline(deadline)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def _project_day_in_transaction(
        self, db: Session, actor: ActorContext, snapshot_id: UUID, *, node_id: str
    ) -> SavedCoverageRollup:
        view = self.daily_windows.read_in_transaction(
            db, actor, snapshot_id, node_id=node_id, mode="node"
        )
        rollup = roll_up_saved_coverage(view, "day")
        if self.session_occupancy is None:
            return rollup
        original = UsageWindowRepository(db).get(snapshot_id)
        if original is None or original.fingerprint != view.snapshot_fingerprint:
            raise DomainError("CONFLICT", "Daily session snapshot differs", 409)
        occupancy = self.session_occupancy.read_in_transaction(db, actor, original)
        try:
            loaded = SavedCoverageRollup.model_validate(
                {**rollup.model_dump(), "daily_session": occupancy}
            )
        except ValueError:
            raise DomainError(
                "CONFLICT", "Daily session totals differ from the snapshot", 409
            ) from None
        return loaded.model_copy(update={"fingerprint": loaded.content_fingerprint()})

    def read(
        self,
        actor: ActorContext,
        query: WeeklyFromDailyQuery,
        sources: tuple[DailyWindowReference, ...],
    ) -> WeeklyFromDaily:
        try:
            with self.daily_windows.sessions() as db:
                return self.read_in_transaction(db, actor, query, sources)
        except DBAPIError as error:
            raise self._failure(error) from None

    def read_in_transaction(
        self,
        db: Session,
        actor: ActorContext,
        query: WeeklyFromDailyQuery,
        sources: tuple[DailyWindowReference, ...],
    ) -> WeeklyFromDaily:
        if len(sources) > 7 or len({source.snapshot_id for source in sources}) != len(sources):
            raise DomainError("VALIDATION_ERROR", "Daily snapshot selection is invalid", 422)
        reader = self.daily_windows
        deadline = monotonic() + reader.timeout_ms / 1000
        try:
            with db.no_autoflush:
                self._begin(db, actor, query.node_id)
                saved = DailyUsageRollupRepository(db).get(
                    query.node_id, tuple(source.snapshot_id for source in sources)
                )
                days = []
                for source in sources:
                    value = saved.get(source.snapshot_id)
                    if value is None:
                        raise DomainError("NOT_FOUND", "Daily usage rollup is missing", 404)
                    if value.snapshot_fingerprint != source.snapshot_fingerprint:
                        raise DomainError(
                            "CONFLICT", "Daily snapshot differs from its reference", 409
                        )
                    days.append(value)
                result = weekly_from_daily(query, tuple(days))
                reader.require_in_transaction(db, actor, query.node_id, None, "node")
                reader._deadline(deadline)
                return result
        except ValueError:
            raise DomainError(
                "CONFLICT", "Daily statistics cannot form the requested week", 409
            ) from None
        except DBAPIError as error:
            raise self._failure(error) from None

    def build_in_transaction(
        self,
        db: Session,
        actor: ActorContext,
        query: WeeklyFromDailyQuery,
        ids: tuple[UUID, ...],
    ) -> WeeklyFromDaily:
        if not 1 <= len(ids) <= 7 or len(set(ids)) != len(ids):
            raise ValueError("Expected up to seven distinct day rollups")
        self._begin(db, actor, query.node_id)
        selected = DailyUsageRollupRepository(db).fingerprints(query.node_id, ids)
        if len(selected) != len(ids):
            raise DomainError(
                "COVERAGE_BUILD_PENDING", "Daily usage rollups are still pending", 409
            )
        return self.read_in_transaction(
            db,
            actor,
            query,
            tuple(
                DailyWindowReference(snapshot_id=id, snapshot_fingerprint=selected[id])
                for id in ids
            ),
        )
