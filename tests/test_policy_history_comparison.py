import json
from fractions import Fraction

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, event, text

from aris.apps.api.main import create_app
from aris.core.access.models import RoleBinding
from aris.core.contracts import DomainError
from aris.interfaces.policy.history_comparison import (
    PolicyHistoryComparisonInput,
    PolicyHistoryComparisonView,
)
from aris.interfaces.policy.history_comparison_report import historical_comparison_report
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_source import compile_policy, parse_policy_source
from aris.modules.nova.quota_comparison_schemas import HistoricalQuotaComparisonQuery
from aris.modules.nova.quota_comparison_service import compare_historical_quota
from tests.test_historical_quota_evidence import nova_fixture, synthetic_evidence
from tests.test_policy_catalog import confirmed, reference
from tests.test_policy_history_interface import (
    HEADERS,
    TOKEN,
    adjustments,
    admin_state,
    assessment,
    catalog_state,
    closed,
    command_state,
    counts,
    coverage,
    derivations,
    evidence,
    history_state,
    history_surface,
    ledger,
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
    "windows",
]
IMPLEMENTATION = "fixture-history-comparison"


def target_source(before, *, window=None):
    raw = before.source.model_dump(mode="json")
    raw["metadata"]["version"] += 1
    raw["spec"]["quota"]["node_limits"][0]["limit"]["gpu_seconds"] = 10
    raw["spec"]["gpu_time_weights"]["model_weights"][0]["weight"]["numerator"] = 2
    if window is not None:
        raw["spec"]["quota"]["window"] = window
    return compile_policy(parse_policy_source(json.dumps(raw).encode()))


def comparison_query(query, target):
    return HistoricalQuotaComparisonQuery(
        before=query,
        target=reference(target.source.model_dump(mode="json")),
        expected_target_fingerprint=target.fingerprint,
    )


@pytest.fixture
def comparison_surface(history_surface, catalog_state):
    original, state = history_surface
    before = original.catalog.exact(state.actor, reference())
    target = target_source(compile_policy(before.source))
    assert (
        confirmed(
            catalog_state,
            key="history-comparison-target",
            source=target.source.model_dump(mode="json"),
        ).state
        == "succeeded"
    )
    surface = PolicyReadSurface(
        original.catalog,
        original.authenticator,
        original.policy_names,
        historical_usage=original.historical_usage,
        historical_review_implementation_ref=IMPLEMENTATION,
        historical_comparison_enabled=True,
    )
    return surface, state, target


def parameters(surface, state, target, **changes):
    return {
        **assessment(surface, state),
        "expected_implementation_ref": IMPLEMENTATION,
        "target": reference(target.source.model_dump(mode="json")).model_dump(mode="json"),
        "expected_target_fingerprint": target.fingerprint,
        **changes,
    }


def test_two_versions_keep_one_history_and_exact_weights_without_promoting_unknown():
    before, query, value = nova_fixture()
    after = target_source(before)
    result = compare_historical_quota(before, after, comparison_query(query, after), value)
    assert result.evidence == value and result.before.state == "exhausted"
    assert result.after.state == "within_limit" and result.quota_state_changed is True
    assert Fraction(**result.before.used_gpu_seconds.model_dump()) == Fraction(3_000_001, 3_000_000)
    assert Fraction(**result.after.used_gpu_seconds.model_dump()) == Fraction(6_000_002, 3_000_000)
    assert result.before.evidence_fingerprint == result.after.evidence_fingerprint
    assert not result.current_dependencies_assessed and not result.booking_authorization
    assert not result.publication_performed and not result.role_assignments_verified
    assert {item.path for item in result.difference.changes} == {
        "/spec/quota/node_limits/node-a/gpu_seconds",
        "/spec/gpu_time_weights/model_weights/fixture GPU/numerator",
        "/metadata/version",
    }
    rolling = target_source(before, window={"kind": "rolling_7d"})
    unknown = compare_historical_quota(before, rolling, comparison_query(query, rolling), value)
    assert unknown.before.state == "exhausted" and unknown.after.reason == "USAGE_WINDOW_MISMATCH"
    assert unknown.after.used_gpu_seconds is None and unknown.quota_state_changed is None
    missing = query.model_copy(update={"expected_configuration_fingerprint": "f" * 64})
    unknown = compare_historical_quota(before, after, comparison_query(missing, after), value)
    assert unknown.before.reason == unknown.after.reason == "USAGE_METHOD_MISMATCH"
    assert unknown.quota_state_changed is None
    with pytest.raises(ValidationError):
        comparison_query(query, before)


def test_comparison_wire_and_report_preserve_large_originals_and_escape_model_text():
    original = synthetic_evidence(source_ceiling=2**90 + 1)
    before, query, value = nova_fixture(original)
    after = target_source(before)
    result = compare_historical_quota(before, after, comparison_query(query, after), value)
    view = PolicyHistoryComparisonView.of(result, IMPLEMENTATION)
    assert view.evidence.source_ceiling == str(2**90 + 1)
    assert view.comparison == result
    changed = view.model_dump(mode="json")
    changed["before"]["used_gpu_seconds"]["numerator"] = "1"
    with pytest.raises(ValidationError):
        PolicyHistoryComparisonView.model_validate(changed)
    html = historical_comparison_report(view)
    assert str(2**90 + 1) in html and "<script" not in html
    # Labels are original source data; they must remain text even inside the raw proof.
    malicious = synthetic_evidence(
        models=(original.models[0].model_copy(update={"model": "<img src=x onerror=alert(1)>"}),)
    )
    before, query, value = nova_fixture(malicious)
    after = target_source(before)
    report = historical_comparison_report(
        PolicyHistoryComparisonView.of(
            compare_historical_quota(before, after, comparison_query(query, after), value),
            IMPLEMENTATION,
        )
    )
    assert "&lt;img" in report and "<img" not in report and "不批准" in report


def test_comparison_service_reads_one_public_original_and_does_not_write(
    comparison_surface, monkeypatch
):
    surface, state, target = comparison_surface
    original = state.service.read
    calls = []

    def tracked(actor, selection):
        result = original(actor, selection)
        calls.append(result)
        return result

    monkeypatch.setattr(state.service, "read", tracked)
    before = counts(state.windows.service)
    view = surface.history_compare(
        "Bearer " + TOKEN,
        "same-original",
        PolicyHistoryComparisonInput(**parameters(surface, state, target)),
    )
    assert len(calls) == 1 and calls[0] == view.comparison.evidence
    assert view.before.selection == view.after.selection
    assert view.before.reason == view.after.reason == "USAGE_WINDOW_MISMATCH"
    assert view.quota_state_changed is None and str(state.other) not in view.model_dump_json()
    assert counts(state.windows.service) == before


@pytest.mark.parametrize("removed", ["usage", "draft"])
def test_comparison_rechecks_both_domain_permissions_after_the_shared_read(
    comparison_surface, monkeypatch, removed
):
    surface, state, target = comparison_surface
    original = state.service.read

    def revoked(actor, selection):
        value = original(actor, selection)
        with surface.catalog.sessions.begin() as db:
            condition = (
                RoleBinding.id == state.ids["binding"]
                if removed == "usage"
                else (RoleBinding.user_id == str(actor.actor_id))
                & (RoleBinding.scope_type == "global")
            )
            db.execute(delete(RoleBinding).where(condition))
        return value

    monkeypatch.setattr(state.service, "read", revoked)
    with pytest.raises(DomainError) as error:
        surface.history_compare(
            "Bearer " + TOKEN,
            "current-access",
            PolicyHistoryComparisonInput(**parameters(surface, state, target)),
        )
    assert error.value.code == "PERMISSION_DENIED"


def test_comparison_http_is_opt_in_owner_bound_and_exports_only_the_same_result(
    comparison_surface, history_surface, settings
):
    surface, state, target = comparison_surface
    value = parameters(surface, state, target)
    base, _ = history_surface
    with TestClient(create_app(settings, SlackSettings(_env_file=None), policy_sources=base)) as c:
        assert (
            c.post("/internal/policy/history/compare", headers=HEADERS, json=value).status_code
            == 404
        )
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as c:
        assert c.post("/internal/policy/session", headers=HEADERS).json()[
            "historical_comparison_available"
        ]
        result = c.post("/internal/policy/history/compare", headers=HEADERS, json=value)
        assert result.status_code == 200 and result.headers["Cache-Control"] == "no-store"
        export = {**value, "expected_context_fingerprint": result.json()["context_fingerprint"]}
        report = c.post("/internal/policy/history/compare/report", headers=HEADERS, json=export)
        assert report.status_code == 200 and "<script" not in report.text
        assert state.saved.fingerprint in report.text and target.fingerprint in report.text
        assert c.get("/internal/policy/ui/assets/policy-history-compare.js").status_code == 200
        for change, status in (
            ({"expected_context_fingerprint": "f" * 64}, 409),
            ({"expected_target_fingerprint": "f" * 64}, 409),
            ({"owner_id": str(state.other)}, 422),
            ({"expected_implementation_ref": "changed"}, 409),
            ({"target": {"name": "outside", "version": 2}}, 422),
        ):
            assert (
                c.post(
                    "/internal/policy/history/compare/report",
                    headers=HEADERS,
                    json={**export, **change},
                ).status_code
                == status
            )
        with surface.catalog.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.id == state.ids["binding"]))
        assert (
            c.post(
                "/internal/policy/history/compare/report", headers=HEADERS, json=export
            ).status_code
            == 403
        )


def test_postgresql_comparison_is_read_only_and_recovers_after_a_source_lock(comparison_surface):
    surface, state, target = comparison_surface
    engine = surface.catalog.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only shared evidence and real lock budget")
    query = PolicyHistoryComparisonInput(**parameters(surface, state, target))
    before = counts(state.windows.service)
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = surface.history_compare("Bearer " + TOKEN, "pg-shared-history", query)
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert result.historical_only
    assert any("SET TRANSACTION" in sql and "READ ONLY" in sql for sql in statements)
    assert not any(
        sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE")) for sql in statements
    )
    with surface.catalog.sessions() as blocker:
        blocker.execute(text("LOCK TABLE policy_versions IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            surface.history_compare("Bearer " + TOKEN, "locked-source", query)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert surface.history_compare("Bearer " + TOKEN, "source-recovered", query) == result
    assert counts(state.windows.service) == before
