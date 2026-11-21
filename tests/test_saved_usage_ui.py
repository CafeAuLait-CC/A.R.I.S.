import csv
import io
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, event, select, text

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, Permission, RoleBinding, RolePermission
from aris.core.access.schemas import AccessScope, ExternalSubject
from aris.core.commands.schemas import ActorContext, CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.saved_report import saved_csv, saved_report
from aris.interfaces.usage.saved_service import SavedUsageDetail
from aris.interfaces.usage.service import UsageHistorySurface
from aris.modules.lyra.coverage_rollup import roll_up_saved_coverage
from aris.modules.lyra.ledger_commands import ReverseUsageMeasurementCommand
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import PostedMeasurement
from aris.modules.lyra.usage_history_service import UsageHistoryService
from aris.modules.lyra.window_reader import UsageWindowReader
from aris.modules.lyra.window_reader_schemas import SavedWindowView, WindowListQuery
from aris.modules.lyra.window_schemas import (
    SampledUsageWindow,
    UsageWindowQuery,
    WindowCoverageCell,
    usage_window_fingerprint,
)
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.lyra.window_snapshot_schemas import StoredUsageWindow, window_snapshot_fingerprint
from aris.modules.nova.coverage_schedule_reader import CoverageScheduleReader
from aris.modules.vega.target_service import VegaTargetService
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
        history,
        auth,
        ("node-a",),
        saved_windows=UsageWindowReader(history.sessions, clock=history.clock),
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


def test_opt_in_plan_summary_preserves_default_snapshot_response_and_cannot_choose_ids(saved_ui):
    state, client, surface, _, _, _ = saved_ui
    assert not client.post("/internal/usage/session", headers=HEADERS).json()[
        "coverage_plans_available"
    ]
    before = counts(state.service)
    response = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection(include_plans=True)
    )
    assert response.status_code == 503
    surface.coverage_plans = CoverageScheduleReader(surface.history.sessions, (uuid4(),))
    assert client.post("/internal/usage/session", headers=HEADERS).json()[
        "coverage_plans_available"
    ]
    default = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection()
    ).json()
    assert "plans" not in default and "items" in default
    response = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection(include_plans=True)
    )
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    assert response.json()["snapshots"] == default and response.json()["plans"]["items"] == []
    assert (
        client.post(
            "/internal/usage/snapshots/query",
            headers=HEADERS,
            json=selection(include_plans=True, plan_ids=[str(uuid4())]),
        ).status_code
        == 422
    )
    assert counts(state.service) == before


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

        def no_original_samples(conn, cursor, sql, *args):
            assert "agent_messages" not in sql.lower()

        engine = state.service.sessions.kw["bind"]
        event.listen(engine, "before_cursor_execute", no_original_samples)
        try:
            for grain in ("day", "week"):
                compact = client.post(
                    "/internal/usage/snapshots/read",
                    headers=HEADERS,
                    json={**selected, "grain": grain},
                )
                assert compact.status_code == 200
                view = compact.json()["rollup"]
                assert view["snapshot_fingerprint"] == snapshot.fingerprint
                assert view["coverage"] == details.view.coverage
                assert view["total_usage_microseconds"] == details.view.total_usage_microseconds
                assert "cells" not in view
                assert sum(
                    duration["microseconds"]
                    for bucket in view["buckets"]
                    for duration in bucket["durations"]
                ) == sum(cell.microseconds for cell in details.view.cells)
        finally:
            event.remove(engine, "before_cursor_execute", no_original_samples)
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

    state, client, surface, actor, _, _ = saved_ui
    surface.saved_windows.personal_node_projection = True
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
        json={"node_id": "node-a", "mode": "mine", "id": str(snapshot.request.id)},
    )
    assert response.status_code == 403 and response.json()["error"]["code"] == "PERMISSION_DENIED"


def test_shared_node_snapshot_projects_only_self_and_keeps_unknown_and_frozen_evidence(
    saved_ui, settings
):
    state, client, surface, actor, other, ids = saved_ui
    surface.gpu_labels = VegaTargetService(surface.history.sessions, settings)
    original = incoming_window(state)
    complete = state.service.capture(CONTEXT, original).snapshot
    partial = state.service.capture(
        CONTEXT,
        original.model_copy(
            update={
                "id": uuid4(),
                "query": original.query.model_copy(
                    update={
                        "start_at": original.query.start_at - timedelta(seconds=10),
                        "end_at": original.query.end_at + timedelta(seconds=60),
                    }
                ),
            }
        ),
    ).snapshot
    unknown = state.service.capture(
        CONTEXT,
        original.model_copy(
            update={
                "id": uuid4(),
                "manifests": (),
            }
        ),
    ).snapshot
    foreign = state.service.capture(CONTEXT, incoming_window(state, owner=UUID(other))).snapshot
    chosen = {"node_id": "node-a", "mode": "mine", "id": str(complete.request.id)}
    assert (
        client.post("/internal/usage/snapshots/read", headers=HEADERS, json=chosen).status_code
        == 403
    )
    surface.saved_windows.personal_node_projection = True
    with surface.history.sessions.begin() as db:
        db.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
        db.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(other),
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-a",
            )
        )
    before = counts(state.service)
    results = {}
    for snapshot in (complete, partial, unknown):
        response = client.post(
            "/internal/usage/snapshots/read",
            headers=HEADERS,
            json={**chosen, "id": str(snapshot.request.id)},
        )
        assert response.status_code == 200, response.text
        detail = response.json()
        if detail["view"]["cells"]:
            assert detail["gpu_indices"] == {"GPU-a": 0}
        view = detail["view"]
        assert view["projection"] == "personal_from_node" and view["detail_scope"] == "mine"
        assert view["query"]["owner_id"] == str(actor.actor_id)
        assert view["recorded_microseconds"] == 30000000
        assert (view["total_usage_microseconds"] is not None) == (view["coverage"] == "complete")
        assert view["snapshot_fingerprint"] == snapshot.fingerprint
        assert view["view_fingerprint"] != snapshot.fingerprint
        assert str(other) not in response.text
        assert all(cell["owner_id"] == str(actor.actor_id) for cell in view["cells"])
        results[snapshot.window.coverage] = detail
    assert set(results) == {"complete", "partial", "unknown"}
    listed = client.post(
        "/internal/usage/snapshots/query", headers=HEADERS, json=selection(mode="mine")
    ).json()
    assert {row["id"] for row in listed["items"]} == {
        str(s.request.id) for s in (complete, partial, unknown)
    }
    assert all(
        row["summary_totals"] == "not_loaded"
        and row["recorded_microseconds"] is None
        and row["verified_usage_microseconds"] is None
        and row["total_usage_microseconds"] is None
        for row in listed["items"]
    )
    assert (
        client.post(
            "/internal/usage/snapshots/read",
            headers=HEADERS,
            json={**chosen, "id": str(foreign.request.id)},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/internal/usage/snapshots/read", headers=HEADERS, json={**chosen, "mode": "node"}
        ).status_code
        == 403
    )
    for snapshot in (complete, partial, unknown):
        with surface.history.sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
            empty = surface.saved_windows.read_in_transaction(
                db,
                ActorContext(actor_id=UUID(other), request_id="zero-member-view"),
                snapshot.request.id,
                node_id="node-a",
                mode="mine",
            )
            assert (
                empty.cells == ()
                and empty.recorded_microseconds == empty.verified_usage_microseconds == 0
            )
            assert empty.total_usage_microseconds == (
                0 if snapshot.window.coverage == "complete" else None
            )
            assert str(actor.actor_id) not in empty.model_dump_json()
    csv_response = client.post(
        "/internal/usage/snapshots/read", headers={**HEADERS, "Accept": "text/csv"}, json=chosen
    )
    html_response = client.post(
        "/internal/usage/snapshots/read", headers={**HEADERS, "Accept": "text/html"}, json=chosen
    )
    assert csv_response.status_code == html_response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
    assert (
        rows[0]["projection"] == "personal_from_node"
        and int(rows[0]["recorded_microseconds"]) == 30000000
    )
    assert rows[0]["view_fingerprint"] == results["complete"]["view"]["view_fingerprint"]
    assert "GPU 0" in html_response.text
    assert str(other) not in csv_response.text + html_response.text
    assert counts(state.service) == before
    # A real confirmed later reversal changes current history, but not this saved parent.
    ledger = state.state.ledger[0]
    with surface.history.sessions.begin() as db:
        target = next(
            entry
            for entry in UsageLedgerRepository(db).prefix(complete.window.ledger)
            if isinstance(entry.details, PostedMeasurement)
            and entry.details.measurement.owner_id == actor.actor_id
        )
        permission = str(uuid4())
        db.add(Permission(id=permission, name="usage.adjust"))
        db.flush()
        db.add(RolePermission(role_id=ids["role"], permission_id=permission))
    state.state.ledger[2][2][5]["now"] += timedelta(seconds=10)
    commands = CommandService(
        surface.history.sessions,
        [ReverseUsageMeasurementCommand(ledger)],
        confirmation_key=SecretStr("fixture-shared-view-reversal-key-1234567890"),
        clock=surface.history.clock,
    )
    review = commands.execute(
        CommandContext(
            actor_id=actor.actor_id,
            request_id="saved-view-review",
            idempotency_key="saved-view-reversal",
        ),
        CommandRequest(
            command="usage.reverse_measurement",
            scope=AccessScope(type="node", id="node-a"),
            parameters={
                "target_id": str(target.id),
                "expected_target_fingerprint": target.fingerprint,
                "expected_target_revision": target.revision,
                "reason": "fixture later correction",
            },
        ),
    )
    done = commands.confirm(actor, ConfirmCommand(command_id=review.command_id, proof=review.proof))
    assert done.state == "succeeded" and done.result["effect"] == "reversed"
    repeated = client.post("/internal/usage/snapshots/read", headers=HEADERS, json=chosen)
    assert repeated.status_code == 200 and repeated.json() == results["complete"]
    assert (
        client.post(
            "/internal/usage/snapshots/read", headers={**HEADERS, "Accept": "text/csv"}, json=chosen
        ).content
        == csv_response.content
    )
    assert (
        client.post(
            "/internal/usage/snapshots/read",
            headers={**HEADERS, "Accept": "text/html"},
            json=chosen,
        ).content
        == html_response.content
    )
    with surface.history.sessions.begin() as db:
        db.execute(delete(RoleBinding).where(RoleBinding.user_id == str(actor.actor_id)))
    assert (
        client.post("/internal/usage/snapshots/read", headers=HEADERS, json=chosen).status_code
        == 403
    )


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
    daily = client.post(
        "/internal/usage/snapshots/read",
        headers=HEADERS,
        json={"node_id": "node-a", "mode": "mine", "id": str(saved.request.id), "grain": "day"},
    )
    assert daily.status_code == 200 and str(other) not in daily.text
    assert daily.json()["rollup"]["bucket_coverage"] == "owned_recorded_cells"
    assert all(not bucket["durations"] for bucket in daily.json()["rollup"]["buckets"])
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


def test_rollup_splits_a_fall_back_day_and_preserves_unknown_seconds():
    # 31 October 23:00 EDT to 2 November 01:00 EST covers a 25h local Sunday.
    start = datetime(2026, 11, 1, 3, tzinfo=UTC)
    end = datetime(2026, 11, 2, 6, tzinfo=UTC)
    owner = uuid4()
    active_end = datetime(2026, 11, 1, 6, tzinfo=UTC)
    minute = 60 * 1000000
    view = SavedWindowView(
        id=uuid4(),
        query=UsageWindowQuery(
            node_id="node-a", start_at=start, end_at=end, timezone="America/New_York"
        ),
        created_at=end,
        captured_at=end,
        detail_scope="node",
        coverage="partial",
        source_prefix_complete=True,
        recorded_microseconds=3 * 60 * minute,
        verified_usage_microseconds=3 * 60 * minute,
        total_usage_microseconds=None,
        covered_gpu_microseconds=3 * 60 * minute,
        unknown_gpu_microseconds=24 * 60 * minute,
        uncovered_gpu_microseconds=0,
        cells=(
            WindowCoverageCell(
                gpu_uuid="GPU-a",
                start_at=start,
                end_at=active_end,
                state="recorded",
                owner_id=owner,
                reason="VERIFIED",
            ),
            WindowCoverageCell(
                gpu_uuid="GPU-a",
                start_at=active_end,
                end_at=end,
                state="unknown",
                reason="SOURCE_QUALITY_UNCERTAIN",
            ),
        ),
        source_fingerprint="1" * 64,
        snapshot_fingerprint="2" * 64,
    )
    daily, weekly = (roll_up_saved_coverage(view, grain) for grain in ("day", "week"))
    assert tuple(b.end_at - b.start_at for b in daily.buckets) == (
        timedelta(hours=1),
        timedelta(hours=25),
        timedelta(hours=1),
    )
    assert [row.microseconds for row in daily.buckets[1].durations] == [
        2 * 60 * minute,
        23 * 60 * minute,
    ]
    assert [b.local_period_start.weekday() for b in weekly.buckets] == [0, 0]
    assert daily.total_usage_microseconds is None and weekly.total_usage_microseconds is None


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
