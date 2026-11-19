import json
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text

from aris.core.access.models import RoleBinding, RolePermission
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.quota_evidence_schemas import (
    HistoricalModelUsage,
    HistoricalQuotaEvidence,
    QuotaEvidenceSelection,
    quota_evidence_fingerprint,
)
from aris.modules.lyra.quota_evidence_service import HistoricalQuotaEvidenceService
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_source import compile_policy, parse_policy_source
from aris.modules.nova.quota_history_schemas import HistoricalQuotaQuery
from aris.modules.nova.quota_history_service import (
    HistoricalQuotaAssessmentService,
    historical_quota_assessment,
)
from tests.test_administration import admin_state
from tests.test_closed_run_recording import closed
from tests.test_commands import command_state
from tests.test_mapping_history import history_state
from tests.test_policy_catalog import catalog_state, confirmed, reference
from tests.test_policy_source import policy
from tests.test_processing_coverage import coverage
from tests.test_recorded_usage import reading_service
from tests.test_trace_consumer import CONTEXT
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import ledger
from tests.test_usage_reversals import adjustments, confirm, pending
from tests.test_window_snapshots import counts, incoming_window, windows

__all__ = [
    "admin_state",
    "history_state",
    "derivations",
    "ledger",
    "closed",
    "coverage",
    "windows",
    "adjustments",
    "command_state",
    "catalog_state",
]


def selection(saved, owner):
    return QuotaEvidenceSelection(
        snapshot_id=saved.request.id,
        expected_snapshot_fingerprint=saved.fingerprint,
        node_id=saved.window.query.node_id,
        owner_id=owner,
    )


@pytest.fixture
def evidence(windows):
    _, actor, other, ids = reading_service(windows.state.ledger)
    service = HistoricalQuotaEvidenceService(windows.service.sessions)
    saved = windows.service.capture(CONTEXT, incoming_window(windows)).snapshot
    return SimpleNamespace(
        service=service, actor=actor, other=other, ids=ids, saved=saved, windows=windows
    )


def test_saved_evidence_uses_original_models_and_projects_only_the_requested_owner(evidence):
    s = evidence
    before = counts(s.windows.service)
    result = s.service.read(s.actor, selection(s.saved, s.actor.actor_id))
    assert result.historical_only and not result.current_quota_authorization
    assert result.total_usage_microseconds == 30_000_000
    assert result.models[0].model == s.windows.state.run.model
    assert result.models[0].observed_microseconds == 30_000_000
    assert result.ledger == s.saved.window.ledger
    assert result.configuration_fingerprint == s.saved.window.configuration.fingerprint
    assert result.timing_fingerprint == s.saved.window.configuration.recording.timing_fingerprint
    # A node snapshot can be safely projected under own-only access.
    with s.service.sessions.begin() as session:
        session.execute(delete(RolePermission).where(RolePermission.permission_id == s.ids["all"]))
    assert s.service.read(s.actor, selection(s.saved, s.actor.actor_id)) == result
    assert str(s.other) not in result.model_dump_json()
    assert counts(s.windows.service) == before


def test_partial_and_reversed_windows_keep_null_while_old_evidence_remains_fixed(
    evidence, adjustments
):
    s = evidence
    old = s.service.read(s.actor, selection(s.saved, s.actor.actor_id))
    partial = s.windows.service.capture(
        CONTEXT, incoming_window(s.windows, manifests=(s.windows.manifests[0],))
    ).snapshot
    unknown = s.service.read(s.actor, selection(partial, s.actor.actor_id))
    assert (
        unknown.coverage != "complete"
        and unknown.models is unknown.total_usage_microseconds is None
    )
    assert confirm(adjustments, pending(adjustments)).state == "succeeded"
    current = s.windows.service.capture(CONTEXT, incoming_window(s.windows)).snapshot
    latest = s.service.read(s.actor, selection(current, s.actor.actor_id))
    assert latest.models is latest.total_usage_microseconds is None
    assert s.service.read(s.actor, selection(s.saved, s.actor.actor_id)) == old


def test_wrong_scope_fingerprint_and_revoked_access_cannot_return_evidence(evidence):
    s = evidence
    choice = selection(s.saved, s.actor.actor_id)
    for changes, code in [
        ({"node_id": "node-b"}, "PERMISSION_DENIED"),
        ({"snapshot_id": uuid4()}, "NOT_FOUND"),
        ({"expected_snapshot_fingerprint": "f" * 64}, "CONFLICT"),
    ]:
        with pytest.raises(DomainError) as error:
            s.service.read(s.actor, choice.model_copy(update=changes))
        assert error.value.code == code
    own = s.windows.service.capture(
        CONTEXT, incoming_window(s.windows, owner=s.actor.actor_id)
    ).snapshot
    with pytest.raises(DomainError) as error:
        s.service.read(s.actor, selection(own, s.other))
    assert error.value.code == "NOT_FOUND"
    with s.service.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.id == s.ids["binding"]))
    with pytest.raises(DomainError) as error:
        s.service.require_access(s.actor, choice)
    assert error.value.code == "PERMISSION_DENIED"


def test_complete_window_can_prove_zero_for_a_different_authorized_owner(evidence):
    s = evidence
    result = s.service.read(s.actor, selection(s.saved, s.other))
    assert result.coverage == "complete" and result.total_usage_microseconds == 0
    assert result.models == () and str(result.window.owner_id) == s.other
    assert str(s.actor.actor_id) not in result.model_dump_json()


def test_postgresql_reads_fixed_facts_but_rechecks_current_permissions_after_computation(
    evidence, monkeypatch
):
    s = evidence
    if s.service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL repeatable-read and independent current authorization")
    original = s.service._build

    def revoked(session, choice, stored):
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        result = original(session, choice, stored)
        with s.service.sessions.begin() as writer:
            writer.execute(delete(RoleBinding).where(RoleBinding.id == s.ids["binding"]))
        return result

    monkeypatch.setattr(s.service, "_build", revoked)
    with pytest.raises(DomainError) as error:
        s.service.read(s.actor, selection(s.saved, s.actor.actor_id))
    assert error.value.code == "PERMISSION_DENIED"


def test_postgresql_lock_timeout_does_not_write_history_and_recovers(evidence):
    s = evidence
    if s.service.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL real lock wait")
    before = counts(s.windows.service)
    with s.service.sessions() as blocker:
        blocker.execute(text("LOCK TABLE usage_window_snapshots IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            s.service.read(s.actor, selection(s.saved, s.actor.actor_id))
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert (
        s.service.read(s.actor, selection(s.saved, s.actor.actor_id)).total_usage_microseconds
        == 30_000_000
    )
    assert counts(s.windows.service) == before


def synthetic_evidence(**changes):
    # Typed reader fixture for pure Nova contract cases; never a persisted or trusted app fact.
    start = datetime(2026, 11, 16, tzinfo=UTC)
    owner = uuid4()
    values = dict(
        selection=QuotaEvidenceSelection(
            snapshot_id=uuid4(),
            expected_snapshot_fingerprint="a" * 64,
            node_id="node-a",
            owner_id=owner,
        ),
        window=UsageWindowQuery(
            node_id="node-a", owner_id=owner, start_at=start, end_at=start + timedelta(hours=1)
        ),
        captured_at=start + timedelta(hours=2),
        created_at=start + timedelta(hours=2),
        source_fingerprint="b" * 64,
        source_ceiling=8,
        gpu_count=1,
        ledger=UsageLedgerHead(node_id="node-a", revision=1, fingerprint="c" * 64),
        configuration_fingerprint="d" * 64,
        timing_fingerprint="e" * 64,
        recording_start_at=start,
        identity_revision=1,
        coverage="complete",
        source_prefix_complete=True,
        total_usage_microseconds=3_000_001,
        models=(HistoricalModelUsage(model="fixture GPU", observed_microseconds=3_000_001),),
        snapshot_window_fingerprint="f" * 64,
        fingerprint="0" * 64,
    )
    values.update(changes)
    draft = HistoricalQuotaEvidence.model_construct(**values)
    return HistoricalQuotaEvidence.model_validate(
        {**draft.model_dump(), "fingerprint": quota_evidence_fingerprint(draft)}
    )


def quota_source():
    return compile_policy(
        parse_policy_source(
            json.dumps(
                policy(
                    timezone="UTC",
                    quota={
                        "scope": "per_node",
                        "basis": "actual_gpu_usage",
                        "window": {"kind": "calendar_week", "week_starts_on": "monday"},
                        "node_limits": [
                            {"node_id": "node-a", "limit": {"kind": "limited", "gpu_seconds": 1}},
                            {
                                "node_id": "node-b",
                                "limit": {"kind": "limited", "gpu_seconds": 168 * 3600},
                            },
                        ],
                    },
                    gpu_time_weights={
                        "default_weight": {"numerator": 1, "denominator": 1},
                        "model_weights": [
                            {"model": "fixture GPU", "weight": {"numerator": 1, "denominator": 3}}
                        ],
                    },
                    role_overrides=[
                        {
                            "role": "fixture-reviewer",
                            "priority": 1,
                            "limits": {"quota_limit": {"kind": "limited", "gpu_seconds": 2}},
                        }
                    ],
                )
            ).encode()
        )
    )


def nova_fixture(value=None, compiled=None):
    value, compiled = value or synthetic_evidence(), compiled or quota_source()

    query = HistoricalQuotaQuery(
        policy=PolicySourceReference(
            name=compiled.source.metadata.name, version=compiled.source.metadata.version
        ),
        expected_policy_fingerprint=compiled.fingerprint,
        usage=value.selection,
        expected_configuration_fingerprint=value.configuration_fingerprint,
        expected_timing_fingerprint=value.timing_fingerprint,
        period_at=value.window.end_at,
    )
    return (
        compiled,
        query,
        value,
    )


def test_nova_applies_exact_original_model_rates_and_explicit_role_assumptions():
    compiled, query, value = nova_fixture()
    result = historical_quota_assessment(compiled, query, value)
    used = result.used_gpu_seconds
    assert Fraction(used.numerator, used.denominator) == Fraction(3_000_001, 3_000_000)
    assert result.state == "exhausted" and result.remaining_gpu_seconds.numerator == 0
    assert (
        result.historical_only and not result.booking_authorization and not result.policy_activated
    )
    assert result.sampled_until == value.window.end_at
    role = historical_quota_assessment(
        compiled, query.model_copy(update={"assumed_roles": ("fixture-reviewer",)}), value
    )
    assert role.state == "within_limit" and role.selected_assumed_role == "fixture-reviewer"
    assert Fraction(
        role.remaining_gpu_seconds.numerator, role.remaining_gpu_seconds.denominator
    ) == Fraction(2) - Fraction(3_000_001, 3_000_000)
    zero = synthetic_evidence(total_usage_microseconds=0, models=())
    compiled, query, value = nova_fixture(zero)
    assert historical_quota_assessment(compiled, query, value).used_gpu_seconds.numerator == 0
    # One model aggregated across GPUs can exceed one GPU-year without rounding or overflow.
    start = value.window.start_at
    microseconds = 128 * 7 * 86400 * 1_000_000
    many = synthetic_evidence(
        selection=value.selection,
        gpu_count=128,
        window=value.window.model_copy(update={"end_at": start + timedelta(days=7)}),
        captured_at=start + timedelta(days=8),
        created_at=start + timedelta(days=8),
        total_usage_microseconds=microseconds,
        models=(HistoricalModelUsage(model="fixture GPU", observed_microseconds=microseconds),),
    )
    compiled, query, value = nova_fixture(many)
    query = query.model_copy(update={"period_at": start})
    assert (
        historical_quota_assessment(compiled, query, value).used_gpu_seconds.numerator
        == 128 * 7 * 86400 // 3
    )


def test_nova_keeps_incomplete_method_and_period_mismatches_unknown():
    cases = [
        (
            {
                "coverage": "unknown",
                "source_prefix_complete": False,
                "models": None,
                "total_usage_microseconds": None,
            },
            {},
            "USAGE_COVERAGE_INCOMPLETE",
        ),
        ({}, {"expected_timing_fingerprint": "f" * 64}, "USAGE_METHOD_MISMATCH"),
        ({}, {"period_at": datetime(2026, 11, 23, tzinfo=UTC)}, "USAGE_WINDOW_MISMATCH"),
    ]
    for evidence_changes, query_changes, reason in cases:
        compiled, query, value = nova_fixture(synthetic_evidence(**evidence_changes))
        result = historical_quota_assessment(
            compiled, query.model_copy(update=query_changes), value
        )
        assert result.state == "not_evaluated" and result.reason == reason
        assert result.used_gpu_seconds is result.remaining_gpu_seconds is None
    compiled = compile_policy(parse_policy_source(json.dumps(policy()).encode()))
    compiled, query, value = nova_fixture(compiled=compiled)
    assert (
        historical_quota_assessment(compiled, query, value).reason
        == "GLOBAL_QUOTA_REQUIRES_ALL_NODE_EVIDENCE"
    )
    raw = quota_source().source.model_dump(mode="json")
    raw["spec"]["quota"]["window"] = {"kind": "rolling_7d"}
    compiled = compile_policy(parse_policy_source(json.dumps(raw).encode()))
    compiled, query, value = nova_fixture(compiled=compiled)
    # A future rolling anchor cannot turn an incomplete rolling week into complete usage.
    query = query.model_copy(update={"period_at": value.window.start_at + timedelta(days=7)})
    assert historical_quota_assessment(compiled, query, value).reason == "USAGE_WINDOW_MISMATCH"
    raw = quota_source().source.model_dump(mode="json")
    raw["spec"]["quota"]["node_limits"][0]["limit"] = {"kind": "unlimited"}
    compiled = compile_policy(parse_policy_source(json.dumps(raw).encode()))
    compiled, query, value = nova_fixture(compiled=compiled)
    unlimited = historical_quota_assessment(compiled, query, value)
    assert unlimited.state == "unlimited" and unlimited.remaining_gpu_seconds is None


def test_contract_rejects_forged_totals_and_wrong_source_bindings():
    value = synthetic_evidence()
    with pytest.raises(ValidationError):
        synthetic_evidence(coverage="unknown")
    with pytest.raises(ValidationError):
        synthetic_evidence(total_usage_microseconds=0)
    with pytest.raises(ValidationError):
        HistoricalQuotaEvidence.model_validate(
            {**value.model_dump(), "current_quota_authorization": True}
        )
    compiled, query, value = nova_fixture(value)
    with pytest.raises(DomainError) as error:
        historical_quota_assessment(
            compiled, query.model_copy(update={"expected_policy_fingerprint": "f" * 64}), value
        )
    assert error.value.code == "CONFLICT"


def test_real_domain_composition_checks_current_policy_and_usage_access(
    evidence, catalog_state, monkeypatch
):
    state = evidence
    _, catalog, sessions, ids, _ = catalog_state
    assert (
        confirmed(catalog_state, source=quota_source().source.model_dump(mode="json")).state
        == "succeeded"
    )
    grant = str(uuid4())
    with sessions.begin() as session:
        session.add(
            RoleBinding(
                id=grant,
                user_id=str(state.actor.actor_id),
                role_id=ids["role"],
                scope_type="global",
                scope_id="*",
            )
        )
    source = catalog.exact(state.actor, reference())
    value = state.service.read(state.actor, selection(state.saved, state.actor.actor_id))
    query = HistoricalQuotaQuery(
        policy=reference(),
        expected_policy_fingerprint=source.fingerprint,
        usage=value.selection,
        expected_configuration_fingerprint=value.configuration_fingerprint,
        expected_timing_fingerprint=value.timing_fingerprint,
        period_at=value.window.end_at,
    )
    service = HistoricalQuotaAssessmentService(catalog, state.service)
    # The real source fixture covers 30 seconds at noon, not from the Monday boundary.
    assert service.assess(state.actor, query).reason == "USAGE_WINDOW_MISMATCH"
    if sessions.kw["bind"].dialect.name == "postgresql":
        with sessions() as blocker:
            blocker.execute(text("LOCK TABLE policy_versions IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(DomainError) as error:
                service.assess(state.actor, query)
            assert error.value.code == "RETRYABLE_DATABASE_ERROR"
            blocker.rollback()
    original = state.service.require_access

    def revoke_policy(actor, selection):
        original(actor, selection)
        with sessions.begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.id == grant))

    monkeypatch.setattr(state.service, "require_access", revoke_policy)
    with pytest.raises(DomainError) as error:
        service.assess(state.actor, query)
    assert error.value.code == "PERMISSION_DENIED"
