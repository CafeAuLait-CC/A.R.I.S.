"""Confirmed source recording only; stored source content is never a policy decision."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import ActorContext, CommandExecution
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc
from aris.modules.nova.policy_catalog_repo import PolicyCatalogRepository, PolicySourceRecord
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
    PolicySourcePage,
    PolicySourceReference,
    PolicySourceSummary,
    PolicySourceView,
    RecordedPolicyDraft,
    RecordPolicyDraft,
    StoredPolicyComparison,
)
from aris.modules.nova.policy_review import compare_policy_scenarios, policy_difference
from aris.modules.nova.policy_source import compile_policy, parse_policy_source

DRAFT_PERMISSION = "policy.manage_drafts"
GLOBAL_SCOPE = AccessScope(type="global", id="*")


class PolicyCatalogService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self.access = AccessService(sessions)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("policy catalog clock requires a timezone")
        return utc(value)

    def _authorize(self, session: Session, actor: ActorContext) -> None:
        self.access.require_in_transaction(
            session,
            AccessRequest(user_id=actor.actor_id, permission=DRAFT_PERMISSION, scope=GLOBAL_SCOPE),
        )

    @staticmethod
    def _view(record: PolicySourceRecord) -> PolicySourceView:
        try:
            encoded = record.canonical_json.encode("utf-8")
            source = compile_policy(parse_policy_source(encoded))
            if (
                source.canonical_json != encoded
                or source.fingerprint != record.fingerprint
                or source.source.metadata.name != record.name
                or source.source.metadata.version != record.source_version
            ):
                raise ValueError("stored source identity differs")
            normalized = RecordPolicyDraft(source=source.source, reason=record.reason)
            if normalized.reason != record.reason:
                raise ValueError("stored source reason differs")
            return PolicySourceView(
                id=record.id,
                source=source.source,
                fingerprint=source.fingerprint,
                created_by=record.created_by,
                command_id=record.command_id,
                request_id=record.request_id,
                reason=record.reason,
                created_at=utc(record.created_at),
            )
        except (DomainError, ValidationError, ValueError, UnicodeError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored policy source is inconsistent", 503
            ) from None

    def exact(self, actor: ActorContext, reference: PolicySourceReference) -> PolicySourceView:
        with self.sessions() as session:
            return self.exact_in_transaction(session, actor, reference)

    def exact_in_transaction(
        self, session: Session, actor: ActorContext, reference: PolicySourceReference
    ) -> PolicySourceView:
        with session.no_autoflush:
            self._authorize(session, actor)
            record = PolicyCatalogRepository(session).exact(reference)
            if record is None:
                raise DomainError("NOT_FOUND", "Policy source is unknown", 404)
            result = self._view(record)
            self._authorize(session, actor)
            return result

    def sources(self, actor: ActorContext, query: PolicySourceListQuery) -> PolicySourcePage:
        with self.sessions() as session:
            return self.sources_in_transaction(session, actor, query)

    def sources_in_transaction(
        self, session: Session, actor: ActorContext, query: PolicySourceListQuery
    ) -> PolicySourcePage:
        query = PolicySourceListQuery.model_validate(query.model_dump(mode="json"))
        with session.no_autoflush:
            self._authorize(session, actor)
            records = PolicyCatalogRepository(session).page(query)
            try:
                versions = tuple(
                    PolicySourceSummary(
                        id=record.id,
                        reference=PolicySourceReference(
                            name=record.name, version=record.source_version
                        ),
                        fingerprint=record.fingerprint,
                        created_at=utc(record.created_at),
                    )
                    for record in records[: query.limit]
                )
                if any(item.reference.name != query.name for item in versions):
                    raise ValueError("source directory binding differs")
            except (ValidationError, ValueError):
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Policy source directory is inconsistent", 503
                ) from None
            self._authorize(session, actor)
            return PolicySourcePage(
                name=query.name,
                versions=versions,
                next_after_version=versions[-1].reference.version
                if len(records) > query.limit
                else None,
            )

    def record_in_transaction(
        self,
        session: Session,
        execution: CommandExecution,
        principal: UserPrincipal,
        request: RecordPolicyDraft,
    ) -> RecordedPolicyDraft:
        if principal.user_id != execution.actor_id or execution.scope != GLOBAL_SCOPE:
            raise DomainError("PERMISSION_DENIED", "Policy source actor or scope differs", 403)
        request = RecordPolicyDraft.model_validate(request.model_dump(mode="json"))
        compiled = compile_policy(request.source)
        reference = PolicySourceReference(
            name=compiled.source.metadata.name, version=compiled.source.metadata.version
        )
        with transaction_savepoint(session):
            self._authorize(session, execution)
            now = self._now()
            record = PolicySourceRecord(
                id=str(uuid4()),
                name=reference.name,
                source_version=reference.version,
                canonical_json=compiled.canonical_json.decode("utf-8"),
                fingerprint=compiled.fingerprint,
                created_by=str(execution.actor_id),
                command_id=str(execution.command_id),
                request_id=execution.request_id,
                reason=request.reason,
                created_at=now,
            )
            repo = PolicyCatalogRepository(session)
            created = repo.insert_if_absent(record)
            # Unique-key arbitration can wait. Recheck current permission and
            # the command's original deadline after any such wait.
            self._authorize(session, execution)
            now = self._now()
            if execution.execution_deadline is not None and now >= execution.execution_deadline:
                raise DomainError("EXECUTION_EXPIRED", "Policy source command deadline passed", 409)
            stored = repo.exact(reference)
            if stored is None:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Policy source was not stored", 503)
            view = self._view(stored)
            if view.fingerprint != compiled.fingerprint:
                raise DomainError(
                    "CONFLICT", "Policy source version already has other content", 409
                )
            if created:
                event_id = str(uuid4())
                session.add(
                    DomainEvent(
                        event_id=event_id,
                        event_type="policy.draft_recorded",
                        event_version=1,
                        aggregate_type="policy_source",
                        aggregate_id=stored.id,
                        aggregate_version=1,
                        occurred_at=now,
                        recorded_at=now,
                        actor=str(execution.actor_id),
                        correlation_id=execution.request_id,
                        causation_id=str(execution.command_id),
                        payload={
                            "source_id": stored.id,
                            "name": reference.name,
                            "source_version": reference.version,
                            "fingerprint": compiled.fingerprint,
                            "catalog_entry_only": True,
                        },
                    )
                )
                session.flush()
                session.add(OutboxEntry(event_id=event_id, created_at=now))
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=str(execution.actor_id),
                        action="policy.draft_recorded",
                        target=stored.id,
                        request_id=execution.request_id,
                        recorded_at=now,
                        changes={
                            "command_id": str(execution.command_id),
                            "fingerprint": compiled.fingerprint,
                            "reason": request.reason,
                        },
                    )
                )
                session.flush()
            return RecordedPolicyDraft(
                id=view.id, reference=reference, fingerprint=view.fingerprint, created=created
            )

    def compare_sources(
        self, actor: ActorContext, query: PolicySourceComparisonQuery
    ) -> StoredPolicyComparison:
        with self.sessions() as session:
            return self.compare_sources_in_transaction(session, actor, query)

    def compare_sources_in_transaction(
        self, session: Session, actor: ActorContext, query: PolicySourceComparisonQuery
    ) -> StoredPolicyComparison:
        query = PolicySourceComparisonQuery.model_validate(query.model_dump(mode="json"))
        with session.no_autoflush:
            before = self.exact_in_transaction(session, actor, query.before)
            after = self.exact_in_transaction(session, actor, query.after)
            if (before.fingerprint, after.fingerprint) != (
                query.expected_before_fingerprint,
                query.expected_after_fingerprint,
            ):
                raise DomainError("CONFLICT", "Stored policy source fingerprint differs", 409)
            a, b = compile_policy(before.source), compile_policy(after.source)
            if query.scenarios:
                comparison = compare_policy_scenarios(a, b, query.scenarios)
                difference, cases = comparison.difference, comparison.cases
            else:
                difference, cases = policy_difference(a, b), ()
            self._authorize(session, actor)
            return StoredPolicyComparison(
                before_source_id=before.id,
                after_source_id=after.id,
                difference=difference,
                cases=cases,
            )
