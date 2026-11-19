"""Internal measured-time authority; no ingress, consumer or production activation."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.budgets import database_failure
from aris.core.commands.schemas import CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.lyra.derivation_schemas import StoredUsageDerivation
from aris.modules.lyra.ledger_plan import (
    MeasuredWindow,
    UsageMeasurementProposal,
    plan_usage_posting,
)
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.ledger_schemas import (
    MAX_LEDGER_BYTES,
    PostedMeasurement,
    ReversedMeasurement,
    UsageLedgerEntry,
    UsageLedgerHead,
    UsageLedgerReceipt,
    UsagePostingConfiguration,
    UsagePostingContext,
    UsagePostingRequest,
    UsageReversalRequest,
    canonical_ledger,
    ledger_fingerprint,
    make_ledger_entry,
    measurement_key,
)

ADJUST_USAGE_PERMISSION = "usage.adjust"


class VerifiedUsageDerivations(Protocol):
    def verified_in_transaction(
        self, session: Session, record_id: UUID
    ) -> StoredUsageDerivation: ...


class UsageLedgerService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        derivations: VerifiedUsageDerivations,
        *,
        configuration: UsagePostingConfiguration | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.derivations = sessions, derivations
        self.access = AccessService(sessions)
        self.configuration = (
            UsagePostingConfiguration.model_validate(configuration.model_dump(mode="json"))
            if configuration is not None
            else None
        )
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("usage ledger clock requires a timezone")
        return utc(value)

    def _configuration(self, request: UsagePostingRequest) -> UsagePostingConfiguration:
        configuration = self.configuration
        if configuration is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "Usage recording is not configured", 503)
        configuration = UsagePostingConfiguration.model_validate(
            configuration.model_dump(mode="json")
        )
        if configuration.fingerprint != request.expected_configuration_fingerprint:
            raise DomainError("CONFLICT", "Usage recording configuration differs", 409)
        return configuration

    def _source(self, session: Session, request: UsagePostingRequest) -> StoredUsageDerivation:
        source = self.derivations.verified_in_transaction(session, request.derivation_id)
        source = StoredUsageDerivation.model_validate(source.model_dump(mode="json"))
        if (
            source.receipt.id != request.derivation_id
            or source.receipt.result_fingerprint != request.expected_result_fingerprint
        ):
            raise DomainError("CONFLICT", "Usage recording evidence differs", 409)
        return source

    def _candidate(
        self,
        source: StoredUsageDerivation,
        request: UsagePostingRequest,
        configuration: UsagePostingConfiguration,
        at: datetime,
    ) -> UsageMeasurementProposal:
        if request.session_index >= len(source.artifact.analysis.request.attribution.attributions):
            raise DomainError("VALIDATION_ERROR", "Usage candidate is outside its evidence", 422)
        result = plan_usage_posting(
            source, rules=configuration.planning_rules(), coverage=None, evaluated_at=at
        )
        outcome = result.outcomes[request.session_index]
        if outcome.state == "needs_review":
            raise DomainError("USAGE_NEEDS_REVIEW", "Usage evidence requires review", 409)
        if outcome.candidate is None or outcome.reason != "LEDGER_COVERAGE_UNAVAILABLE":
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Usage recording evidence is unavailable", 503
            )
        return outcome.candidate

    def exact(self, entry_id: UUID) -> UsageLedgerEntry:
        with self.sessions() as session, session.no_autoflush:
            entry = UsageLedgerRepository(session).entry(entry_id)
            if entry is None:
                raise DomainError("NOT_FOUND", "Usage entry not found", 404)
            return entry

    def post(
        self, context: UsagePostingContext, request: UsagePostingRequest
    ) -> UsageLedgerReceipt:
        with self.sessions() as session:
            result = self.post_in_transaction(session, context, request)
            commit_owned_transaction(session)
            return result

    def post_in_transaction(
        self,
        session: Session,
        context: UsagePostingContext,
        request: UsagePostingRequest,
    ) -> UsageLedgerReceipt:
        context = UsagePostingContext.model_validate(context.model_dump(mode="json"))
        request = UsagePostingRequest.model_validate(request.model_dump(mode="json"))
        raw = canonical_ledger(request)
        if len(raw.encode()) > MAX_LEDGER_BYTES:
            raise DomainError("VALIDATION_ERROR", "Usage recording input is oversized", 422)
        fingerprint = ledger_fingerprint("request", raw)
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = UsageLedgerRepository(session)
                repo.lock("operation", str(request.id))
                prior = repo.operation(request.id)
                if prior is not None:
                    if (
                        prior.receipt.request_fingerprint != fingerprint
                        or prior.receipt.actor != context.actor
                    ):
                        raise DomainError("CONFLICT", "Usage operation has different input", 409)
                    return prior.receipt
                configuration = self._configuration(request)
                original = self._source(session, request)
                candidate = self._candidate(original, request, configuration, self._now())
                # A per-node gate orders committed prefixes; a GPU gate also
                # covers an opaque GPU reference seen under conflicting nodes.
                repo.lock("node", candidate.node_id)
                repo.lock("gpu", candidate.gpu_uuid)
                source = self._source(session, request)
                if source.artifact != original.artifact or source.receipt.model_copy(
                    update={"duplicate": True}
                ) != original.receipt.model_copy(update={"duplicate": True}):
                    raise DomainError("CONFLICT", "Usage evidence changed while waiting", 409)
                if self._configuration(request) != configuration:
                    raise DomainError("CONFLICT", "Usage recording configuration changed", 409)
                now = self._now()
                candidate = self._candidate(source, request, configuration, now)
                if repo.overlapping_reversal(candidate) is not None:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW",
                        "Previously reversed GPU time requires reconciliation",
                        409,
                    )
                head = repo.head(candidate.node_id)
                coverage = repo.coverage(candidate, at=now)
                outcome = plan_usage_posting(
                    source,
                    rules=configuration.planning_rules(),
                    coverage=coverage,
                    evaluated_at=now,
                ).outcomes[request.session_index]
                if outcome.state == "needs_review":
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Usage measurement overlaps existing history", 409
                    )
                if outcome.state == "unavailable":
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Usage ledger coverage is unavailable", 503
                    )
                existing = repo.measured(measurement_key(candidate))
                if outcome.state == "already_recorded":
                    if existing is None or (existing.id,) != outcome.existing_ids:
                        raise inconsistent()
                    entry, effect = existing, "already_recorded"
                else:
                    if existing is not None:
                        raise DomainError(
                            "USAGE_NEEDS_REVIEW", "A prior measurement requires reconciliation", 409
                        )
                    entry = self._append_measurement(
                        repo, head, context, request, candidate, configuration, now
                    )
                    head = repo.head(candidate.node_id)
                    effect = "recorded"
                if self._configuration(request) != configuration:
                    raise DomainError("CONFLICT", "Usage recording configuration changed", 409)
                final = self._source(session, request)
                if final.artifact != source.artifact or final.receipt.model_copy(
                    update={"duplicate": True}
                ) != source.receipt.model_copy(update={"duplicate": True}):
                    raise DomainError("CONFLICT", "Usage evidence changed before recording", 409)
                if self._configuration(request) != configuration:
                    raise DomainError("CONFLICT", "Usage recording configuration changed", 409)
                now = self._now()
                if now < entry.recorded_at:
                    raise DomainError("CONFLICT", "Usage recording clock moved backwards", 409)
                receipt = UsageLedgerReceipt(
                    id=request.id,
                    actor=context.actor,
                    operation=request.operation,
                    duplicate=False,
                    effect=effect,
                    created_at=now,
                    head=head,
                    entries=(entry.id,),
                    entry_fingerprints=(entry.fingerprint,),
                    request_fingerprint=fingerprint,
                )
                repo.record_operation(request, receipt)
                if effect == "already_recorded":
                    session.add(
                        AuditEntry(
                            id=str(uuid4()),
                            actor=context.actor,
                            action="lyra.usage.measurement_reused",
                            target=str(entry.id),
                            request_id=context.request_id,
                            recorded_at=now,
                            changes={
                                "operation_id": str(request.id),
                                "entry_fingerprint": entry.fingerprint,
                            },
                        )
                    )
                session.flush()
                stored = repo.operation(request.id, duplicate=False)
                if stored is None:
                    raise inconsistent()
                return stored.receipt
        except DBAPIError as error:
            if getattr(error.orig, "sqlstate", None) == "23P01":
                raise DomainError(
                    "USAGE_NEEDS_REVIEW", "Usage interval requires reconciliation", 409
                ) from None
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Usage recording storage is unavailable", 503
            ) from None

    def _append_measurement(
        self,
        repo: UsageLedgerRepository,
        head: UsageLedgerHead,
        context: UsagePostingContext,
        request: UsagePostingRequest,
        candidate: UsageMeasurementProposal,
        configuration: UsagePostingConfiguration,
        at: datetime,
    ) -> UsageLedgerEntry:
        entry = make_ledger_entry(
            id=uuid4(),
            event_id=uuid4(),
            node_id=head.node_id,
            revision=head.revision + 1,
            previous_fingerprint=head.fingerprint,
            actor=context.actor,
            request_id=context.request_id,
            recorded_at=at,
            details=PostedMeasurement(measurement=candidate, configuration=configuration),
        )
        return self._append_entry(repo, head, entry, request.id)

    def _append_entry(
        self,
        repo: UsageLedgerRepository,
        head: UsageLedgerHead,
        entry: UsageLedgerEntry,
        operation_id: UUID,
    ) -> UsageLedgerEntry:
        session = repo.session
        at = entry.recorded_at
        action = (
            "lyra.usage.measurement_recorded"
            if isinstance(entry.details, PostedMeasurement)
            else "lyra.usage.measurement_reversed"
        )
        session.add(
            DomainEvent(
                event_id=str(entry.event_id),
                event_type=action,
                event_version=1,
                aggregate_type="usage_ledger",
                aggregate_id=entry.node_id,
                aggregate_version=entry.revision,
                occurred_at=at,
                recorded_at=at,
                actor=entry.actor,
                correlation_id=entry.request_id,
                causation_id=str(operation_id),
                payload={
                    "entry_id": str(entry.id),
                    "node_id": entry.node_id,
                    "revision": entry.revision,
                    "fingerprint": entry.fingerprint,
                    "quota_usage_available": False,
                },
            )
        )
        session.flush()
        repo.append(entry, head)
        session.flush()
        if isinstance(entry.details, PostedMeasurement):
            repo.claim(entry)
        else:
            repo.unclaim(entry.details.target_id, entry.details.measurement)
        session.add(OutboxEntry(event_id=str(entry.event_id), created_at=at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=entry.actor,
                action=action,
                target=str(entry.id),
                request_id=entry.request_id,
                recorded_at=at,
                changes={
                    "node_id": entry.node_id,
                    "revision": entry.revision,
                    "fingerprint": entry.fingerprint,
                    "operation_id": str(operation_id),
                },
            )
        )
        session.flush()
        return entry

    def adjustment_scope(self, session: Session, target_id: UUID) -> AccessScope:
        with session.no_autoflush:
            target = UsageLedgerRepository(session).entry(target_id)
        if target is None:
            raise DomainError("NOT_FOUND", "Usage measurement not found", 404)
        return AccessScope(type="node", id=target.node_id)

    def _authorize_adjustment(
        self, session: Session, execution: CommandExecution, principal: UserPrincipal, node_id: str
    ) -> None:
        scope = AccessScope(type="node", id=node_id)
        if principal.user_id != execution.actor_id or execution.scope != scope:
            raise DomainError("PERMISSION_DENIED", "Usage adjustment actor or scope differs", 403)
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=execution.actor_id, permission=ADJUST_USAGE_PERMISSION, scope=scope
            ),
        )
        if execution.execution_deadline is not None and self._now() >= execution.execution_deadline:
            raise DomainError("EXECUTION_EXPIRED", "Usage adjustment deadline elapsed", 409)

    def reverse_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: UsageReversalRequest,
    ) -> UsageLedgerReceipt:
        """Called only from a confirmed registered Core command; never commits."""
        execution = CommandExecution.model_validate(execution.model_dump(mode="json"))
        principal = UserPrincipal.model_validate(principal.model_dump(mode="json"))
        request = UsageReversalRequest.model_validate(request.model_dump(mode="json"))
        if request.id != execution.command_id:
            raise DomainError("CONFLICT", "Usage operation differs from its command", 409)
        fingerprint = ledger_fingerprint("request", canonical_ledger(request))
        try:
            with transaction_savepoint(session), session.no_autoflush:
                repo = UsageLedgerRepository(session)
                repo.lock("operation", str(request.id))
                target = repo.entry(request.target_id)
                if target is None:
                    raise DomainError("NOT_FOUND", "Usage measurement not found", 404)
                self._authorize_adjustment(session, execution, principal, target.node_id)
                prior = repo.operation(request.id)
                if prior is not None:
                    if (
                        prior.receipt.request_fingerprint != fingerprint
                        or prior.receipt.actor != str(execution.actor_id)
                    ):
                        raise DomainError("CONFLICT", "Usage operation has different input", 409)
                    return prior.receipt
                repo.lock("node", target.node_id)
                repo.lock("gpu", target.details.measurement.gpu_uuid)
                current = repo.entry(request.target_id)
                if current != target:
                    raise DomainError("CONFLICT", "Usage target changed while waiting", 409)
                self._authorize_adjustment(session, execution, principal, target.node_id)
                if not isinstance(target.details, PostedMeasurement):
                    raise DomainError(
                        "VALIDATION_ERROR", "Only a measured entry can be reversed", 422
                    )
                if (
                    target.fingerprint != request.expected_target_fingerprint
                    or target.revision != request.expected_target_revision
                ):
                    raise DomainError(
                        "CONFLICT", "Usage target differs from the reviewed entry", 409
                    )
                now = self._now()
                head = repo.head(target.node_id)
                window = MeasuredWindow.model_validate(
                    target.details.measurement.model_dump(include=set(MeasuredWindow.model_fields))
                )
                coverage = repo.coverage(window, at=now)
                if coverage.state != "complete":
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Usage projection is incomplete", 503
                    )
                if target.id not in {entry.id for entry in coverage.records}:
                    raise DomainError(
                        "USAGE_NEEDS_REVIEW", "Usage measurement is no longer active", 409
                    )
                entry = make_ledger_entry(
                    id=uuid4(),
                    event_id=uuid4(),
                    node_id=head.node_id,
                    revision=head.revision + 1,
                    previous_fingerprint=head.fingerprint,
                    actor=str(execution.actor_id),
                    request_id=execution.request_id,
                    recorded_at=now,
                    details=ReversedMeasurement(
                        measurement=window,
                        target_id=target.id,
                        target_fingerprint=target.fingerprint,
                        reason=request.reason,
                    ),
                )
                self._append_entry(repo, head, entry, request.id)
                self._authorize_adjustment(session, execution, principal, target.node_id)
                head = repo.head(target.node_id)
                receipt = UsageLedgerReceipt(
                    id=request.id,
                    operation=request.operation,
                    actor=str(execution.actor_id),
                    duplicate=False,
                    effect="reversed",
                    created_at=self._now(),
                    head=head,
                    entries=(entry.id,),
                    entry_fingerprints=(entry.fingerprint,),
                    request_fingerprint=fingerprint,
                )
                if receipt.created_at < entry.recorded_at:
                    raise DomainError("CONFLICT", "Usage adjustment clock moved backwards", 409)
                repo.record_operation(request, receipt)
                session.flush()
                self._authorize_adjustment(session, execution, principal, target.node_id)
                stored = repo.operation(request.id, duplicate=False)
                if stored is None:
                    raise inconsistent()
                return stored.receipt
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                "RETRYABLE_DATABASE_ERROR"
                if code == "RETRYABLE_DATABASE_ERROR"
                else "DEPENDENCY_UNAVAILABLE",
                "Usage adjustment storage is unavailable",
                503,
            ) from None
