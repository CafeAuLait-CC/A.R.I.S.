import copy

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select, text

from aris.core.access.models import RoleBinding
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.contracts import DomainError
from aris.modules.nova import policy_catalog_service as service_module
from aris.modules.nova.policy_catalog_schemas import PolicySourceComparisonQuery
from aris.modules.nova.policy_source import compile_policy
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, confirmed, reference, source_counts
from tests.test_policy_simulation import scenario
from tests.test_policy_source import policy

__all__ = ["command_state", "catalog_state"]


def setup_pair(state, *, changed_limit=True):
    _, catalog, _, ids, _ = state
    confirmed(state, key="stored-before")
    old = catalog.exact(actor(ids), reference())
    raw = copy.deepcopy(policy())
    raw["metadata"]["version"] = 2
    if changed_limit:
        raw["spec"]["quota"]["default_limit"]["gpu_seconds"] = 3600
    confirmed(state, key="stored-after", source=raw)
    new = catalog.exact(actor(ids), reference(raw))
    query = PolicySourceComparisonQuery(
        before=reference(),
        after=reference(raw),
        expected_before_fingerprint=old.fingerprint,
        expected_after_fingerprint=new.fingerprint,
    )
    return old, new, query


def test_stored_comparison_reads_validated_sources_by_reference_without_creating_review_state(
    catalog_state,
):
    _, catalog, sessions, ids, _ = catalog_state
    old, new, query = setup_pair(catalog_state)
    before = source_counts(sessions)
    with sessions() as session:
        history = [
            session.scalar(select(func.count()).select_from(model))
            for model in (AuditEntry, CommandRecord)
        ]
    result = catalog.compare_sources(actor(ids), query)
    assert (
        result.comparison_only
        and result.before_source_id == old.id
        and result.after_source_id == new.id
    )
    assert result.difference.before_fingerprint == old.fingerprint
    assert result.difference.after_fingerprint == new.fingerprint
    assert {change.path for change in result.difference.changes} == {
        "/metadata/version",
        "/spec/quota/default_limit/gpu_seconds",
    }
    assert result.cases == () and source_counts(sessions) == before
    with sessions() as session:
        assert history == [
            session.scalar(select(func.count()).select_from(model))
            for model in (AuditEntry, CommandRecord)
        ]
    assert (
        catalog.exact(actor(ids), query.before) == old
        and catalog.exact(actor(ids), query.after) == new
    )


def test_explicit_synthetic_usage_scenarios_remain_simulations_when_sources_are_stored(
    catalog_state,
):
    _, catalog, _, ids, _ = catalog_state
    old, _, query = setup_pair(catalog_state)
    source = compile_policy(old.source)
    cases = (
        scenario(source, name="synthetic-one-hour", used_microseconds=3600 * 1000000),
        scenario(source, name="usage-unknown", include_usage=False),
    )
    query = query.model_copy(update={"scenarios": cases})
    result = catalog.compare_sources(actor(ids), query)
    assert result.cases[0].decision_changed
    assert (
        result.cases[0].before.decision == "WOULD_ALLOW"
        and result.cases[0].after.decision == "WOULD_DENY"
    )
    assert result.cases[1].before.decision == result.cases[1].after.decision == "NOT_EVALUATED"
    assert all(case.before.simulation_only and case.after.simulation_only for case in result.cases)


def test_metadata_only_stored_version_change_is_not_a_changed_simulation_decision(catalog_state):
    _, catalog, _, ids, _ = catalog_state
    old, _, query = setup_pair(catalog_state, changed_limit=False)
    case = scenario(compile_policy(old.source))
    result = catalog.compare_sources(actor(ids), query.model_copy(update={"scenarios": (case,)}))
    assert not result.cases[0].decision_changed and not result.cases[0].changed_rules


@pytest.mark.parametrize("side", ["before", "after"])
def test_client_expected_fingerprints_cannot_be_substituted_for_stored_source_identity(
    catalog_state, side
):
    _, catalog, _, ids, _ = catalog_state
    _, _, query = setup_pair(catalog_state)
    changed = query.model_copy(update={"expected_" + side + "_fingerprint": "f" * 64})
    with pytest.raises(DomainError) as error:
        catalog.compare_sources(actor(ids), changed)
    assert error.value.code == "CONFLICT"


def test_missing_version_returns_not_found_without_manufacturing_a_draft(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    _, _, query = setup_pair(catalog_state)
    changed = query.model_copy(update={"after": query.after.model_copy(update={"version": 999})})
    with pytest.raises(DomainError) as error:
        catalog.compare_sources(actor(ids), changed)
    assert error.value.code == "NOT_FOUND" and source_counts(sessions) == (2, 2, 2)


def test_unprivileged_comparison_never_reads_the_stored_policy_body(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    _, _, query = setup_pair(catalog_state)
    with sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
    statements = []
    engine = sessions.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(DomainError) as error:
            catalog.compare_sources(actor(ids), query)
        assert error.value.code == "PERMISSION_DENIED"
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("policy_versions" in sql for sql in statements)


@pytest.mark.parametrize("with_cases", [False, True])
def test_permission_revoked_during_cpu_comparison_prevents_returning_results(
    catalog_state, monkeypatch, with_cases
):
    _, catalog, sessions, ids, _ = catalog_state
    old, _, query = setup_pair(catalog_state)
    name = "compare_policy_scenarios" if with_cases else "policy_difference"
    original = getattr(service_module, name)
    if with_cases:
        query = query.model_copy(update={"scenarios": (scenario(compile_policy(old.source)),)})

    def revoked(*args):
        result = original(*args)
        with sessions.begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        return result

    monkeypatch.setattr(service_module, name, revoked)
    with pytest.raises(DomainError) as error:
        catalog.compare_sources(actor(ids), query)
    assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("case", ["other_name", "duplicate_case", "oversized", "client_body"])
def test_stored_review_queries_reject_cross_policy_or_unbounded_client_input(catalog_state, case):
    _, _, _, _, _ = catalog_state
    old, _, query = setup_pair(catalog_state)
    raw = query.model_dump(mode="json")
    if case == "other_name":
        raw["after"]["name"] = "another-policy"
    elif case == "client_body":
        raw["source"] = policy()
    else:
        sample = scenario(compile_policy(old.source)).model_dump(mode="json")
        raw["scenarios"] = [sample] * (2 if case == "duplicate_case" else 101)
    with pytest.raises(ValidationError):
        PolicySourceComparisonQuery.model_validate(raw)


def test_postgresql_stored_comparison_uses_a_read_only_transaction(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    engine = sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL stored source comparison READ ONLY contract")
    old, new, query = setup_pair(catalog_state)

    def read_only(connection):
        connection.execute(text("SET TRANSACTION READ ONLY"))

    event.listen(engine, "begin", read_only)
    try:
        result = catalog.compare_sources(actor(ids), query)
    finally:
        event.remove(engine, "begin", read_only)
    assert result.before_source_id == old.id and result.after_source_id == new.id
