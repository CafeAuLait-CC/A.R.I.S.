"""Read-only saved statistics: current access checks and bounded result paging."""

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Literal
from uuid import UUID

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.window_personal_projection import personal_window_view
from aris.modules.lyra.window_reader_schemas import (
    SavedWindowPage,
    SavedWindowView,
    WindowListQuery,
    WindowPageCursor,
)
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository


class UsageWindowReader:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        timeout_ms: int = 3000,
        personal_node_projection: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 10000:
            raise ValueError("saved usage reading requires a bounded timeout")
        self.sessions, self.timeout_ms = sessions, timeout_ms
        self.access = AccessService(sessions)
        self.personal_node_projection = personal_node_projection
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _request(
        actor: ActorContext, node: str, owner: UUID | None, mode: Literal["mine", "node"] | None
    ) -> tuple[AccessRequest, Literal["mine", "node"]]:
        selected = mode or ("mine" if owner == actor.actor_id else "node")
        if selected == "mine" and owner != actor.actor_id:
            raise DomainError(
                "PERMISSION_DENIED", "Personal statistics require the current user", 403
            )
        return AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_own" if selected == "mine" else "usage.read_all",
            scope=AccessScope(type="node", id=node),
        ), selected

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        code = database_failure(error)
        return DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Saved usage database is unavailable",
            503,
        )

    @staticmethod
    def _deadline(value: float) -> None:
        if monotonic() >= value:
            raise DomainError("RETRYABLE_DATABASE_ERROR", "Saved usage read budget exceeded", 503)

    def read(self, actor: ActorContext, id: UUID) -> SavedWindowView:
        try:
            with self.sessions() as session:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                return self.read_in_transaction(session, actor, id)
        except DBAPIError as error:
            raise self._failure(error) from None

    def require_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        node_id: str,
        owner_id: UUID | None,
        mode: Literal["mine", "node"],
    ) -> None:
        access, _ = self._request(actor, node_id, owner_id, mode)
        self.access.require_in_transaction(session, access)

    def node_snapshot_fingerprints(
        self, actor: ActorContext, node_id: str, ids: tuple[UUID, ...]
    ) -> dict[UUID, str]:
        if not 1 <= len(ids) <= 7 or len(set(ids)) != len(ids):
            raise ValueError("Expected up to seven distinct day snapshots")
        try:
            with self.sessions() as session, session.no_autoflush:
                transaction_budget(session, 100, 1000, self.timeout_ms)
                self.require_in_transaction(session, actor, node_id, None, "node")
                values = UsageWindowRepository(session).node_fingerprints(node_id, ids)
                self.require_in_transaction(session, actor, node_id, None, "node")
                return values
        except DBAPIError as error:
            raise self._failure(error) from None

    def read_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        id: UUID,
        *,
        node_id: str | None = None,
        mode: Literal["mine", "node"] | None = None,
    ) -> SavedWindowView:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        deadline = monotonic() + self.timeout_ms / 1000
        with session.no_autoflush:
            repo = UsageWindowRepository(session)
            scope = repo.scope(id)
            if scope is None or (node_id is not None and scope[0] != node_id):
                raise DomainError("NOT_FOUND", "Window snapshot not found", 404)
            project = self.personal_node_projection and mode == "mine" and scope[1] is None
            access, selected = self._request(
                actor, scope[0], actor.actor_id if project else scope[1], mode
            )
            self.access.require_in_transaction(session, access)
            value = repo.get(id)
            if (
                value is None
                or (value.request.query.node_id, value.request.query.owner_id) != scope
            ):
                raise DomainError("CONFLICT", "Window read scope changed", 409)
            window = value.window
            if project:
                result = personal_window_view(
                    self.sessions,
                    session,
                    actor,
                    value,
                    self.access,
                    deadline=deadline,
                    clock=self.clock,
                )
                self.access.require_in_transaction(session, access)
                self._deadline(deadline)
                return result
            cells = tuple(
                cell
                for cell in window.cells
                if selected == "node"
                or (cell.state == "recorded" and cell.owner_id == actor.actor_id)
            )
            result = SavedWindowView(
                id=id,
                query=window.query,
                created_at=value.created_at,
                captured_at=window.read_at,
                detail_scope=selected,
                coverage=window.coverage,
                source_prefix_complete=window.source_prefix_complete,
                recorded_microseconds=window.recorded_microseconds,
                verified_usage_microseconds=window.verified_usage_microseconds,
                total_usage_microseconds=window.total_usage_microseconds,
                covered_gpu_microseconds=window.covered_gpu_microseconds,
                unknown_gpu_microseconds=window.unknown_gpu_microseconds,
                uncovered_gpu_microseconds=window.uncovered_gpu_microseconds,
                cells=cells,
                source_fingerprint=window.source.fingerprint,
                snapshot_fingerprint=value.fingerprint,
            )
            self.access.require_in_transaction(session, access)
            self._deadline(deadline)
            return result

    def list_in_transaction(
        self,
        session: Session,
        actor: ActorContext,
        query: WindowListQuery,
        *,
        mode: Literal["mine", "node"] | None = None,
    ) -> SavedWindowPage:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        query = WindowListQuery.model_validate(query.model_dump(mode="json"))
        access, _ = self._request(actor, query.node_id, query.owner_id, mode)
        deadline = monotonic() + self.timeout_ms / 1000
        with session.no_autoflush:
            self.access.require_in_transaction(session, access)
            values = UsageWindowRepository(session).summaries(
                query, include_node_for_owner=self.personal_node_projection and mode == "mine"
            )
            has_more = len(values) > query.limit
            result = SavedWindowPage(
                query=query,
                items=values[: query.limit],
                next_cursor=WindowPageCursor(
                    created_at=values[query.limit - 1].created_at, id=values[query.limit - 1].id
                )
                if has_more
                else None,
            )
            self.access.require_in_transaction(session, access)
            self._deadline(deadline)
            return result
