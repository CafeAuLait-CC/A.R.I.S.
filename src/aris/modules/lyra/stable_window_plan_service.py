"""Persist rolling original layouts and bounded atomic acceptance/job dispatch."""

import re
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration, UsagePostingContext
from aris.modules.lyra.recording_job_schemas import RecordingJobLimits
from aris.modules.lyra.recording_job_service import RecordingJobService
from aris.modules.lyra.residency_layout import plan_residency_windows
from aris.modules.lyra.residency_layout_verification_service import StableResidencyWindowService
from aris.modules.lyra.stable_window_acceptance_schemas import AcceptStableWindow
from aris.modules.lyra.stable_window_acceptance_service import StableWindowAcceptanceService
from aris.modules.lyra.stable_window_plan_repo import StableWindowPlanRepository
from aris.modules.lyra.stable_window_plan_schemas import (
    StableWindowDispatchReceipt,
    StableWindowDispatchRequest,
    StableWindowPlanObservation,
    StableWindowPlanReceipt,
    StoredStableWindowPlan,
    stable_plan_fingerprint,
)
from aris.modules.lyra.stable_window_recording_schemas import StableWindowJobRequest
from aris.modules.lyra.trace_consumer_service import trace_replay_scope


class StableWindowPlanService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        evidence: StableResidencyWindowService,
        acceptances: StableWindowAcceptanceService,
        jobs: RecordingJobService,
        *,
        configuration: UsagePostingConfiguration | None,
        clock: Callable[[], datetime] | None = None,
        timeout_ms: int = 30000,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 30000:
            raise ValueError("stable dispatch requires a bounded timeout")
        self.sessions, self.evidence, self.acceptances, self.jobs = (
            sessions,
            evidence,
            acceptances,
            jobs,
        )
        self.configuration, self.timeout_ms = configuration, timeout_ms
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stable plan clock requires a timezone")
        return value.astimezone(UTC)

    @staticmethod
    def _deadline(deadline: float) -> None:
        if monotonic() >= deadline:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Stable dispatch budget exceeded", 503)

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(database_failure(error), "Stable plan database is unavailable", 503)

    def _configuration(self, request: StableWindowPlanObservation) -> UsagePostingConfiguration:
        if self.configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Stable planning is paused", 503)
        value = UsagePostingConfiguration.model_validate(self.configuration.model_dump(mode="json"))
        if (
            value.fingerprint != request.expected_recording_configuration_fingerprint
            or self.acceptances.configuration != value
            or self.acceptances.evidence is not self.evidence
            or self.evidence.method.fingerprint != request.window.expected_method_fingerprint
        ):
            raise DomainError("CONFLICT", "Stable plan authority or configuration differs", 409)
        return value

    def _jobs(self, value: StoredStableWindowPlan) -> RecordingJobLimits:
        writer, limits = self.jobs.stable_recorder, self.jobs.limits
        if writer is None or limits is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Stable dispatch jobs are paused", 503)
        if (
            writer.acceptances is not self.acceptances
            or writer.ledger.configuration != value.configuration
        ):
            raise DomainError("CONFLICT", "Stable job authority differs", 409)
        return RecordingJobLimits.model_validate(limits.model_dump(mode="json"))

    @staticmethod
    def _seal(value: StoredStableWindowPlan) -> StoredStableWindowPlan:
        return StoredStableWindowPlan.model_validate(
            {**value.model_dump(), "fingerprint": stable_plan_fingerprint(value)}
        )

    def progress_in_transaction(self, session: Session, key: str) -> StoredStableWindowPlan:
        if re.fullmatch(r"[a-f0-9]{64}", key) is None:
            raise DomainError("VALIDATION_ERROR", "Invalid stable layout key", 422)
        with session.no_autoflush:
            value = StableWindowPlanRepository(session).get(key)
        if value is None:
            raise DomainError("NOT_FOUND", "Stable plan not found", 404)
        return value

    def progress(self, key: str) -> StoredStableWindowPlan:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.progress_in_transaction(session, key)
        except DBAPIError as error:
            raise self._failure(error) from None

    def observe(
        self, context: UsagePostingContext, request: StableWindowPlanObservation
    ) -> StableWindowPlanReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.observe_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def observe_in_transaction(
        self, session: Session, context: UsagePostingContext, request: StableWindowPlanObservation
    ) -> StableWindowPlanReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = StableWindowPlanObservation.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush, trace_replay_scope(session):
                witness = self.evidence.layout_witness_in_transaction(session, request.window)
                page = plan_residency_windows(
                    witness, self.evidence.method, first_window=0, limit=1
                )
                repo = StableWindowPlanRepository(session)
                repo.lock(page.layout_key)
                prior = repo.get(page.layout_key)
                if prior is not None:
                    if (
                        prior.configuration.fingerprint
                        != request.expected_recording_configuration_fingerprint
                    ):
                        raise DomainError(
                            "CONFLICT", "Stable plan recording configuration differs", 409
                        )
                    if witness.consumer_revision <= prior.witness.consumer_revision:
                        if witness.consumer_revision == prior.witness.consumer_revision and (
                            request.window.reference.record_id
                            != prior.latest.window.reference.record_id
                            or witness.parent_run != prior.witness.parent_run
                            or witness.state_at_record != prior.witness.state_at_record
                        ):
                            raise DomainError(
                                "CONFLICT", "Stable source revision has another parent", 409
                            )
                        return StableWindowPlanReceipt(plan=prior, duplicate=True)
                configuration = self._configuration(request)
                if witness.parent_run.first.compute_observed_at < configuration.recording_start_at:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Stable parent precedes recording start", 409
                    )
                now = self._now()
                if prior is None:
                    draft = StoredStableWindowPlan.model_construct(
                        key=page.layout_key,
                        origin=request,
                        latest=request,
                        configuration=configuration,
                        method=self.evidence.method,
                        witness=witness,
                        version=1,
                        next_window=0,
                        event_id=uuid4(),
                        created_at=now,
                        updated_at=now,
                        fingerprint="0" * 64,
                    )
                else:
                    if (
                        witness.parent_run.first != prior.witness.parent_run.first
                        or witness.parent_run.intervals < prior.witness.parent_run.intervals
                        or (
                            prior.witness.state_at_record == "closed"
                            and witness.state_at_record != "closed"
                        )
                        or request.identity_revision < prior.latest.identity_revision
                        or now < prior.updated_at
                    ):
                        raise DomainError(
                            "CONFLICT", "Stable parent or progress moved backwards", 409
                        )
                    draft = prior.model_copy(
                        update={
                            "latest": request,
                            "witness": witness,
                            "version": prior.version + 1,
                            "updated_at": now,
                        }
                    )
                value = self._seal(draft)
                self._persist(session, context, repo, value, prior, "observed")
                fresh = self.evidence.layout_witness_in_transaction(session, request.window)
                if (
                    fresh.fingerprint != witness.fingerprint
                    or self._configuration(request) != configuration
                ):
                    raise DomainError(
                        "CONFLICT", "Stable source changed before observation commit", 409
                    )
                self._deadline(deadline)
                return StableWindowPlanReceipt(plan=repo.get(value.key), duplicate=False)
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable plan source is inconsistent", 503
            ) from None

    def dispatch(
        self, context: UsagePostingContext, request: StableWindowDispatchRequest
    ) -> StableWindowDispatchReceipt:
        try:
            with self.sessions() as session:
                transaction_budget(session, 1000, 1000, self.timeout_ms)
                result = self.dispatch_in_transaction(session, context, request)
                commit_owned_transaction(session)
                return result
        except DBAPIError as error:
            raise self._failure(error) from None

    def dispatch_in_transaction(
        self, session: Session, context: UsagePostingContext, request: StableWindowDispatchRequest
    ) -> StableWindowDispatchReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = StableWindowDispatchRequest.model_validate(request.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            with transaction_savepoint(session), session.no_autoflush, trace_replay_scope(session):
                repo = StableWindowPlanRepository(session)
                repo.lock(request.key)
                prior = self.progress_in_transaction(session, request.key)
                if request.expected_version != prior.version:
                    raise DomainError("CONFLICT", "Stable plan version changed", 409)
                if prior.next_window == prior.page.total_planned_windows:
                    return StableWindowDispatchReceipt(plan=prior, job_ids=())
                configuration = self._configuration(prior.latest)
                limits = self._jobs(prior)
                job_ids = []
                stop = min(prior.next_window + request.limit, prior.page.total_planned_windows)
                for index in range(prior.next_window, stop):
                    self._deadline(deadline)
                    accepted = self.acceptances.accept_in_transaction(
                        session,
                        context,
                        AcceptStableWindow(
                            window=prior.latest.window.model_copy(update={"window_index": index}),
                            expected_recording_configuration_fingerprint=configuration.fingerprint,
                            identity_revision=prior.latest.identity_revision,
                        ),
                    )
                    fixed = StableWindowJobRequest.from_acceptance(
                        self.acceptances.exact_in_transaction(session, accepted.key)
                    )
                    queued = self.jobs.enqueue_in_transaction(session, context, fixed)
                    if queued.job.request != fixed:
                        raise DomainError(
                            "CONFLICT", "Stable job input changed during dispatch", 409
                        )
                    job_ids.append(fixed.id)
                value = self._seal(
                    prior.model_copy(
                        update={
                            "next_window": stop,
                            "version": prior.version + 1,
                            "updated_at": self._now(),
                        }
                    )
                )
                self._persist(session, context, repo, value, prior, "dispatched")
                witness = self.evidence.layout_witness_in_transaction(session, prior.latest.window)
                if (
                    witness.fingerprint != prior.witness.fingerprint
                    or self._configuration(prior.latest) != configuration
                    or self._jobs(prior) != limits
                ):
                    raise DomainError("CONFLICT", "Stable dispatch dependencies changed", 409)
                self._deadline(deadline)
                return StableWindowDispatchReceipt(plan=repo.get(value.key), job_ids=tuple(job_ids))
        except DBAPIError as error:
            raise self._failure(error) from None
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable dispatch is inconsistent", 503
            ) from None

    @staticmethod
    def _persist(
        session: Session,
        context: UsagePostingContext,
        repo: StableWindowPlanRepository,
        value: StoredStableWindowPlan,
        prior: StoredStableWindowPlan | None,
        transition: str,
    ) -> None:
        action = "lyra.usage.stable_plan." + transition
        event_id = value.event_id if prior is None else uuid4()
        payload = dict(
            layout_key=value.key,
            version=value.version,
            next_window=value.next_window,
            planned_windows=value.page.total_planned_windows,
            state_at_record=value.witness.state_at_record,
            source_record_id=str(value.latest.window.reference.record_id),
            fingerprint=value.fingerprint,
            previous_fingerprint=prior.fingerprint if prior else None,
            quota_usage_available=False,
        )
        session.add(
            DomainEvent(
                event_id=str(event_id),
                event_type=action,
                event_version=1,
                aggregate_type="stable_window_dispatch_plan",
                aggregate_id=value.key,
                aggregate_version=value.version,
                occurred_at=value.updated_at,
                recorded_at=value.updated_at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(value.latest.window.reference.record_id),
                payload=payload,
            )
        )
        session.flush()
        session.add(OutboxEntry(event_id=str(event_id), created_at=value.updated_at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action=action,
                target=value.key,
                request_id=context.request_id,
                recorded_at=value.updated_at,
                changes=payload,
            )
        )
        if prior is None:
            repo.append(value)
        else:
            repo.update(value)
        session.flush()
