"""Project the current member from an immutable node snapshot without writing one."""

from collections.abc import Callable
from datetime import datetime
from time import monotonic

from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.service import AccessService
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.usage_history import UsageHistoryQuery
from aris.modules.lyra.window_reader_schemas import SavedWindowView, personal_view_fingerprint
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow


def personal_window_view(
    sessions: sessionmaker[Session],
    session: Session,
    actor: ActorContext,
    stored: StoredUsageWindow,
    access: AccessService,
    *,
    deadline: float,
    clock: Callable[[], datetime],
) -> SavedWindowView:
    window = stored.window
    if window.query.owner_id is not None:
        raise DomainError("PERMISSION_DENIED", "Only node snapshots can be projected", 403)
    remaining = int((deadline - monotonic()) * 1000)
    if remaining < 100:
        raise DomainError("RETRYABLE_DATABASE_ERROR", "Saved personal read budget exceeded", 503)
    history = IndexedUsageHistoryService(sessions, timeout_ms=min(10000, remaining), clock=clock)
    history.access = access
    original = history.read_frozen_in_transaction(
        session,
        actor,
        UsageHistoryQuery(
            node_id=window.query.node_id,
            start_at=window.query.start_at,
            end_at=window.query.end_at,
            timezone=window.query.timezone,
            snapshot=window.ledger,
        ),
        read_at=window.read_at,
        mode="mine",
    )
    cells = tuple(
        cell
        for cell in window.cells
        if cell.state == "recorded" and cell.owner_id == actor.actor_id
    )
    verified = sum(cell.microseconds for cell in cells)
    draft = SavedWindowView.model_construct(
        id=stored.request.id,
        query=window.query.model_copy(update={"owner_id": actor.actor_id}),
        created_at=stored.created_at,
        captured_at=window.read_at,
        detail_scope="mine",
        coverage=window.coverage,
        source_prefix_complete=window.source_prefix_complete,
        recorded_microseconds=original.recorded_microseconds,
        verified_usage_microseconds=verified,
        total_usage_microseconds=verified if window.coverage == "complete" else None,
        covered_gpu_microseconds=window.covered_gpu_microseconds,
        unknown_gpu_microseconds=window.unknown_gpu_microseconds,
        uncovered_gpu_microseconds=window.uncovered_gpu_microseconds,
        cells=cells,
        source_fingerprint=window.source.fingerprint,
        snapshot_fingerprint=stored.fingerprint,
        projection="personal_from_node",
        recorded_fingerprint=original.fingerprint,
        view_fingerprint=None,
    )
    try:
        return SavedWindowView.model_validate(
            {**draft.model_dump(mode="json"), "view_fingerprint": personal_view_fingerprint(draft)}
        )
    except ValueError:
        raise DomainError(
            "DEPENDENCY_UNAVAILABLE", "Personal snapshot evidence is inconsistent", 503
        ) from None
