from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, func, select, text

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, Permission, Role, RoleBinding, RolePermission
from aris.core.commands.schemas import ActorContext, CommandContext, CommandRequest, ConfirmCommand
from aris.core.contracts import DomainError
from aris.modules.nova.policy_approval_repo import PolicyApprovalRepository
from aris.modules.nova.policy_approval_review_schemas import (
    PolicyApprovalReviewQuery,
    PolicyApprovalReviewRequirements,
)
from aris.modules.nova.policy_approval_review_service import PolicyApprovalReviewService
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE
from aris.modules.nova.policy_history_record_schemas import (
    RecordHistoricalPolicyReview,
    historical_context_fingerprint,
)
from tests.test_policy_source_approvals import (
    adjustments,
    admin_state,
    approval,
    catalog_state,
    closed,
    command_state,
    confirm,
    coverage,
    derivations,
    evidence,
    history_state,
    history_surface,
    ledger,
    pending,
    pending_revoke,
    recording,
    review_surface,
    revoke_request,
    windows,
)

__all__ = [
    "adjustments",
    "admin_state",
    "approval",
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
def approval_review(approval):
    s = approval
    now = {"value": s.service.catalog._now()}
    s.service.catalog.clock = lambda: now["value"]
    s.bus.clock = lambda: now["value"]
    s.recording.bus.clock = lambda: now["value"]
    other = ActorContext(
        actor_id=UUID(str(s.recording.state.other)), request_id="second-reviewer-fixture"
    )
    role, binding = str(uuid4()), str(uuid4())
    with s.service.sessions.begin() as db:
        db.add(Role(id=role, name="fixture-secondary-approver"))
        db.flush()
        permissions = dict(db.execute(select(Permission.name, Permission.id)).all())
        db.add_all(
            RolePermission(role_id=role, permission_id=permissions[name])
            for name in ("policy.approve", "policy.manage_drafts", "usage.read_own")
        )
        db.add(
            RoleBinding(
                id=binding,
                user_id=str(other.actor_id),
                role_id=role,
                scope_type="global",
                scope_id="*",
            )
        )
        if not db.scalar(
            select(func.count())
            .select_from(ExternalIdentity)
            .where(
                ExternalIdentity.user_id == str(other.actor_id),
                ExternalIdentity.provider == "slack",
                ExternalIdentity.namespace == "TFIXTURE",
            )
        ):
            db.add(
                ExternalIdentity(
                    id=str(uuid4()),
                    user_id=str(other.actor_id),
                    provider="slack",
                    namespace="TFIXTURE",
                    external_id="USECOND",
                )
            )
    first_query = s.recording.request.query.model_copy(
        update={
            "policy": s.request.source,
            "expected_policy_fingerprint": s.request.expected_source_fingerprint,
            "usage": s.recording.request.query.usage.model_copy(
                update={"owner_id": other.actor_id}
            ),
        }
    )
    context = s.service.history.assessments.review(other, first_query)
    assert (
        context.evidence.coverage == "complete" and context.evidence.total_usage_microseconds == 0
    )
    original = RecordHistoricalPolicyReview(
        query=first_query,
        expected_implementation_ref=s.service.history.configuration.implementation_ref,
        expected_context_fingerprint=historical_context_fingerprint(
            context, s.service.history.configuration.implementation_ref
        ),
        reason="OTHER_PRIVATE_REVIEW_REASON must stay in own-only details",
    )
    proposal = s.recording.bus.execute(
        CommandContext(
            actor_id=other.actor_id,
            request_id=other.request_id,
            idempotency_key="second-reviewer-history",
        ),
        CommandRequest(
            command="policy.record_historical_review",
            scope=GLOBAL_SCOPE,
            parameters=original.model_dump(mode="json"),
        ),
    )
    result = s.recording.bus.confirm(
        other, ConfirmCommand(command_id=proposal.command_id, proof=proposal.proof)
    )
    other_request = s.request.model_copy(
        update={
            "history": (
                s.request.history[0].model_copy(
                    update={
                        "id": UUID(result.result["id"]),
                        "expected_fingerprint": result.result["fingerprint"],
                    }
                ),
            ),
            "reason": "OTHER_PRIVATE_APPROVAL_REASON must stay in own-only details",
        }
    )
    requirements = PolicyApprovalReviewRequirements(
        namespace=IdentityNamespace(provider="slack", namespace="TFIXTURE"),
        approval=s.service.requirements,
        minimum_distinct_approvers=2,
        signer_selection="latest_recorded_statement",
    )
    service = PolicyApprovalReviewService(s.service, requirements)
    query = PolicyApprovalReviewQuery(
        source=s.request.source,
        expected_source_fingerprint=s.request.expected_source_fingerprint,
        expected_review_requirements_fingerprint=requirements.fingerprint,
    )
    return SimpleNamespace(
        state=s,
        service=service,
        query=query,
        other=other,
        other_request=other_request,
        other_binding=binding,
        now=now,
    )


def endorse(r, who="first", *, key=None):
    r.now["value"] += timedelta(seconds=1)
    s = r.state
    if who == "first":
        return confirm(s, pending(s, key=key or str(uuid4())))
    ctx = CommandContext(
        actor_id=r.other.actor_id,
        request_id=r.other.request_id,
        idempotency_key=key or str(uuid4()),
    )
    value = s.bus.execute(
        ctx,
        CommandRequest(
            command="policy.approve_source",
            scope=GLOBAL_SCOPE,
            parameters=r.other_request.model_dump(mode="json"),
        ),
    )
    return s.bus.confirm(r.other, ConfirmCommand(command_id=value.command_id, proof=value.proof))


def test_current_review_counts_people_once_and_never_revives_a_retracted_older_statement(
    approval_review,
):
    r = approval_review
    s = r.state
    assert r.service.review(s.actor, r.query).qualifying_signers == 0
    endorse(r)
    endorse(r, "second")
    value = r.service.review(s.actor, r.query)
    assert (
        value.qualifying_signers == 2
        and value.outcome == "minimum_met"
        and not value.publication_authorized
    )
    repeated = endorse(r)
    value = r.service.review(s.actor, r.query)
    assert (
        value.qualifying_signers == 2
        and [item.state for item in value.entries].count("superseded") == 1
    )
    confirm(s, pending_revoke(s, revoke_request(s, repeated)))
    after = r.service.review(s.actor, r.query)
    assert after.qualifying_signers == 1 and after.outcome == "minimum_not_met"
    assert [
        item.state for item in after.entries if item.approval.approved_by == s.actor.actor_id
    ] == ["revoked", "superseded"]
    raw = after.model_dump_json()
    assert all(
        private not in raw
        for private in (
            "OTHER_PRIVATE_REVIEW_REASON",
            "OTHER_PRIVATE_APPROVAL_REASON",
            "quota_state",
            "models",
            "window_start_at",
        )
    )
    endorse(r)
    with s.service.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.id == r.other_binding))
    lost = r.service.review(s.actor, r.query)
    assert lost.qualifying_signers == 1 and any(
        item.state == "approver_unavailable" and item.approval.approved_by == r.other.actor_id
        for item in lost.entries
    )


def test_approval_review_binds_its_current_configuration_and_rejects_changed_statements(
    approval_review, monkeypatch
):
    r = approval_review
    s = r.state
    endorse(r)
    with pytest.raises(DomainError, match="requirements"):
        r.service.review(
            s.actor,
            r.query.model_copy(update={"expected_review_requirements_fingerprint": "f" * 64}),
        )
    original = PolicyApprovalRepository.review_candidates
    calls = []

    def changed(repo, source):
        values = original(repo, source)
        calls.append(1)
        return values if len(calls) == 1 else ()

    monkeypatch.setattr(PolicyApprovalRepository, "review_candidates", changed)
    with pytest.raises(DomainError, match="statements changed"):
        r.service.review(s.actor, r.query)
    monkeypatch.setattr(PolicyApprovalRepository, "review_candidates", original)
    engine = s.service.sessions.kw["bind"]
    if engine.dialect.name == "postgresql":
        statements = []

        def observe(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", observe)
        try:
            assert r.service.review(s.actor, r.query).qualifying_signers == 1
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert any("SET TRANSACTION READ ONLY" in sql for sql in statements)
        assert not any(
            sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE"))
            or "agent_messages" in sql
            or "usage_ledger_entries" in sql
            for sql in statements
        )
        with s.service.sessions() as blocker:
            blocker.execute(text("LOCK TABLE policy_source_approvals IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(DomainError) as locked:
                r.service.review(s.actor, r.query)
            assert locked.value.code == "RETRYABLE_DATABASE_ERROR"
            blocker.rollback()
        assert r.service.review(s.actor, r.query).qualifying_signers == 1
    with s.service.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.id == s.binding))
    with pytest.raises(DomainError) as failure:
        r.service.review(s.actor, r.query)
    assert failure.value.code == "PERMISSION_DENIED"


def test_truncated_approval_review_retains_unknown_but_can_prove_a_minimum_lower_bound(
    approval_review,
):
    r = approval_review
    s = r.state
    endorse(r, "second")
    for i in range(129):
        endorse(r, key="bounded-review-duplicate-" + str(i))
    result = r.service.review(s.actor, r.query)
    assert (
        len(result.entries) == 128
        and result.truncated
        and result.qualifying_signers == 1
        and result.outcome == "unknown"
    )
    endorse(r, "second")
    latest = r.service.review(s.actor, r.query)
    assert latest.truncated and latest.qualifying_signers == 2 and latest.outcome == "minimum_met"
