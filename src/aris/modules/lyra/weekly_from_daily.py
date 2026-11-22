"""Generate a fixed natural-week report from consolidated day evidence."""

from collections import defaultdict
from datetime import date, timedelta
from time import monotonic
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator
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
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_history import calendar_boundaries
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_schemas import UsageWindowQuery


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
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

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
        fingerprint="0" * 64,
    )
    return result.model_copy(update={"fingerprint": result.content_fingerprint()})


class WeeklyFromDailyReader:
    def __init__(self, daily_windows: UsageWindowReader) -> None:
        self.daily_windows = daily_windows

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
                    value = reader.read_in_transaction(
                        db, actor, source.snapshot_id, node_id=query.node_id, mode="node"
                    )
                    saved = roll_up_saved_coverage(value, "day")
                    repo.insert(saved)
                if (
                    saved.snapshot_fingerprint != source.snapshot_fingerprint
                    or saved.query != query
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
