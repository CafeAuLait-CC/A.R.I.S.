import json
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, event, text

from aris.apps.api.main import create_app
from aris.apps.ops import policy_ui
from aris.core.access.models import RoleBinding
from aris.core.access.schemas import ExternalSubject
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.policy.history import (
    ExactSecondsView,
    HistoricalPolicyNode,
    PolicyHistoricalUsage,
    PolicyHistoryWindowsInput,
)
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_simulation import ExactGpuSeconds
from aris.modules.nova.quota_history_service import HistoricalQuotaAssessmentService
from tests.test_historical_quota_evidence import (
    adjustments,
    admin_state,
    catalog_state,
    closed,
    command_state,
    coverage,
    derivations,
    evidence,
    history_state,
    ledger,
    quota_source,
    windows,
)
from tests.test_policy_catalog import confirmed, reference
from tests.test_window_snapshots import CONTEXT, counts, incoming_window

__all__ = [
    "admin_state",
    "history_state",
    "derivations",
    "ledger",
    "closed",
    "coverage",
    "windows",
    "evidence",
    "command_state",
    "catalog_state",
    "adjustments",
]
TOKEN = "fixture-history-credential-123456789"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def history_surface(evidence, catalog_state, settings):
    state = evidence
    _, catalog, sessions, ids, _ = catalog_state
    assert (
        confirmed(catalog_state, source=quota_source().source.model_dump(mode="json")).state
        == "succeeded"
    )
    with sessions.begin() as session:
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(state.actor.actor_id),
                role_id=ids["role"],
                scope_type="global",
                scope_id="*",
            )
        )
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(
                subject=ExternalSubject(
                    provider="slack", namespace="TFIXTURE", external_id="UFIRST"
                ),
                token=SecretStr(TOKEN),
            )
        ],
        reserved_tokens=[settings.read_token],
    )
    config = state.saved.window.configuration
    node = HistoricalPolicyNode(
        node_id="node-a",
        expected_configuration_fingerprint=config.fingerprint,
        expected_timing_fingerprint=config.recording.timing_fingerprint,
    )
    historical = PolicyHistoricalUsage(
        HistoricalQuotaAssessmentService(catalog, state.service),
        state.service,
        (node, node.model_copy(update={"node_id": "node-b"})),
    )
    surface = PolicyReadSurface(catalog, auth, ("fixture-only",), historical_usage=historical)
    return surface, state


def directory(state, **changes):
    return {
        "node_id": "node-a",
        "start_at": (state.saved.window.query.start_at - timedelta(seconds=1)).isoformat(),
        "end_at": (state.saved.window.query.end_at + timedelta(seconds=1)).isoformat(),
        "limit": 1,
        **changes,
    }


def assessment(surface, state, **changes):
    source = surface.catalog.exact(state.actor, reference())
    return {
        "reference": reference().model_dump(),
        "expected_fingerprint": source.fingerprint,
        "node_id": "node-a",
        "snapshot_id": str(state.saved.request.id),
        "expected_snapshot_fingerprint": state.saved.fingerprint,
        "period_at": state.saved.window.query.start_at.isoformat(),
        **changes,
    }


def test_history_directory_pages_own_or_node_windows_without_leaking_other_owner_totals(
    history_surface, settings
):
    surface, state = history_surface
    own = state.windows.service.capture(
        CONTEXT, incoming_window(state.windows, owner=state.actor.actor_id)
    ).snapshot
    other = state.windows.service.capture(
        CONTEXT, incoming_window(state.windows, owner=state.other)
    ).snapshot
    before = counts(state.windows.service)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        info = client.post("/internal/policy/session", headers=HEADERS).json()
        assert info["historical_usage_nodes"] == ["node-a"]
        first = client.post(
            "/internal/policy/history/windows", headers=HEADERS, json=directory(state)
        ).json()
        second = client.post(
            "/internal/policy/history/windows",
            headers=HEADERS,
            json=directory(state, before=first["next_cursor"]),
        ).json()
        items = first["items"] + second["items"]
        assert {item["selection"]["snapshot_id"] for item in items} == {
            str(own.request.id),
            str(state.saved.request.id),
        }
        assert second["next_cursor"] is None
        assert str(other.request.id) not in json.dumps(items) and str(
            state.other
        ) not in json.dumps(items)
        assert all(set(item) == {"selection", "start_at", "end_at", "created_at"} for item in items)
        checked = client.post(
            "/internal/policy/history/assess", headers=HEADERS, json=assessment(surface, state)
        )
        assert checked.status_code == 200 and "no-store" in checked.headers["cache-control"]
        result = checked.json()
        assert result["historical_only"] and not result["booking_authorization"]
        assert result["reason"] == "USAGE_WINDOW_MISMATCH" and result["used_gpu_seconds"] is None
        assert result["selection"]["owner_id"] == str(state.actor.actor_id)
        for extra in ({"owner_id": state.other}, {"expected_timing_fingerprint": "f" * 64}):
            assert (
                client.post(
                    "/internal/policy/history/assess",
                    headers=HEADERS,
                    json=assessment(surface, state, **extra),
                ).status_code
                == 422
            )
    assert counts(state.windows.service) == before


def test_history_scope_current_roles_and_identity_cannot_be_overridden(
    history_surface, settings, monkeypatch
):
    surface, state = history_surface
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        for node in ("node-b", "outside"):
            assert (
                client.post(
                    "/internal/policy/history/windows",
                    headers=HEADERS,
                    json=directory(state, node_id=node),
                ).status_code
                == 403
            )
        assert (
            client.post(
                "/internal/policy/history/windows",
                headers={"Authorization": "Bearer " + settings.read_token.get_secret_value()},
                json=directory(state),
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/internal/policy/history/assess",
                headers=HEADERS,
                json=assessment(surface, state, expected_snapshot_fingerprint="f" * 64),
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/internal/policy/history/assess",
                headers=HEADERS,
                json=assessment(surface, state, reference={"name": "outside", "version": 1}),
            ).status_code
            == 403
        )
        with state.service.sessions.begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.id == state.ids["binding"]))
        assert (
            client.post(
                "/internal/policy/history/windows", headers=HEADERS, json=directory(state)
            ).status_code
            == 403
        )
        assert (
            client.post("/internal/policy/session", headers=HEADERS).json()[
                "historical_usage_nodes"
            ]
            == []
        )
    # The outer policy surface also checks external identity after all nested reads.
    original = surface.catalog.access.resolve_external_in_transaction
    calls = 0

    def lost(session, subject):
        nonlocal calls
        calls += 1
        return original(session, subject) if calls == 1 else None

    monkeypatch.setattr(surface.catalog.access, "resolve_external_in_transaction", lost)
    with pytest.raises(DomainError) as error:
        surface.session("Bearer " + TOKEN, "fixture-current-identity")
    assert error.value.code == "PERMISSION_DENIED"


def test_postgresql_history_directory_reads_only_metadata_and_obeys_real_lock_budget(
    history_surface,
):
    surface, state = history_surface
    engine = state.service.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL directory READ ONLY and lock budget")
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", observe)
    try:
        result = surface.history_windows(
            "Bearer " + TOKEN,
            "fixture-directory",
            PolicyHistoryWindowsInput.model_validate(directory(state)),
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(result.items) == 1
    assert any("SET TRANSACTION READ ONLY" in sql for sql in statements)
    assert not any("canonical_snapshot" in sql or "agent_messages" in sql for sql in statements)
    with state.service.sessions() as blocker:
        blocker.execute(text("LOCK TABLE usage_window_snapshots IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            surface.history_windows(
                "Bearer " + TOKEN,
                "fixture-directory",
                PolicyHistoryWindowsInput.model_validate(directory(state)),
            )
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()


def test_history_wire_precision_and_private_configuration_need_no_database(
    tmp_path, monkeypatch, capsys
):
    integer = 2**90 + 1
    value = ExactSecondsView.of(ExactGpuSeconds(numerator=integer, denominator=3))
    assert json.loads(value.model_dump_json()) == {"numerator": str(integer), "denominator": "3"}
    assert ExactSecondsView.of(None) is None

    def forbidden(*args, **kwargs):
        raise AssertionError("configuration validation accessed database")

    monkeypatch.setattr(policy_ui, "build_engine", forbidden)
    node = {
        "node_id": "fixture-node",
        "expected_configuration_fingerprint": "a" * 64,
        "expected_timing_fingerprint": "b" * 64,
    }
    config = {
        "environment": "test",
        "database_url": "sqlite+pysqlite:///fixture-never-opened.db",
        "read_token": "fixture-unused-read-token-123456789",
        "user_tokens": [
            {
                "subject": {"provider": "slack", "namespace": "TFIXTURE", "external_id": "UFIRST"},
                "token": TOKEN,
            }
        ],
        "policy_names": ["fixture-only"],
        "historical_usage": [node],
    }
    path = tmp_path / "policy-private.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    assert policy_ui.main(["--config", str(path), "--validate"]) == 0
    assert json.loads(capsys.readouterr().out)["historical_usage_nodes"] == ["fixture-node"]
    config["historical_usage"].append(node)
    path.write_text(json.dumps(config))
    assert policy_ui.main(["--config", str(path), "--validate"]) == 2
