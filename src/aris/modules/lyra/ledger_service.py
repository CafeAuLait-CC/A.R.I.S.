"""Internal measured-time authority; no ingress, consumer or production activation."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction, transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.lyra.derivation_schemas import StoredUsageDerivation
from aris.modules.lyra.ledger_plan import UsageMeasurementProposal, plan_usage_posting
from aris.modules.lyra.ledger_repo import UsageLedgerRepository, inconsistent
from aris.modules.lyra.ledger_schemas import (
    MAX_LEDGER_BYTES,
    PostedMeasurement,
    UsageLedgerEntry,
    UsageLedgerHead,
    UsageLedgerReceipt,
    UsagePostingConfiguration,
    UsagePostingContext,
    UsagePostingRequest,
    canonical_ledger,
    ledger_fingerprint,
    make_ledger_entry,
    measurement_key,
)


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
        session = repo.session
        session.add(
            DomainEvent(
                event_id=str(entry.event_id),
                event_type="lyra.usage.measurement_recorded",
                event_version=1,
                aggregate_type="usage_ledger",
                aggregate_id=entry.node_id,
                aggregate_version=entry.revision,
                occurred_at=at,
                recorded_at=at,
                actor=context.actor,
                correlation_id=context.request_id,
                causation_id=str(request.id),
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
        repo.claim(entry)
        session.add(OutboxEntry(event_id=str(entry.event_id), created_at=at))
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=context.actor,
                action="lyra.usage.measurement_recorded",
                target=str(entry.id),
                request_id=context.request_id,
                recorded_at=at,
                changes={
                    "node_id": entry.node_id,
                    "revision": entry.revision,
                    "fingerprint": entry.fingerprint,
                    "operation_id": str(request.id),
                },
            )
        )
        session.flush()
        return entry
