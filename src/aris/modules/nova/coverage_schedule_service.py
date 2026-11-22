"""Short coordinator transactions; the long Lyra build runs outside this service."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import uuid4, uuid5

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.scheduler.schemas import SchedulerContext
from aris.modules.lyra.coverage_build_service import CoverageBuildService
from aris.modules.lyra.weekly_from_daily import WeeklyFromDailyQuery, WeeklyFromDailyReader
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.nova.coverage_schedule_repo import CoverageScheduleRepository
from aris.modules.nova.coverage_schedule_schemas import (
    CoveragePeriodResult,
    CoverageScheduleDefinition,
    CoverageScheduleStatus,
    DailyCoverageEvidence,
    DailyCoverageEvidenceCheck,
    DailyCoveragePeriodResult,
    WeeklyEvidenceReview,
)
from aris.modules.nova.daily_coverage_calendar import daily_coverage_targets
from aris.modules.nova.report_calendar import weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target


class CoverageScheduleService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        enabled: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.enabled = sessions, enabled
        self.clock = clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("coverage schedule clock requires timezone")
        return value.astimezone(UTC)

    def _require(self, session: Session, definition: CoverageScheduleDefinition) -> None:
        plan = definition.calendar
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=plan.owner_id,
                scope=plan.scope,
                permission="usage.read_own"
                if plan.report_type == "usage.mine"
                else "usage.read_all",
            ),
        )

    def _active(self) -> None:
        if not self.enabled:
            raise DomainError(
                "WEEKLY_COVERAGE_NOT_CONFIGURED", "Coverage scheduling is paused", 503
            )

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Coverage schedule database is unavailable",
            503,
        )

    def select(
        self,
        context: SchedulerContext,
        definition: CoverageScheduleDefinition,
        *,
        status_only: bool = False,
    ) -> CoverageScheduleStatus | None:
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        if not status_only:
            self._active()
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                repo = CoverageScheduleRepository(session)
                if not status_only:
                    repo.lock(definition.id)
                self._require(session, definition)
                value = repo.get(definition.id, lock=not status_only)
                if value is None and not status_only:
                    now = self._now()
                    value = CoverageScheduleStatus(
                        definition=definition,
                        next_week=definition.calendar.first_week,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                    )
                    repo.append(value)
                    self._event(session, context, value, "registered", now)
                    self._require(session, definition)
                    self._active()
                    commit_owned_transaction(session)
                elif value is not None and value.definition != definition:
                    raise DomainError(
                        "CONFLICT", "Coverage schedule identity has different input", 409
                    )
                self._require(session, definition)
                return value
        except DBAPIError as error:
            raise self._failure(error) from None

    def complete(
        self,
        context: SchedulerContext,
        definition: CoverageScheduleDefinition,
        week: date,
        builds: CoverageBuildService,
    ) -> CoveragePeriodResult:
        self._active()
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        if definition.daily_rollups:
            raise DomainError("VALIDATION_ERROR", "Coverage schedule requires daily rollups", 422)
        if self.sessions.kw.get("bind") is not builds.sessions.kw.get("bind"):
            raise ValueError("coverage coordinator and evidence require the same engine")
        now = self._now()
        target = weekly_coverage_target(
            definition.calendar,
            weekly_report_period(definition.calendar, week),
            definition.configuration,
            now=now,
        )
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                repo = CoverageScheduleRepository(session)
                repo.lock(definition.id)
                self._require(session, definition)
                status = repo.get(definition.id, lock=True)
                if status is None:
                    raise DomainError("NOT_FOUND", "Coverage schedule is not registered", 404)
                if status.definition != definition:
                    raise DomainError(
                        "CONFLICT", "Coverage schedule identity has different input", 409
                    )
                previous = repo.result(target.id)
                if previous is not None:
                    if (
                        previous.schedule_id != definition.id
                        or previous.definition_fingerprint != definition.fingerprint
                        or previous.week != week
                        or status.next_week <= week
                    ):
                        raise DomainError(
                            "CONFLICT", "Coverage result differs from schedule progress", 409
                        )
                    self._require(session, definition)
                    return previous
                if status.next_week != week:
                    raise DomainError("CONFLICT", "Coverage schedule cannot skip a week", 409)
                snapshot = builds.completed_in_transaction(session, target)
                mode: Literal["mine", "node"] = (
                    "mine" if definition.calendar.report_type == "usage.mine" else "node"
                )
                view = builds.windows.reader.read_in_transaction(
                    session,
                    ActorContext(
                        actor_id=definition.calendar.owner_id, request_id=context.request_id
                    ),
                    snapshot.request.id,
                    node_id=definition.calendar.scope.id,
                    mode=mode,
                )
                if view.snapshot_fingerprint != snapshot.fingerprint or view.query != target.query:
                    raise DomainError(
                        "CONFLICT", "Coverage snapshot differs from its schedule", 409
                    )
                draft = CoveragePeriodResult.model_construct(
                    id=target.id,
                    schedule_id=definition.id,
                    definition_fingerprint=definition.fingerprint,
                    week=week,
                    snapshot_id=snapshot.request.id,
                    snapshot_fingerprint=snapshot.fingerprint,
                    coverage=view.coverage,
                    recorded_microseconds=view.recorded_microseconds,
                    verified_usage_microseconds=view.verified_usage_microseconds,
                    total_usage_microseconds=view.total_usage_microseconds,
                    created_at=now,
                    event_id=uuid4(),
                    fingerprint="0" * 64,
                )
                result = CoveragePeriodResult.model_validate(
                    {**draft.model_dump(mode="json"), "fingerprint": draft.content_fingerprint()}
                )
                self._event(session, context, status, "snapshot_saved", now, result=result)
                repo.complete(status, result)
                self._require(session, definition)
                self._active()
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def complete_daily(
        self, context: SchedulerContext, definition: CoverageScheduleDefinition, week: date
    ) -> DailyCoveragePeriodResult:
        self._active()
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        if not definition.daily_rollups:
            raise DomainError("VALIDATION_ERROR", "Daily reporting is not configured", 422)
        calendar = definition.calendar
        period = weekly_report_period(calendar, week)
        now = self._now()
        if period.scheduled_for > now:
            raise DomainError("COVERAGE_BUILD_PENDING", "Weekly report is not due", 409)
        id = uuid5(definition.id, week.isoformat())
        try:
            with self.sessions() as db:
                transaction_budget(db, 100, 1000, 3000)
                repo = CoverageScheduleRepository(db)
                repo.lock(definition.id)
                self._require(db, definition)
                status = repo.get(definition.id, lock=True)
                if status is None:
                    raise DomainError("NOT_FOUND", "Coverage schedule is not registered", 404)
                if status.definition != definition:
                    raise DomainError("CONFLICT", "Coverage schedule has different input", 409)
                prior = repo.daily_result(id)
                if prior is not None:
                    if (
                        prior.definition_fingerprint != definition.fingerprint
                        or prior.week != week
                        or prior.schedule_id != definition.id
                        or status.next_week <= week
                    ):
                        raise DomainError(
                            "CONFLICT", "Weekly report differs from schedule progress", 409
                        )
                    self._require(db, definition)
                    return prior
                if status.next_week != week:
                    raise DomainError("CONFLICT", "Coverage schedule cannot skip a week", 409)
                targets = daily_coverage_targets(definition, week)
                actor = ActorContext(actor_id=calendar.owner_id, request_id=context.request_id)
                report = WeeklyFromDailyReader(
                    UsageWindowReader(self.sessions)
                ).build_in_transaction(
                    db,
                    actor,
                    WeeklyFromDailyQuery(
                        node_id=calendar.scope.id, timezone=calendar.timezone, week=week
                    ),
                    tuple(target.snapshot_id for target in targets),
                )
                if (
                    definition.session_occupancy
                    and (
                        report.session_evidence is None
                        or report.session_evidence.assessed_days != len(targets)
                    )
                ) or (not definition.session_occupancy and report.session_evidence is not None):
                    raise DomainError("CONFLICT", "Weekly session evidence differs from plan", 409)
                draft = DailyCoveragePeriodResult.model_construct(
                    id=id,
                    schedule_id=definition.id,
                    definition_fingerprint=definition.fingerprint,
                    week=week,
                    report=report,
                    created_at=now,
                    event_id=uuid4(),
                    fingerprint="0" * 64,
                )
                result = DailyCoveragePeriodResult.model_validate(
                    {**draft.model_dump(mode="json"), "fingerprint": draft.content_fingerprint()}
                )
                self._event(db, context, status, "daily_report_saved", now, result=result)
                repo.complete_daily(status, result)
                self._require(db, definition)
                self._active()
                commit_owned_transaction(db)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def verify_daily_week(
        self, context: SchedulerContext, definition: CoverageScheduleDefinition, week: date
    ) -> DailyCoveragePeriodResult:
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        context = SchedulerContext.model_validate(context.model_dump(mode="json"))
        if not definition.daily_rollups:
            raise DomainError("VALIDATION_ERROR", "Daily reporting is not configured", 422)
        targets = daily_coverage_targets(definition, week)
        calendar = definition.calendar
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                self._require(db, definition)
                repo = CoverageScheduleRepository(db)
                result = self._published_daily_week(repo, definition, week)
                actor = ActorContext(actor_id=calendar.owner_id, request_id=context.request_id)
                recomposed = WeeklyFromDailyReader(
                    UsageWindowReader(self.sessions)
                ).build_in_transaction(
                    db,
                    actor,
                    WeeklyFromDailyQuery(
                        node_id=calendar.scope.id, timezone=calendar.timezone, week=week
                    ),
                    tuple(target.snapshot_id for target in targets),
                )
                if recomposed != result.report:
                    raise DomainError("CONFLICT", "Weekly report differs from daily rollups", 409)
                self._require(db, definition)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _published_daily_week(
        repo: CoverageScheduleRepository, definition: CoverageScheduleDefinition, week: date
    ) -> DailyCoveragePeriodResult:
        weekly_report_period(definition.calendar, week)
        status = repo.get(definition.id)
        result = repo.daily_result(uuid5(definition.id, week.isoformat()))
        if status is None or result is None:
            raise DomainError("NOT_FOUND", "Weekly coverage report not found", 404)
        if (
            not definition.daily_rollups
            or status.definition != definition
            or result.schedule_id != definition.id
            or result.definition_fingerprint != definition.fingerprint
            or result.week != week
            or status.next_week <= week
        ):
            raise DomainError("CONFLICT", "Weekly report schedule differs", 409)
        return result

    @staticmethod
    def _evidence_matches(
        report: DailyCoveragePeriodResult, evidence: DailyCoverageEvidence
    ) -> bool:
        return (evidence.report_id, evidence.report_fingerprint) == (
            report.id,
            report.fingerprint,
        ) and any(
            (day.day, day.snapshot_fingerprint, day.rollup_fingerprint)
            == (evidence.day, evidence.snapshot_fingerprint, evidence.rollup_fingerprint)
            for day in report.report.days
        )

    def save_evidence_check(
        self, definition: CoverageScheduleDefinition, evidence: DailyCoverageEvidence
    ) -> DailyCoverageEvidenceCheck:
        self._active()
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        evidence = DailyCoverageEvidence.model_validate(evidence.model_dump(mode="json"))
        week = evidence.day - timedelta(days=evidence.day.weekday())
        try:
            with self.sessions() as db:
                transaction_budget(db, 100, 1000, 3000)
                repo = CoverageScheduleRepository(db)
                self._require(db, definition)
                report = self._published_daily_week(repo, definition, week)
                now = self._now()
                if not self._evidence_matches(report, evidence) or now < report.created_at:
                    raise DomainError("CONFLICT", "Evidence check differs from the report", 409)
                check = DailyCoverageEvidenceCheck(**evidence.model_dump(), checked_at=now)
                repo.add_evidence_check(check)
                saved = next(
                    (
                        value
                        for value in repo.evidence_checks(report.id)
                        if value.day == evidence.day
                    ),
                    None,
                )
                if (
                    saved is None
                    or not self._evidence_matches(report, saved)
                    or saved.checked_at < report.created_at
                ):
                    raise DomainError("CONFLICT", "Saved evidence check is inconsistent", 409)
                self._require(db, definition)
                self._active()
                commit_owned_transaction(db)
                return saved
        except DBAPIError as error:
            raise self._failure(error) from None

    def evidence_review(
        self, definition: CoverageScheduleDefinition, week: date
    ) -> WeeklyEvidenceReview:
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                review, _ = self.evidence_review_in_transaction(db, definition, week)
                return review
        except DBAPIError as error:
            raise self._failure(error) from None

    def evidence_review_in_transaction(
        self, db: Session, definition: CoverageScheduleDefinition, week: date
    ) -> tuple[WeeklyEvidenceReview, DailyCoveragePeriodResult]:
        definition = CoverageScheduleDefinition.model_validate(definition.model_dump(mode="json"))
        self._require(db, definition)
        repo = CoverageScheduleRepository(db)
        report = self._published_daily_week(repo, definition, week)
        checks = repo.evidence_checks(report.id)
        if any(
            not self._evidence_matches(report, check) or check.checked_at < report.created_at
            for check in checks
        ):
            raise DomainError("CONFLICT", "Saved evidence checks are inconsistent", 409)
        checked = {check.day for check in checks}
        self._require(db, definition)
        review = WeeklyEvidenceReview(
            report_id=report.id,
            report_fingerprint=report.fingerprint,
            week=week,
            checked_days=checks,
            pending_days=tuple(day.day for day in report.report.days if day.day not in checked),
        )
        return review, report

    @staticmethod
    def _event(
        session: Session,
        context: SchedulerContext,
        status: CoverageScheduleStatus,
        action: str,
        now: datetime,
        *,
        result: CoveragePeriodResult | DailyCoveragePeriodResult | None = None,
    ) -> None:
        id = str(status.definition.id)
        event_id = str(result.event_id if result else uuid4())
        name = "weekly_coverage." + action
        payload = {"schedule_id": id, "definition_fingerprint": status.definition.fingerprint}
        if isinstance(result, DailyCoveragePeriodResult):
            payload.update(
                result_id=str(result.id),
                report_fingerprint=result.report.fingerprint,
                coverage=result.report.coverage,
            )
        elif result is not None:
            payload.update(
                result_id=str(result.id),
                snapshot_id=str(result.snapshot_id),
                coverage=result.coverage,
            )
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=name,
                event_version=1,
                aggregate_type="weekly_coverage_schedule",
                aggregate_id=id,
                aggregate_version=status.revision + (1 if result else 0),
                occurred_at=now,
                recorded_at=now,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=id,
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=now))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=name,
                target=id,
                request_id=context.request_id,
                recorded_at=now,
                changes=payload,
            )
        )
