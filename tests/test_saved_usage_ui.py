import csv
import io
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, select, text

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding, RolePermission
from aris.core.access.schemas import ExternalSubject
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.saved_report import saved_csv, saved_report
from aris.interfaces.usage.saved_service import SavedUsageDetail
from aris.interfaces.usage.service import UsageHistorySurface
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_reader_schemas import WindowListQuery
from aris.modules.lyra.window_schemas import SampledUsageWindow, usage_window_fingerprint
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow, window_snapshot_fingerprint
from tests.test_administration import admin_state
from tests.test_closed_run_recording import closed
from tests.test_mapping_history import history_state
from tests.test_processing_coverage import coverage
from tests.test_recorded_usage import reading_service
from tests.test_trace_consumer import CONTEXT
from tests.test_usage_derivations import derivations
from tests.test_usage_history import TOKEN
from tests.test_usage_ledger import ledger
from tests.test_usage_report import ReadReport
from tests.test_window_snapshots import counts, incoming_window, windows

__all__ = ["admin_state", "history_state", "derivations", "ledger", "closed", "coverage", "windows"]
HEADERS = {"Authorization": "Bearer " + TOKEN}


def selection(**changes):
    return {
        "node_id": "node-a",
        "mode": "node",
        "window": {
            "kind": "calendar",
            "start_date": "2026-11-16",
            "end_date": "2026-11-16",
            "timezone": "UTC",
        },
        **changes,
    }


@pytest.fixture
def saved_ui(windows, settings):
    _, actor, other, ids = reading_service(windows.state.ledger)
    history = UsageHistoryService(windows.service.sessions, clock=windows.service.clock)
    with history.sessions() as db:
        identity = db.scalar(
            select(ExternalIdentity).where(ExternalIdentity.user_id == str(actor.actor_id))
        )
        subject = ExternalSubject(
            provider=identity.provider,
            namespace=identity.namespace,
            external_id=identity.external_id,
        )
    auth = BoundSubjectTokens(
        [UserTokenBinding(subject=subject, token=SecretStr(TOKEN))],
        reserved_tokens=[
            settings.read_token,
            *(value.token for value in settings.agent_credentials.values()),
        ],
    )
    surface = UsageHistorySurface(
        history, auth, ("node-a",), saved_windows=UsageWindowReader(history.sessions)
    )
    app = create_app(
        settings,
        SlackSettings(_env_file=None, signing_secret="", bot_token="", team_id=""),
        usage_history=surface,
    )
    with TestClient(app) as client:
        yield windows, client, surface, actor, other, ids


def test_saved_list_keyset_paging_and_detail_share_authorization_without_writing(saved_ui):
    state, client, surface, actor, _, _ = saved_ui
    snapshots = [state.service.capture(CONTEXT, incoming_window(state)).snapshot for _ in range(3)]
    before = counts(state.service)
    if surface.history.sessions.kw["bind"].dialect.name == "postgresql":
        with surface.history.sessions() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            page = surface.saved_windows.list_in_transaction(
                db,
                actor,
                WindowListQuery(**snapshots[0].window.query.model_dump(), limit=1),
                mode="node",
            )
            assert len(page.items) == 1
            assert (
                surface.saved_windows.read_in_transaction(
                    db, actor, snapshots[0].request.id, node_id="node-a", mode="node"
                ).coverage
                == "complete"
            )
    assert client.post("/internal/usage/session", headers=HEADERS).json()[
        "saved_snapshots_available"
    ]
    input = selection(limit=1)
    found = []
    while True:
        response = client.post("/internal/usage/snapshots/query", json=input, headers=HEADERS)
        assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
        result = response.json()
        found.extend(item["id"] for item in result["items"])
        if result["next_cursor"] is None:
            break
        input["before"] = result["next_cursor"]
    assert len(found) == len(set(found)) == 3
    assert set(found) == {str(value.request.id) for value in snapshots}
    result = client.post(
        "/internal/usage/snapshots/read",
        headers=HEADERS,
        json={"node_id": "node-a", "mode": "node", "id": found[0]},
    )
    assert result.status_code == 200
    view = result.json()["view"]
    assert view["total_usage_microseconds"] == 30_000_000 and view["coverage"] == "complete"
    assert view["detail_scope"] == "node"
    assert "configuration" not in view and "manifests" not in view
    assert counts(state.service) == before
    assert not hasattr(surface.saved_windows, "capture")
    assert (
        client.post("/internal/usage/snapshots/capture", headers=HEADERS, json={}).status_code
        == 404
    )


def test_saved_reports_and_csv_preserve_exact_snapshot_quality_and_safe_labels(saved_ui):
    state, client, _, _, _, _ = saved_ui
    complete = incoming_window(state)
    partial = complete.model_copy(
        update={
            "id": uuid4(),
            "query": complete.query.model_copy(
                update={
                    "start_at": complete.query.start_at - timedelta(seconds=10),
                    "end_at": complete.query.end_at + timedelta(seconds=60),
                }
            ),
        }
    )
    qualities = set()
    for original in (complete, partial):
        snapshot = state.service.capture(CONTEXT, original).snapshot
        selected = {"node_id": "node-a", "mode": "node", "id": str(snapshot.request.id)}
        details = SavedUsageDetail.model_validate(
            client.post("/internal/usage/snapshots/read", headers=HEADERS, json=selected).json()
        )
        qualities.add(details.view.coverage)
        before = counts(state.service)
        exported = client.post(
            "/internal/usage/snapshots/read",
            headers={**HEADERS, "Accept": "text/csv"},
            json=selected,
        )
        html = client.post(
            "/internal/usage/snapshots/read",
            headers={**HEADERS, "Accept": "text/html"},
            json=selected,
        )
        assert exported.status_code == html.status_code == 200
        assert (
            "no-store" in exported.headers["cache-control"]
            and "attachment" in html.headers["content-disposition"]
        )
        rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert len(rows) == len(details.view.cells) + 1 and rows[0]["record_type"] == "summary"
        assert {row["snapshot_fingerprint"] for row in rows} == {snapshot.fingerprint}
        assert rows[0]["recorded_microseconds"] == str(details.view.recorded_microseconds)
        assert all(row["recorded_microseconds"] == "" for row in rows[1:])
        expected = (
            ""
            if details.view.total_usage_microseconds is None
            else str(details.view.total_usage_microseconds)
        )
        assert rows[0]["total_usage_microseconds"] == expected
        assert ("<strong>未知</strong>" in html.text) == (
            details.view.total_usage_microseconds is None
        )
        parser = ReadReport()
        parser.feed(html.text)
        assert (
            parser.scripts == parser.external_attributes == 0 and snapshot.fingerprint in html.text
        )
        assert counts(state.service) == before
        # Rendering accepts labels as untrusted text; it cannot turn them into HTML or formulas.
        owners = {
            str(cell.owner_id): '<script>alert("fixture")</script>'
            for cell in details.view.cells
            if cell.owner_id
        }
        hostile = saved_report(details.view, owners).decode()
        parser = ReadReport()
        parser.feed(hostile)
        assert parser.scripts == parser.external_attributes == 0
        if owners:
            assert "&lt;script&gt;" in hostile
            formulas = saved_csv(details.view, {owner: "=2+3" for owner in owners})
            assert all(
                row["display_name"] == "'=2+3"
                for row in csv.DictReader(io.StringIO(formulas.decode("utf-8-sig")))
                if row["owner_id"]
            )
        state.service.capture(CONTEXT, incoming_window(state))
        assert (
            client.post(
                "/internal/usage/snapshots/read",
                headers={**HEADERS, "Accept": "text/csv"},
                json=selected,
            ).content
            == exported.content
        )
    assert qualities == {"complete", "partial"}


@pytest.mark.parametrize("format", ["export", "report"])
def test_saved_export_rechecks_current_permission_after_render(saved_ui, monkeypatch, format):
    import aris.interfaces.usage.saved_service as presentation

    state, client, _, actor, _, _ = saved_ui
    snapshot = state.service.capture(CONTEXT, incoming_window(state)).snapshot
    original = presentation.saved_csv if format == "export" else presentation.saved_report

    def revoke_after_render(*args):
        body = original(*args)
        with state.service.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
        return body

    monkeypatch.setattr(
        presentation, "saved_csv" if format == "export" else "saved_report", revoke_after_render
    )
    response = client.post(
        "/internal/usage/snapshots/read",
        headers={**HEADERS, "Accept": "text/html" if format == "report" else "text/csv"},
        json={"node_id": "node-a", "mode": "node", "id": str(snapshot.request.id)},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_personal_and_node_results_are_distinct_and_cannot_be_selected_by_impersonation(saved_ui):
    state, client, _, actor, other, ids = saved_ui
    personal = state.service.capture(CONTEXT, incoming_window(state, owner=actor.actor_id)).snapshot
    state.service.capture(CONTEXT, incoming_window(state))
    response = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection(mode="mine")
    )
    assert [item["id"] for item in response.json()["items"]] == [str(personal.request.id)]
    assert (
        client.post(
            "/internal/usage/snapshots/query",
            headers=HEADERS,
            json=selection(mode="mine", owner_id=str(other)),
        ).status_code
        == 422
    )
    with state.service.sessions.begin() as db:
        db.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
    assert (
        client.post(
            "/internal/usage/snapshots/query", headers=HEADERS, json=selection(mode="node")
        ).status_code
        == 403
    )
    result = client.post(
        "/internal/usage/snapshots/read",
        headers=HEADERS,
        json={"node_id": "node-a", "mode": "mine", "id": str(personal.request.id)},
    )
    assert result.status_code == 200 and result.json()["view"]["detail_scope"] == "mine"
    with state.service.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))
    assert (
        client.post(
            "/internal/usage/snapshots/query", headers=HEADERS, json=selection(mode="mine")
        ).status_code
        == 403
    )


def test_personal_projection_never_returns_other_owners_intervals(saved_ui, monkeypatch):
    state, client, _, actor, other, _ = saved_ui
    saved = state.service.capture(CONTEXT, incoming_window(state, owner=actor.actor_id)).snapshot
    # Read-side fixture: a complete node window whose sole busy owner is someone else.
    window = saved.window.model_copy(
        update={
            "cells": tuple(
                cell.model_copy(update={"owner_id": UUID(other)}) for cell in saved.window.cells
            ),
            "recorded_microseconds": 0,
            "verified_usage_microseconds": 0,
            "total_usage_microseconds": 0,
        }
    )
    window = SampledUsageWindow.model_validate(
        {**window.model_dump(mode="json"), "fingerprint": usage_window_fingerprint(window)}
    )
    fixture = saved.model_copy(update={"window": window})
    fixture = StoredUsageWindow.model_validate(
        {**fixture.model_dump(mode="json"), "fingerprint": window_snapshot_fingerprint(fixture)}
    )
    get = UsageWindowRepository.get
    monkeypatch.setattr(
        UsageWindowRepository,
        "get",
        lambda repo, id: fixture if id == saved.request.id else get(repo, id),
    )
    result = client.post(
        "/internal/usage/snapshots/read",
        headers=HEADERS,
        json={"node_id": "node-a", "mode": "mine", "id": str(saved.request.id)},
    )
    assert result.status_code == 200
    assert result.json()["view"]["cells"] == [] and result.json()["labels"] == {}
    assert str(other) not in result.text
    assert result.json()["view"]["total_usage_microseconds"] == 0
    for format in ("report", "export"):
        exported = client.post(
            "/internal/usage/snapshots/read",
            headers={**HEADERS, "Accept": "text/html" if format == "report" else "text/csv"},
            json={"node_id": "node-a", "mode": "mine", "id": str(saved.request.id)},
        )
        assert exported.status_code == 200 and str(other) not in exported.text
        if format == "export":
            rows = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
            assert (
                len(rows) == 1
                and rows[0]["record_type"] == "summary"
                and rows[0]["total_usage_microseconds"] == "0"
            )


def test_disabled_reader_is_explicit_and_reserved_credentials_cannot_query(saved_ui, settings):
    _, client, surface, _, _, _ = saved_ui
    surface.saved_windows = None
    assert not client.post("/internal/usage/session", headers=HEADERS).json()[
        "saved_snapshots_available"
    ]
    assert (
        client.post(
            "/internal/usage/snapshots/query", headers=HEADERS, json=selection()
        ).status_code
        == 503
    )
    for token in (
        None,
        settings.read_token.get_secret_value(),
        settings.agent_credentials["agent-a"].token.get_secret_value(),
    ):
        headers = {"Authorization": "Bearer " + token} if token else {}
        assert (
            client.post(
                "/internal/usage/snapshots/query", headers=headers, json=selection()
            ).status_code
            == 401
        )


def test_date_node_scope_and_invalid_cursor_do_not_return_unrelated_snapshots(saved_ui):
    state, client, _, _, _, _ = saved_ui
    snapshot = state.service.capture(CONTEXT, incoming_window(state)).snapshot
    assert (
        client.post(
            "/internal/usage/snapshots/query", headers=HEADERS, json=selection(node_id="node-b")
        ).status_code
        == 403
    )
    value = selection(
        window={
            "kind": "calendar",
            "start_date": "2026-11-15",
            "end_date": "2026-11-15",
            "timezone": "UTC",
        }
    )
    assert (
        client.post("/internal/usage/snapshots/query", headers=HEADERS, json=value).json()["items"]
        == []
    )
    assert (
        client.post(
            "/internal/usage/snapshots/read",
            headers=HEADERS,
            json={"node_id": "node-b", "mode": "node", "id": str(snapshot.request.id)},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/usage/snapshots/query",
            headers=HEADERS,
            json=selection(before={"id": str(uuid4()), "created_at": "2026-11-16T00:00:00"}),
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/internal/usage/snapshots/read?actor_id=fake",
            headers=HEADERS,
            json={"node_id": "node-a", "mode": "node", "id": str(snapshot.request.id)},
        ).status_code
        == 422
    )


def test_permissions_are_rechecked_after_display_labels_are_built(saved_ui, monkeypatch):
    state, client, surface, _, _, ids = saved_ui
    snapshot = state.service.capture(CONTEXT, incoming_window(state)).snapshot
    labels = surface.history.access.labels_in_transaction

    def revoke(session, query):
        result = labels(session, query)
        with state.service.sessions.begin() as other:
            other.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))
        return result

    monkeypatch.setattr(surface.history.access, "labels_in_transaction", revoke)
    response = client.post(
        "/internal/usage/snapshots/read",
        headers=HEADERS,
        json={"node_id": "node-a", "mode": "node", "id": str(snapshot.request.id)},
    )
    assert response.status_code == 403 and "labels" not in response.json()


def test_partial_window_summary_keeps_recorded_amount_but_does_not_claim_a_complete_total(saved_ui):
    from datetime import timedelta

    state, client, _, _, _, _ = saved_ui
    request = incoming_window(state)
    request = request.model_copy(
        update={
            "query": request.query.model_copy(
                update={
                    "start_at": request.query.start_at - timedelta(seconds=5),
                    "end_at": request.query.end_at + timedelta(seconds=5),
                }
            )
        }
    )
    state.service.capture(CONTEXT, request)
    result = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection()
    ).json()
    summary = result["items"][0]
    assert summary["coverage"] == "partial" and summary["total_usage_microseconds"] is None
    assert summary["recorded_microseconds"] == summary["verified_usage_microseconds"] == 30_000_000
