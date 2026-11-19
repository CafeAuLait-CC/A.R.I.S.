"""Queue fixed partitions in bounded atomic batches; no automatic worker activation."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration, UsagePostingContext
from aris.modules.lyra.ledger_service import UsageLedgerService
from aris.modules.lyra.partition_recording_service import PartitionEvidenceReader
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.recording_plan_repo import RecordingPlanRepository
from aris.modules.lyra.recording_plan_schemas import (
    RecordingDispatchReceipt,
    RecordingDispatchRequest,
    RecordingPlanProgress,
    RecordingPlanReceipt,
    RecordingPlanRequest,
    StoredRecordingPlan,
    recording_plan_fingerprint,
)
from aris.modules.lyra.residency_partition_schemas import VerifiedResidencyPartition


class RecordingPlanService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        evidence: PartitionEvidenceReader,
        ledger: UsageLedgerService,
        jobs: RecordingJobService,
        *,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("recording plan requires a bounded transaction budget")
        self.sessions, self.evidence, self.ledger, self.jobs = sessions, evidence, ledger, jobs
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_ms = timeout_ms

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("recording plan clock requires a timezone")
        return now.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Recording plan budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Recording plan database is unavailable",
            503,
        )

    def _configuration(self, request: RecordingPlanRequest) -> UsagePostingConfiguration:
        value = self.ledger.configuration
        if value is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Recording plan is paused", 503)
        config = UsagePostingConfiguration.model_validate(value.model_dump(mode="json"))
        if config.fingerprint != request.expected_recording_configuration_fingerprint:
            raise DomainError("CONFLICT", "Recording plan configuration differs", 409)
        return config

    def _job_configuration(self) -> RecordingJobLimits:
        value = self.jobs.limits
        if value is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Recording jobs are paused", 503)
        return RecordingJobLimits.model_validate(value.model_dump(mode="json"))

    def progress(self, id: UUID) -> RecordingPlanProgress:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, 3000)
                return self.progress_in_transaction(session, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def progress_in_transaction(session: Session, id: UUID) -> RecordingPlanProgress:
        result = RecordingPlanRepository(session).get(id)
        if result is None:
            raise DomainError("NOT_FOUND", "Recording plan was not found", 404)
        return result

    def create(
        self, context: UsagePostingContext, request: RecordingPlanRequest
    ) -> RecordingPlanReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.create_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def create_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordingPlanRequest
    ) -> RecordingPlanReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = RecordingPlanRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = RecordingPlanRepository(session)
                repo.lock_operation(request.id)
                prior = repo.get(request.id)
                if prior is not None:
                    if prior.plan.request != request:
                        raise DomainError("CONFLICT", "Recording plan has different input", 409)
                    return RecordingPlanReceipt(plan=prior.plan, duplicate=True)
                config = self._configuration(request)
                proof = self.evidence.verify_in_transaction(session, request.partition)
                proof = VerifiedResidencyPartition.model_validate(proof.model_dump(mode="json"))
                now = self._now()
                if proof.request != request.partition or proof.read_at > now:
                    raise DomainError("CONFLICT", "Recording plan source binding differs", 409)
                if proof.parent_run.rule_fingerprint != config.timing_fingerprint:
                    raise DomainError("CONFLICT", "Recording plan timing rules differ", 409)
                if proof.parent_run.first.compute_observed_at < config.recording_start_at:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Parent residency precedes recording start", 409
                    )
                unchecked = StoredRecordingPlan.model_construct(
                    request=request,
                    parent_run=proof.parent_run,
                    consumer_id=proof.consumer_id,
                    method=proof.method,
                    configuration=config,
                    partition_count=proof.partition_count,
                    first_partition_fingerprint=proof.fingerprint,
                    event_id=uuid4(),
                    created_at=now,
                    fingerprint="0" * 64,
                )
                plan = StoredRecordingPlan.model_validate(
                    {
                        **unchecked.model_dump(mode="json"),
                        "fingerprint": recording_plan_fingerprint(unchecked),
                    }
                )
                self._emit(
                    session,
                    context,
                    plan,
                    "created",
                    {"partition_count": plan.partition_count},
                    now,
                    1,
                )
                session.flush()
                repo.append(plan)
                session.flush()
                if self._configuration(request) != config:
                    raise DomainError("CONFLICT", "Recording plan configuration changed", 409)
                if self._now() < plan.created_at:
                    raise DomainError("CONFLICT", "Recording plan clock moved backwards", 409)
                self._deadline(deadline)
                if repo.get(request.id) != RecordingPlanProgress(
                    plan=plan, version=1, next_partition=0, updated_at=now, all_enqueued=False
                ):
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Recording plan was not stored", 503
                    )
                return RecordingPlanReceipt(plan=plan, duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Recording plan source is inconsistent", 503
            ) from None

    def dispatch(
        self, context: UsagePostingContext, request: RecordingDispatchRequest
    ) -> RecordingDispatchReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                result = self.dispatch_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def dispatch_in_transaction(
        self, session: Session, context: UsagePostingContext, request: RecordingDispatchRequest
    ) -> RecordingDispatchReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = RecordingDispatchRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = RecordingPlanRepository(session)
                row = repo.locked(request.plan_id)
                progress = self.progress_in_transaction(session, request.plan_id)
                if row is None:
                    raise DomainError("NOT_FOUND", "Recording plan was not found", 404)
                if progress.version != request.expected_version:
                    raise DomainError("CONFLICT", "Recording plan progress changed; reload it", 409)
                if progress.all_enqueued:
                    return RecordingDispatchReceipt(progress=progress, job_ids=())
                plan = progress.plan
                if self._configuration(plan.request) != plan.configuration:
                    raise DomainError("CONFLICT", "Recording plan configuration changed", 409)
                job_config = self._job_configuration()
                if self._now() < progress.updated_at:
                    raise DomainError("CONFLICT", "Recording plan clock moved backwards", 409)
                through = min(plan.partition_count, progress.next_partition + request.limit)
                job_ids = []
                for index in range(progress.next_partition, through):
                    self._deadline(deadline)
                    job = plan.job(index)
                    receipt = self.jobs.enqueue_in_transaction(session, context, job)
                    if receipt.job.request != job:
                        raise DomainError("CONFLICT", "Recording plan child input differs", 409)
                    job_ids.append(job.id)
                now = self._now()
                if now < progress.updated_at:
                    raise DomainError("CONFLICT", "Recording plan clock moved backwards", 409)
                if self._configuration(plan.request) != plan.configuration:
                    raise DomainError("CONFLICT", "Recording plan configuration changed", 409)
                row.next_partition = through
                row.version += 1
                row.updated_at = now
                self._emit(
                    session,
                    context,
                    plan,
                    "dispatched",
                    {
                        "after_partition": progress.next_partition,
                        "through_partition": through,
                        "version": row.version,
                    },
                    now,
                    row.version,
                )
                session.flush()
                current = self.progress_in_transaction(session, plan.request.id)
                self._deadline(deadline)
                if (
                    self._now() < current.updated_at
                    or self._configuration(plan.request) != plan.configuration
                ):
                    raise DomainError(
                        "CONFLICT", "Recording plan configuration or clock changed", 409
                    )
                if self._job_configuration() != job_config:
                    raise DomainError("CONFLICT", "Recording job configuration changed", 409)
                return RecordingDispatchReceipt(progress=current, job_ids=tuple(job_ids))
        except DBAPIError as error:
            raise self._failure(error) from None

    @staticmethod
    def _emit(
        session: Session,
        context: UsagePostingContext,
        plan: StoredRecordingPlan,
        action: str,
        changes: dict[str, object],
        at: datetime,
        version: int,
    ) -> None:
        event_id = str(plan.event_id if action == "created" else uuid4())
        kind = "lyra.usage.recording_plan_" + action
        payload = {
            "plan_id": str(plan.request.id),
            "fingerprint": plan.fingerprint,
            "run_id": plan.parent_run.id,
            "quota_usage_available": False,
            **changes,
        }
        session.add(
            DomainEvent(
                event_id=event_id,
                event_type=kind,
                event_version=1,
                aggregate_type="usage_recording_plan",
                aggregate_id=str(plan.request.id),
                aggregate_version=version,
                occurred_at=at,
                recorded_at=at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(plan.request.id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=event_id, created_at=at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=kind,
                target=str(plan.request.id),
                request_id=context.request_id,
                recorded_at=at,
                changes=payload,
            )
        )
