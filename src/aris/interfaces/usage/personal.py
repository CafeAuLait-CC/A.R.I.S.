"""Trusted-subject personal usage reads shared by native presentation adapters."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope, UserPrincipal
from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.usage_history import UsageHistoryQuery, calendar_boundaries

ReportPeriod = Literal["today", "week"]


class PersonalUsageSummary(AccessContract):
    assessment_scope: Literal["recorded_usage_only"] = "recorded_usage_only"
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    node_id: str
    period: ReportPeriod
    timezone: str
    start_at: AwareDatetime
    end_at: AwareDatetime
    read_at: AwareDatetime
    ledger_revision: int = Field(ge=0)
    recorded_microseconds: int = Field(ge=0)
    gpu_count: int = Field(ge=0)
    segment_count: int = Field(ge=0)


class PersonalUsageQueries:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        namespace: IdentityNamespace,
        node_ids: tuple[str, ...],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= len(node_ids) <= 8 or len(set(node_ids)) != len(node_ids):
            raise ValueError("personal usage requires explicit unique bounded nodes")
        for node in node_ids:
            AccessScope(type="node", id=node)
        self.namespace, self.node_ids = namespace, node_ids
        self.clock = clock or (lambda: datetime.now(UTC))
        self.usage = IndexedUsageHistoryService(sessions, clock=self.clock, timeout_ms=750)
        self.sessions, self.access = sessions, self.usage.access

    @staticmethod
    def permission(principal: UserPrincipal, node: str) -> AccessRequest:
        return AccessRequest(
            user_id=principal.user_id,
            permission="usage.read_own",
            scope=AccessScope(type="node", id=node),
        )

    @contextmanager
    def _session(self, context: ExternalCommandActor) -> Iterator[tuple[Session, UserPrincipal]]:
        context = ExternalCommandActor.model_validate(context.model_dump(mode="json"))
        if (context.subject.provider, context.subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "Usage identity namespace differs", 403)
        deadline = monotonic() + 1.5
        try:
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(text("SET TRANSACTION READ ONLY"))
                transaction_budget(session, 100, 500, 1500)
                principal = self.access.resolve_external_in_transaction(session, context.subject)
                if principal is None:
                    raise DomainError(
                        "PERMISSION_DENIED", "Current usage identity is unavailable", 403
                    )
                yield session, principal
                current = self.access.resolve_external_in_transaction(session, context.subject)
                if current is None or current.user_id != principal.user_id:
                    raise DomainError("PERMISSION_DENIED", "Current usage identity differs", 403)
                if monotonic() >= deadline:
                    raise DomainError(
                        "RETRYABLE_DATABASE_ERROR", "Personal usage read budget exceeded", 503
                    )
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Personal usage database is unavailable", 503
            ) from None

    def choices(self, context: ExternalCommandActor) -> tuple[str, ...]:
        with self._session(context) as (session, principal):
            allowed = []
            for node in self.node_ids:
                try:
                    self.access.require_in_transaction(session, self.permission(principal, node))
                    allowed.append(node)
                except DomainError as error:
                    if error.code != "PERMISSION_DENIED":
                        raise
            if not allowed:
                raise DomainError("PERMISSION_DENIED", "Personal usage permission is required", 403)
            for node in allowed:
                self.access.require_in_transaction(session, self.permission(principal, node))
            return tuple(allowed)

    def mine(
        self, context: ExternalCommandActor, node_id: str, period: ReportPeriod
    ) -> PersonalUsageSummary:
        if node_id not in self.node_ids:
            raise DomainError("PERMISSION_DENIED", "Node is outside the usage interface", 403)
        if period not in ("today", "week"):
            raise DomainError("VALIDATION_ERROR", "Unknown personal report period", 422)
        with self._session(context) as (session, principal):
            access = self.permission(principal, node_id)
            self.access.require_in_transaction(session, access)
            now = self.clock()
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("personal usage clock requires a timezone")
            now = now.astimezone(UTC)
            local = now.astimezone(ZoneInfo(principal.timezone)).date()
            first = local - timedelta(days=local.weekday()) if period == "week" else local
            start = calendar_boundaries(first, first, principal.timezone)[0]
            if start >= now:
                raise DomainError(
                    "WINDOW_NOT_STARTED", "The reporting window has just started", 409
                )
            result = self.usage.summary_in_transaction(
                session,
                ActorContext(actor_id=principal.user_id, request_id=context.request_id),
                UsageHistoryQuery(
                    node_id=node_id, start_at=start, end_at=now, timezone=principal.timezone
                ),
            )
            if result.owner_id != principal.user_id:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Personal usage owner differs", 503)
            summary = PersonalUsageSummary(
                node_id=node_id,
                period=period,
                timezone=principal.timezone,
                start_at=result.start_at,
                end_at=result.end_at,
                read_at=result.read_at,
                ledger_revision=result.snapshot.revision,
                recorded_microseconds=result.recorded_microseconds,
                gpu_count=result.gpu_count,
                segment_count=result.segment_count,
            )
            self.access.require_in_transaction(session, access)
            return summary
