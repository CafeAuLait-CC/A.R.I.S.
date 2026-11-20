from types import SimpleNamespace
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.commands.schemas import CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.db import SCHEMA_REVISION
from aris.core.events import DomainEvent
from aris.interfaces.policy.history import PolicyHistoryReviewInput
from aris.modules.nova.models import PolicyHistoricalReviewRow
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE
from aris.modules.nova.policy_history_record_commands import RecordHistoricalPolicyReviewCommand
from aris.modules.nova.policy_history_record_schemas import (
    HistoricalReviewConfiguration,
    HistoricalReviewDetailQuery,
    HistoricalReviewListQuery,
    RecordHistoricalPolicyReview,
    historical_context_fingerprint,
)
from aris.modules.nova.policy_history_record_service import PolicyHistoricalReviewService
from tests.test_policy_history_review import (
    TOKEN,
    adjustments,
    admin_state,
    catalog_state,
    closed,
    command_state,
    coverage,
    derivations,
    evidence,
    history_state,
    history_surface,
    ledger,
    query,
    review_surface,
    windows,
)

__all__ = [
    "adjustments",
    "admin_state",
    "catalog_state",
    "closed",
    "command_state",
    "coverage",
    "derivations",
    "evidence",
    "history_state",
    "history_surface",
    "ledger",
    "review_surface",
    "windows",
]


@pytest.fixture
def recording(review_surface):
    surface, state = review_surface
    catalog = surface.catalog
    catalog.clock = lambda: state.saved.created_at
    configuration = HistoricalReviewConfiguration(
        implementation_ref=surface.historical_review_implementation_ref,
        nodes=surface.historical_usage.nodes,
    )
    service = PolicyHistoricalReviewService(
        catalog, surface.historical_usage.assessments, configuration, writing_enabled=True
    )
    value = surface.history_review(
        "Bearer " + TOKEN, "history-save-fixture", PolicyHistoryReviewInput(**query(surface, state))
    ).review
    request = RecordHistoricalPolicyReview(
        query=value.assessment.query,
        expected_implementation_ref=configuration.implementation_ref,
        expected_context_fingerprint=historical_context_fingerprint(
            value, configuration.implementation_ref
        ),
        reason="Keep the fixed historical evidence for review",
    )
    bus = CommandService(
        service.sessions,
        [RecordHistoricalPolicyReviewCommand(service)],
        confirmation_key=SecretStr("fixture-history-record-confirmation-key-123456789"),
        clock=catalog.clock,
    )
    return SimpleNamespace(
        surface=surface, state=state, service=service, request=request, bus=bus, value=value
    )


def pending(s, *, request=None, key="history-save"):
    return s.bus.execute(
        CommandContext(
            actor_id=s.state.actor.actor_id, request_id="history-record-test", idempotency_key=key
        ),
        CommandRequest(
            command="policy.record_historical_review",
            scope=GLOBAL_SCOPE,
            parameters=(request or s.request).model_dump(mode="json"),
        ),
    )


def confirm(s, value):
    return s.bus.confirm(
        s.state.actor, ConfirmCommand(command_id=value.command_id, proof=value.proof)
    )


def counts(s):
    with s.service.sessions() as db:
        return (
            db.scalar(select(func.count()).select_from(PolicyHistoricalReviewRow)),
            db.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "policy.historical_review_recorded")
            ),
        )


def selection(s, result):
    return HistoricalReviewDetailQuery(
        source=s.request.query.policy,
        expected_source_fingerprint=s.request.query.expected_policy_fingerprint,
        node_id=s.request.query.usage.node_id,
        id=result.result["id"],
        expected_fingerprint=result.result["fingerprint"],
    )


def directory(s, **changes):
    return HistoricalReviewListQuery(
        source=s.request.query.policy,
        expected_source_fingerprint=s.request.query.expected_policy_fingerprint,
        node_id=s.request.query.usage.node_id,
        **changes,
    )


def test_confirmed_historical_record_keeps_original_evidence_and_does_not_reinterpret_it(recording):
    s = recording
    draft = pending(s)
    assert draft.state == "awaiting_confirmation" and counts(s) == (0, 0)
    result = confirm(s, draft)
    assert result.state == "succeeded" and counts(s) == (1, 1)
    assert result.result["historical_only"] and not result.result["approval_performed"]
    fixed = s.service.get(s.state.actor, selection(s, result))
    assert fixed.review == s.value and fixed.request == s.request
    assert fixed.created_by == s.state.actor.actor_id and fixed.command_id == draft.command_id
    assert fixed.review.evidence.total_usage_microseconds == 30_000_000
    assert fixed.review.assessment.state == "not_evaluated"
    assert s.service.list(s.state.actor, directory(s)).items[0].id == fixed.id
    assert confirm(s, draft).duplicate and counts(s) == (1, 1)
    s.service.configuration = s.service.configuration.model_copy(
        update={"implementation_ref": "newer-implementation"}
    )
    assert s.service.get(s.state.actor, selection(s, result)) == fixed
    with pytest.raises(DomainError, match="implementation changed"):
        s.service.preview(s.state.actor, s.request)


def test_context_owner_methods_and_confirmed_fingerprint_are_checked_before_saving(recording):
    s = recording
    changes = [
        {"expected_context_fingerprint": "f" * 64},
        {"expected_implementation_ref": "wrong-implementation"},
        {"query": s.request.query.model_copy(update={"expected_timing_fingerprint": "f" * 64})},
        {
            "query": s.request.query.model_copy(
                update={
                    "usage": s.request.query.usage.model_copy(
                        update={"owner_id": UUID(str(s.state.other))}
                    )
                }
            )
        },
    ]
    for fields in changes:
        with pytest.raises(DomainError) as failure:
            s.service.preview(s.state.actor, s.request.model_copy(update=fields))
        assert failure.value.code in {"CONFLICT", "PERMISSION_DENIED"}
    draft = pending(s)
    s.service.configuration = s.service.configuration.model_copy(
        update={"implementation_ref": "changed-after-confirmation-preview"}
    )
    with pytest.raises(DomainError) as failure:
        confirm(s, draft)
    assert failure.value.code == "CONFLICT" and counts(s) == (0, 0)


def test_historical_record_and_events_roll_back_together_then_original_confirmation_can_retry(
    recording, monkeypatch
):
    s = recording
    original = s.service._event

    def failed(session, execution, value):
        original(session, execution, value)
        raise RuntimeError("synthetic failure before transaction commit")

    draft = pending(s)
    monkeypatch.setattr(s.service, "_event", failed)
    with pytest.raises(RuntimeError):
        confirm(s, draft)
    assert counts(s) == (0, 0)
    monkeypatch.setattr(s.service, "_event", original)
    assert confirm(s, draft).state == "succeeded" and counts(s) == (1, 1)


def test_historical_records_reject_update_delete_and_nonempty_downgrade(recording, monkeypatch):
    s = recording
    confirm(s, pending(s))
    dialect = s.service.sessions.kw["bind"].dialect.name
    statements = [
        "UPDATE policy_historical_reviews SET implementation_ref='changed'",
        "DELETE FROM policy_historical_reviews",
    ]
    if dialect == "postgresql":
        statements.append("TRUNCATE policy_historical_reviews")
    for sql in statements:
        with s.service.sessions.begin() as db, pytest.raises(DBAPIError):
            db.execute(text(sql))
    monkeypatch.setenv(
        "ARIS_DATABASE_URL", s.service.sessions.kw["bind"].url.render_as_string(hide_password=False)
    )
    with pytest.raises(RuntimeError, match="historical review records prevent downgrade"):
        command.downgrade(Config("alembic.ini"), "0031")
    with s.service.sessions() as db:
        # Both migration backends now roll back later empty revisions atomically.
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    assert counts(s) == (1, 1)


def test_postgresql_historical_directory_is_read_only_bounded_and_recovers_after_lock(recording):
    s = recording
    engine = s.service.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY and lock budget")
    result = confirm(s, pending(s))
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        page = s.service.list(s.state.actor, directory(s))
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(page.items) == 1 and any("SET TRANSACTION READ ONLY" in sql for sql in statements)
    assert not any("canonical_record" in sql or "agent_messages" in sql for sql in statements)
    with s.service.sessions() as blocker:
        blocker.execute(text("LOCK TABLE policy_historical_reviews IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as failure:
            s.service.list(s.state.actor, directory(s))
        assert failure.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert s.service.get(s.state.actor, selection(s, result)).id == page.items[0].id
