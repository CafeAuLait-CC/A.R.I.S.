import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from aris.apps.api.main import create_app
from aris.apps.ops.usage_ui import LocalUsageConfig
from aris.core.access.models import ExternalIdentity, RolePermission
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.activity_service import (
    ActivityPreviewAccess,
    ActivityPreviewConfig,
    activity_view,
)
from aris.interfaces.usage.service import UsageHistorySurface
from tests.test_activity_preview import RULE, fact, grouped
from tests.test_activity_preview_service import preview_source
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_usage_derivations import derivations
from tests.test_usage_history import TOKEN, surface_for
from tests.test_usage_ledger import counts, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings", "preview_source"]
HEADERS = {"Authorization": "Bearer " + TOKEN}
QUERY = {"node_id": "node-a", "max_messages": 5}


def interface(state, settings, *, enabled=True):
    service, _, _, readings = state
    base, actor, ids = surface_for(readings, settings)
    config = ActivityPreviewConfig(
        sources=[{"agent_id": "agent-a", "node_id": "node-a"}], rule=RULE
    )
    surface = UsageHistorySurface(
        base.history,
        base.authenticator,
        base.node_ids,
        activity=ActivityPreviewAccess(service, config) if enabled else None,
    )
    return (
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface),
        surface,
        actor,
        ids,
    )


def test_optional_archive_route_is_absent_by_default_and_reads_real_fixed_sources_without_writes(
    preview_source, settings
):
    app, surface, _, _ = interface(preview_source, settings, enabled=False)
    with TestClient(app) as client:
        assert (
            client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY).status_code
            == 404
        )
        assert (
            client.post("/internal/usage/session", headers=HEADERS).json()["activity_preview_nodes"]
            == []
        )
    app, surface, _, _ = interface(preview_source, settings)
    before = counts(preview_source[3][0])
    with TestClient(app) as client:
        assert client.post("/internal/usage/session", headers=HEADERS).json()[
            "activity_preview_nodes"
        ] == ["node-a"]
        result = client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY)
        assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
        value = result.json()
        assert value["source_packets"] == 5 and value["heartbeat_packets"] == 2
        assert value["highest_committed_seq"] == "7" and value["requested_after_seq"] == "2"
        assert value["groups"][0]["first_source"]["seq"] == "3"
        assert value["groups"][0]["state"] == "reported_busy" and value["range_complete"]
        assert not value["continuous_usage_established"] and not value["retention_enabled"]
        assert "private-fixture-name" not in result.text and "username" not in result.text
        gap = client.post(
            "/internal/usage/activity/query",
            headers=HEADERS,
            json={"node_id": "node-a", "max_messages": 128},
        ).json()
        assert (
            gap["source_packets"] == 0 and gap["missing_seq"] == "1" and not gap["range_complete"]
        )
        assert client.get("/internal/usage/ui/assets/usage-activity.js").status_code == 200
    assert counts(preview_source[3][0]) == before


def test_archive_http_binding_input_and_current_all_permission_are_required_before_reading(
    preview_source, settings, monkeypatch
):
    app, surface, _, ids = interface(preview_source, settings)
    with TestClient(app) as client:
        assert client.post("/internal/usage/activity/query", json=QUERY).status_code == 401
        for change in (
            {"agent_id": "other"},
            {"actor_id": "other"},
            {"rule": {}},
            {"max_messages": True},
            {"max_messages": 1025},
        ):
            assert (
                client.post(
                    "/internal/usage/activity/query", headers=HEADERS, json={**QUERY, **change}
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/internal/usage/activity/query?node_id=node-a", headers=HEADERS, json=QUERY
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/usage/activity/query",
                headers=HEADERS,
                json={**QUERY, "node_id": "node-b"},
            ).status_code
            == 403
        )
        with surface.history.sessions.begin() as session:
            session.execute(
                delete(RolePermission).where(
                    RolePermission.role_id == ids["role"],
                    RolePermission.permission_id == ids["all"],
                )
            )

        def forbidden(*args, **kwargs):
            raise AssertionError("own-only caller read archive bodies")

        monkeypatch.setattr(surface.activity.reader, "recent", forbidden)
        assert (
            client.post("/internal/usage/session", headers=HEADERS).json()["activity_preview_nodes"]
            == []
        )
        assert (
            client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY).status_code
            == 403
        )


def test_archive_http_rechecks_external_identity_after_domain_read(
    preview_source, settings, monkeypatch
):
    app, surface, actor, _ = interface(preview_source, settings)
    original = surface.activity.reader.recent

    def revoke(*args, **kwargs):
        value = original(*args, **kwargs)
        with surface.history.sessions.begin() as session:
            session.execute(
                delete(ExternalIdentity).where(ExternalIdentity.user_id == str(actor.actor_id))
            )
        return value

    monkeypatch.setattr(surface.activity.reader, "recent", revoke)
    with TestClient(app) as client:
        result = client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY)
        assert result.status_code == 403 and "source_packets" not in result.text


def test_archive_projection_keeps_long_sequences_exact_and_does_not_disclose_raw_linux_owners():
    first = (1 << 53) + 1
    view = activity_view(grouped([fact(first, 0), fact(first + 1, 15)], max_messages=2))
    raw = view.model_dump(mode="json")
    assert raw["highest_committed_seq"] == str(first + 1)
    assert raw["requested_after_seq"] == str(first - 1)
    assert raw["groups"][0]["first_source"]["seq"] == str(first)
    assert raw["groups"][0]["last_source"]["seq"] == str(first + 1)
    assert "private-fixture-name" not in view.model_dump_json()
    assert "uid" not in raw["groups"][0]


def test_private_activity_configuration_requires_explicit_unique_node_sources_and_diagnostic_rule():
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from tests.test_usage_ui_runtime import private_config

    with TemporaryDirectory() as directory:
        path, _ = private_config(Path(directory))
        value = json.loads(path.read_text())
    assert LocalUsageConfig.model_validate(value).activity_preview is None
    source = {"agent_id": "agent-a", "node_id": "node-a"}
    config = {"sources": [source], "rule": RULE.model_dump(mode="json")}
    assert (
        LocalUsageConfig.model_validate({**value, "activity_preview": config}).activity_preview.rule
        == RULE
    )
    for change in (
        {"sources": [source, source]},
        {"rule": {}},
        {"sources": [{"agent_id": "agent-b", "node_id": "outside"}]},
    ):
        with pytest.raises(ValidationError):
            LocalUsageConfig.model_validate({**value, "activity_preview": {**config, **change}})
