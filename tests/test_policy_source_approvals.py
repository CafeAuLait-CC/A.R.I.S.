from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import delete, event, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission
from aris.core.commands.schemas import CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.db import SCHEMA_REVISION
from aris.core.events import DomainEvent
from aris.modules.nova.models import PolicySourceApprovalRow
from aris.modules.nova.policy_approval_commands import ApprovePolicySourceCommand
from aris.modules.nova.policy_approval_schemas import (
    ApprovalHistoryReference,
    ApprovePolicySource,
    PolicyApprovalDetailQuery,
    PolicyApprovalListQuery,
    PolicyApprovalRequirements,
)
from aris.modules.nova.policy_approval_service import PolicyApprovalService
from aris.modules.nova.policy_catalog_commands import RecordPolicyDraftCommand
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE
from aris.modules.nova.policy_history_record_schemas import historical_context_fingerprint
from tests.test_policy_history_records import (
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
    recording,
    review_surface,
    windows,
)
from tests.test_policy_history_records import confirm as save_confirm
from tests.test_policy_history_records import pending as save_pending

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
    "recording",
    "review_surface",
    "windows",
]


@pytest.fixture
def approval(recording):
    s = recording
    catalog, actor = s.service.catalog, s.state.actor
    source = catalog.exact(actor, s.request.query.policy)
    raw = source.source.model_dump(mode="json")
    raw["metadata"]["version"] = 2
    raw["spec"]["quota"]["node_limits"] = raw["spec"]["quota"]["node_limits"][:1]
    raw["spec"]["role_overrides"] = []
    raw["spec"]["idle_reclaim"] = {"mode": "disabled"}
    drafts = CommandService(
        s.service.sessions,
        [RecordPolicyDraftCommand(catalog)],
        confirmation_key=SecretStr("fixture-source-approval-draft-key-123456789"),
        clock=catalog.clock,
    )
    ctx = CommandContext(
        actor_id=actor.actor_id,
        request_id="approval-fixture",
        idempotency_key="approval-source-fixture",
    )
    pending = drafts.execute(
        ctx,
        CommandRequest(
            command="policy.record_draft",
            scope=GLOBAL_SCOPE,
            parameters={"source": raw, "reason": "Explicit approval fixture source"},
        ),
    )
    made = drafts.confirm(actor, ConfirmCommand(command_id=pending.command_id, proof=pending.proof))
    reference = PolicySourceReference(name=source.source.metadata.name, version=2)
    query = s.request.query.model_copy(
        update={"policy": reference, "expected_policy_fingerprint": made.result["fingerprint"]}
    )
    review = s.service.assessments.review(actor, query)
    save = s.request.model_copy(
        update={
            "query": query,
            "expected_context_fingerprint": historical_context_fingerprint(
                review, s.service.configuration.implementation_ref
            ),
        }
    )
    saved = save_confirm(s, save_pending(s, request=save, key="approval-original-history"))
    role, permission, binding = str(uuid4()), str(uuid4()), str(uuid4())
    with s.service.sessions.begin() as db:
        db.add_all(
            [
                Role(id=role, name="fixture-source-approver"),
                Permission(id=permission, name="policy.approve"),
            ]
        )
        db.flush()
        db.add(RolePermission(role_id=role, permission_id=permission))
        db.add(
            RoleBinding(
                id=binding,
                user_id=str(actor.actor_id),
                role_id=role,
                scope_type="global",
                scope_id="*",
            )
        )
    requirements = PolicyApprovalRequirements(
        history=s.service.configuration,
        allow_source_author_approval=True,
        minimum_window_seconds=30,
        maximum_history_age_seconds=int(
            (catalog._now() - review.evidence.window.end_at).total_seconds()
        )
        + 86400,
        require_evaluated_quota=False,
        validity_seconds=3600,
    )
    service = PolicyApprovalService(catalog, s.service, requirements, writing_enabled=True)
    request = ApprovePolicySource(
        source=reference,
        expected_source_fingerprint=made.result["fingerprint"],
        expected_requirements_fingerprint=requirements.fingerprint,
        history=(
            ApprovalHistoryReference(
                node_id=query.usage.node_id,
                id=saved.result["id"],
                expected_fingerprint=saved.result["fingerprint"],
            ),
        ),
        reason="Approve this exact source using the fixed historical sample",
    )
    bus = CommandService(
        s.service.sessions,
        [ApprovePolicySourceCommand(service)],
        confirmation_key=SecretStr("fixture-human-source-approval-key-123456789"),
        clock=catalog.clock,
    )
    return SimpleNamespace(
        recording=s, service=service, request=request, bus=bus, binding=binding, actor=actor
    )


def pending(s, *, key="approval-fixture", request=None):
    return s.bus.execute(
        CommandContext(actor_id=s.actor.actor_id, request_id="approval-test", idempotency_key=key),
        CommandRequest(
            command="policy.approve_source",
            scope=GLOBAL_SCOPE,
            parameters=(request or s.request).model_dump(mode="json"),
        ),
    )


def confirm(s, value):
    return s.bus.confirm(s.actor, ConfirmCommand(command_id=value.command_id, proof=value.proof))


def counts(s):
    with s.service.sessions() as db:
        return db.scalar(select(func.count()).select_from(PolicySourceApprovalRow)), db.scalar(
            select(func.count())
            .select_from(DomainEvent)
            .where(DomainEvent.event_type == "policy.source_approved")
        )


def selection(s, result):
    return PolicyApprovalDetailQuery(
        source=s.request.source,
        expected_source_fingerprint=s.request.expected_source_fingerprint,
        id=result.result["id"],
        expected_fingerprint=result.result["fingerprint"],
    )


def directory(s, **changes):
    return PolicyApprovalListQuery(
        source=s.request.source,
        expected_source_fingerprint=s.request.expected_source_fingerprint,
        **changes,
    )


def test_human_approval_is_confirmed_exact_and_individual_without_activation(approval):
    s = approval
    original = s.service.preview(s.actor, s.request)
    assert (
        original.samples[0].quota_state == "not_evaluated" and not original.publication_authorized
    )
    proposal = pending(s)
    assert proposal.state == "awaiting_confirmation" and counts(s) == (0, 0)
    result = confirm(s, proposal)
    assert result.state == "succeeded" and counts(s) == (1, 1)
    stored = s.service.get(s.actor, selection(s, result))
    assert stored.preview == original and stored.approved_by == s.actor.actor_id
    assert stored.expires_at - stored.approved_at == timedelta(hours=1)
    assert not stored.publication_authorized and not stored.activation_performed
    assert confirm(s, proposal).duplicate and counts(s) == (1, 1)
    assert s.service.list(s.actor, directory(s)).items[0].id == stored.id
    s.service.requirements = s.service.requirements.model_copy(update={"validity_seconds": 60})
    assert s.service.get(s.actor, selection(s, result)) == stored
    with pytest.raises(DomainError, match="requirements"):
        s.service.preview(s.actor, s.request)


def test_approval_requires_its_own_permission_and_explicit_complete_fresh_samples(approval):
    s = approval
    original = s.service.requirements
    for changes, code in [
        ({"allow_source_author_approval": False}, "PERMISSION_DENIED"),
        ({"require_evaluated_quota": True}, "POLICY_REVIEW_INCOMPLETE"),
        ({"minimum_window_seconds": 1000}, "POLICY_REVIEW_EXPIRED"),
        ({"maximum_history_age_seconds": 1}, "POLICY_REVIEW_EXPIRED"),
    ]:
        s.service.requirements = original.model_copy(update=changes)
        with pytest.raises(DomainError) as failure:
            s.service.preview(
                s.actor,
                s.request.model_copy(
                    update={"expected_requirements_fingerprint": s.service.requirements.fingerprint}
                ),
            )
        assert failure.value.code == code
    s.service.requirements = original
    with pytest.raises(DomainError, match="fingerprint"):
        s.service.preview(
            s.actor,
            s.request.model_copy(
                update={
                    "history": (
                        s.request.history[0].model_copy(update={"expected_fingerprint": "f" * 64}),
                    )
                }
            ),
        )
    with s.service.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.id == s.binding))
    with pytest.raises(DomainError) as failure:
        pending(s)
    assert failure.value.code == "PERMISSION_DENIED" and counts(s) == (0, 0)


def test_approval_and_atomic_event_roll_back_and_changed_requirements_cannot_use_old_confirmation(
    approval, monkeypatch
):
    s = approval
    original = s.service._event
    proposal = pending(s)

    def failed(db, execution, value):
        original(db, execution, value)
        raise RuntimeError("synthetic approval transaction failure")

    monkeypatch.setattr(s.service, "_event", failed)
    with pytest.raises(RuntimeError):
        confirm(s, proposal)
    assert counts(s) == (0, 0)
    monkeypatch.setattr(s.service, "_event", original)
    assert confirm(s, proposal).state == "succeeded"
    next = pending(s, key="second-explicit-approval")
    s.service.requirements = s.service.requirements.model_copy(update={"validity_seconds": 60})
    with pytest.raises(DomainError, match="requirements"):
        confirm(s, next)
    assert counts(s) == (1, 1)


def test_source_approval_is_immutable_and_nonempty_downgrade_is_refused(approval, monkeypatch):
    s = approval
    confirm(s, pending(s))
    statements = [
        "UPDATE policy_source_approvals SET request_id='changed'",
        "DELETE FROM policy_source_approvals",
    ]
    if s.service.sessions.kw["bind"].dialect.name == "postgresql":
        statements.append("TRUNCATE policy_source_approvals")
    for sql in statements:
        with s.service.sessions.begin() as db, pytest.raises(DBAPIError):
            db.execute(text(sql))
    monkeypatch.setenv(
        "ARIS_DATABASE_URL", s.service.sessions.kw["bind"].url.render_as_string(hide_password=False)
    )
    with pytest.raises(RuntimeError, match="source approval records prevent downgrade"):
        command.downgrade(Config("alembic.ini"), "0032")
    with s.service.sessions() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION
    assert counts(s) == (1, 1)


def test_postgresql_approval_directory_is_read_only_metadata_and_recovers_after_lock(approval):
    s = approval
    engine = s.service.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only and lock budget")
    result = confirm(s, pending(s))
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        page = s.service.list(s.actor, directory(s))
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(page.items) == 1 and any("SET TRANSACTION READ ONLY" in sql for sql in statements)
    assert not any("canonical_approval" in sql or "agent_messages" in sql for sql in statements)
    with s.service.sessions() as blocker:
        blocker.execute(text("LOCK TABLE policy_source_approvals IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as failure:
            s.service.list(s.actor, directory(s))
        assert failure.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert s.service.get(s.actor, selection(s, result)).id == page.items[0].id
