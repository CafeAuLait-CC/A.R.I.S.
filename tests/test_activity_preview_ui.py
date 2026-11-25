from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, event

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
from aris.modules.lyra.reporting_gap_history_service import ReportingGapHistoryService
from aris.modules.vega.models import AgentArchiveState, AgentMessage, AgentMessageReceipt
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from tests.test_activity_preview import RULE, fact, grouped
from tests.test_activity_preview_service import preview_source
from tests.test_administration import admin_state
from tests.test_heartbeat import heartbeat
from tests.test_historical_usage_facts import beat
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_usage_derivations import derivations
from tests.test_usage_history import TOKEN, surface_for
from tests.test_usage_ledger import counts, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings", "preview_source"]
HEADERS = {"Authorization": "Bearer " + TOKEN}
QUERY = {"node_id": "node-a", "max_messages": 5}


def test_saved_reporting_history_pages_without_raw_sources_and_rechecks_authority(
    preview_source, settings, monkeypatch
):
    app, surface, _, ids = interface(preview_source, settings)
    preview, _, actor, readings = preview_source
    caller = readings[0][4]
    start = readings[0][2][2][5]["now"] - timedelta(minutes=20)
    for seq in range(8, 12):
        observed = start + timedelta(seconds=(seq - 8) * 200)
        assert (
            beat(
                caller,
                heartbeat(seq=seq, observed_at=observed.isoformat(), sent_at=observed.isoformat()),
            ).status_code
            == 200
        )
    source = UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")
    history = ReportingGapHistoryService(preview)
    assert history.capture(actor, source, max_messages=4, through_seq=11).saved == 3
    with surface.history.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq >= 8))
    engine = surface.history.sessions.kw["bind"]

    def saved_reads_only(conn, cursor, statement, parameters, context, many):
        assert statement.split(None, 1)[0].upper() in {"SELECT", "SET", "SHOW", "BEGIN"}
        assert "agent_messages" not in statement.lower()
        assert "usage_ledger_entries" not in statement.lower()

    query = {"view": "reporting_history", "node_id": "node-a", "limit": 2}
    with TestClient(app) as client:
        assert client.post("/internal/usage/activity/query", json=query).status_code == 401
        event.listen(engine, "before_cursor_execute", saved_reads_only)
        try:
            result = client.post("/internal/usage/activity/query", headers=HEADERS, json=query)
            assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
            first = result.json()
            assert len(first["items"]) == 2 and first["next_before_seq"] == "10"
            assert first["items"][0]["large_report_interval"]
            assert not first["items"][0]["large_received_interval"]
            assert first["items"][0]["cause"] == "unknown"
            assert "fingerprint" not in result.text and "username" not in result.text
            second = client.post(
                "/internal/usage/activity/query",
                headers=HEADERS,
                json={**query, "before_seq": first["next_before_seq"]},
            ).json()
            assert len(second["items"]) == 1 and second["next_before_seq"] is None
            assert second["items"][0] not in first["items"]
        finally:
            event.remove(engine, "before_cursor_execute", saved_reads_only)
        for change in (
            {"limit": 33},
            {"limit": True},
            {"before_seq": "0"},
            {"before_seq": "9223372036854775808"},
            {"before_seq": 10},
            {"agent_id": "other"},
            {"owner_id": "other"},
        ):
            assert (
                client.post(
                    "/internal/usage/activity/query", headers=HEADERS, json={**query, **change}
                ).status_code
                == 422
            )
        assert (
            client.post(
                "/internal/usage/activity/query",
                headers=HEADERS,
                json={**query, "node_id": "node-b"},
            ).status_code
            == 403
        )
        original = ReportingGapHistoryService.latest

        def revoke_after_read(reader, *args, **kwargs):
            result = original(reader, *args, **kwargs)
            with surface.history.sessions.begin() as db:
                db.execute(
                    delete(RolePermission).where(
                        RolePermission.role_id == ids["role"],
                        RolePermission.permission_id == ids["all"],
                    )
                )
            return result

        monkeypatch.setattr(ReportingGapHistoryService, "latest", revoke_after_read)
        assert (
            client.post("/internal/usage/activity/query", headers=HEADERS, json=query).status_code
            == 403
        )


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
        fixed = client.post(
            "/internal/usage/activity/query",
            headers=HEADERS,
            json={**QUERY, "max_messages": 3, "through_seq": "5"},
        )
        assert fixed.status_code == 200
        assert fixed.json()["selected_through_seq"] == "5"
        assert fixed.json()["highest_committed_seq"] == "7" and fixed.json()["range_complete"]
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


def test_archive_ui_distinguishes_retired_prefix_from_missing_packets(preview_source, settings):
    app, surface, _, _ = interface(preview_source, settings)
    # This fixture starts at seq 3. First it is a genuine missing prefix; then
    # seed the independent retained acknowledgments and completed exit boundary.
    with TestClient(app) as client:
        initial = client.post(
            "/internal/usage/activity/query",
            headers=HEADERS,
            json={"node_id": "node-a", "max_messages": 128},
        ).json()
        assert initial["missing_seq"] == "1" and not initial["range_complete"]
        with surface.history.sessions.begin() as db:
            for seq in (1, 2):
                db.add(
                    AgentMessageReceipt(
                        agent_id="agent-a",
                        seq=seq,
                        message_id=str(uuid4()),
                        fingerprint="a" * 64,
                        applied_to_current=True,
                    )
                )
            db.get(AgentArchiveState, "agent-a").retired_through_seq = 2
        response = client.post(
            "/internal/usage/activity/query",
            headers=HEADERS,
            json={"node_id": "node-a", "max_messages": 128},
        )
        assert response.status_code == 200
        value = response.json()
        assert value["range_complete"] and value["missing_seq"] is None
        assert value["source_packets"] == 5 and value["requested_after_seq"] == "2"
        assert value["source_retention"] == {
            "retired_through_seq": "2",
            "receipts_compacted_through_seq": "0",
        }
        expired = client.post(
            "/internal/usage/activity/query",
            headers=HEADERS,
            json={"node_id": "node-a", "max_messages": 128, "through_seq": "2"},
        )
        assert expired.status_code == 410 and expired.json()["error"]["code"] == "ARCHIVE_EXPIRED"
        # An older saved preview fingerprint remains independently reproducible.
        fixed = surface.activity.reader.recent(
            preview_source[2],
            UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a"),
            RULE,
            max_messages=5,
            through_seq=7,
        )
        assert fixed.fingerprint == value["source_result_fingerprint"]


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
            {"through_seq": 5},
            {"through_seq": True},
            {"through_seq": "0"},
            {"through_seq": "01"},
            {"through_seq": "5.0"},
            {"through_seq": "9223372036854775808"},
            {"through_seq": "8"},
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
    assert raw["selected_through_seq"] == str(first + 1)
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
