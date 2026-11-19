import csv
import io
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, text

from aris.apps.api.main import create_app
from aris.core.access.models import RoleBinding
from aris.core.contracts import DomainError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.group_report import group_html_report, group_summary_csv
from aris.modules.lyra.group_usage import GroupUsageQuery, group_usage_report
from aris.modules.lyra.group_usage_service import GroupUsageService
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.usage_history import UsageHistoryQuery, usage_history
from aris.modules.lyra.usage_history_service import UsageHistoryService
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import BASE, OWNER, measured, readings
from tests.test_usage_derivations import derivations
from tests.test_usage_history import TOKEN, surface_for
from tests.test_usage_ledger import POST, counts, ledger, post_window

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings"]


def query(**changes):
    return GroupUsageQuery(
        **{
            "node_ids": ("node-b", "node-a"),
            "start_at": BASE,
            "end_at": BASE + timedelta(seconds=120),
            "timezone": "UTC",
            **changes,
        }
    )


def history(node, entries, *, owner=None, read_at=None):
    return usage_history(
        UsageHistoryQuery(
            node_id=node, start_at=BASE, end_at=BASE + timedelta(seconds=120), timezone="UTC"
        ),
        owner_id=owner,
        head=UsageLedgerHead(
            node_id=node,
            revision=len(entries),
            fingerprint=entries[-1].fingerprint if entries else None,
        ),
        entries=entries,
        read_at=read_at or BASE + timedelta(seconds=200),
    )


def test_selected_node_totals_keep_daily_owner_and_node_breakdowns_consistent():
    other = uuid4()
    first = measured()
    second = measured(previous=first, owner_id=other, gpu_uuid="GPU-other", start=40, end=60)
    third = measured(node_id="node-b", start=20, end=80)
    values = (history("node-a", (first, second)), history("node-b", (third,)))
    result = group_usage_report(
        query(),
        values,
        owner_id=None,
        started_at=BASE + timedelta(seconds=199),
        finished_at=BASE + timedelta(seconds=201),
    )
    assert result.recorded_microseconds == 110000000
    assert (
        sum(day.recorded_microseconds for day in result.days)
        == sum(owner.recorded_microseconds for owner in result.owners)
        == 110000000
    )
    assert result.gpu_instance_count == 3 and result.segment_count == 3
    own = next(value for value in result.owners if value.owner_id == OWNER)
    assert (
        own.recorded_microseconds == 90000000
        and own.node_count == 2
        and own.gpu_instance_count == 2
    )
    assert (
        result.total_usage_microseconds is None
        and result.source_coverage == "unknown"
        and not result.quota_usage_available
    )
    later = tuple(
        history(
            item.node_id,
            (first, second) if item.node_id == "node-a" else (third,),
            read_at=BASE + timedelta(seconds=250),
        )
        for item in values
    )
    repeated = group_usage_report(
        result.query,
        later,
        owner_id=None,
        started_at=BASE + timedelta(seconds=249),
        finished_at=BASE + timedelta(seconds=251),
    )
    assert repeated.fingerprint == result.fingerprint


def test_group_input_does_not_accept_missing_nodes_duplicate_heads_or_partial_child_pages():
    for changes in (
        {"node_ids": ("node-a", "node-a")},
        {"snapshots": (UsageLedgerHead(node_id="node-a", revision=0),)},
    ):
        with pytest.raises(ValidationError):
            query(**changes)
    value = history("node-a", (measured(),))
    with pytest.raises(ValueError):
        group_usage_report(
            query(),
            (value,),
            owner_id=None,
            started_at=BASE,
            finished_at=BASE + timedelta(seconds=200),
        )
    with pytest.raises(ValueError):
        group_usage_report(
            query(),
            (value.model_copy(update={"rows": ()}), history("node-b", ())),
            owner_id=None,
            started_at=BASE,
            finished_at=BASE + timedelta(seconds=200),
        )


@pytest.fixture
def grouped(readings):
    _, (original, actor, _, ids), _ = readings
    history = UsageHistoryService(original.sessions, clock=original.clock)
    with history.sessions.begin() as session:
        session.add(
            RoleBinding(
                id=str(uuid4()),
                user_id=str(actor.actor_id),
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-b",
            )
        )
    return GroupUsageService(history), actor, readings


def test_group_service_fixes_every_node_prefix_and_keeps_empty_node_unknown(grouped):
    service, actor, readings = grouped
    before = counts(readings[0])
    result = service.read(actor, query())
    assert result.recorded_microseconds == 30000000 and result.owner_id == actor.actor_id
    assert [(item.node_id, item.snapshot.revision) for item in result.histories] == [
        ("node-a", 1),
        ("node-b", 0),
    ]
    assert (
        result.histories[1].recorded_microseconds == 0
        and result.histories[1].total_usage_microseconds is None
    )
    assert counts(readings[0]) == before
    readings[0][0].post(POST, post_window(readings[0], start=40, seq=101))
    current = service.read(actor, query())
    assert current.recorded_microseconds == 60000000 and current.fingerprint != result.fingerprint
    assert service.read(actor, result.query).fingerprint == result.fingerprint
    with service.history.sessions() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
        assert (
            service.read_in_transaction(session, actor, result.query).fingerprint
            == result.fingerprint
        )


def test_missing_current_node_permission_and_second_node_failure_reject_whole_group(
    grouped, monkeypatch
):
    service, actor, _ = grouped
    with service.history.sessions.begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.scope_id == "node-b"))
    with pytest.raises(DomainError) as denied:
        service.read(actor, query())
    assert denied.value.code == "PERMISSION_DENIED"
    original = service.history.read_in_transaction

    def fail_second(session, actor, selection, **kwargs):
        if selection.node_id == "node-b":
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Synthetic node report failure", 503)
        return original(session, actor, selection, **kwargs)

    monkeypatch.setattr(service.history, "read_in_transaction", fail_second)
    with pytest.raises(DomainError) as failed:
        service.read(actor, query())
    assert failed.value.code == "DEPENDENCY_UNAVAILABLE"


def test_group_rechecks_first_node_after_later_node_read(grouped, monkeypatch):
    service, actor, _ = grouped
    original = service.history.read_in_transaction

    def revoke_first(session, actor, selection, **kwargs):
        result = original(session, actor, selection, **kwargs)
        if selection.node_id == "node-b":
            session.execute(delete(RoleBinding).where(RoleBinding.scope_id == "node-a"))
        return result

    monkeypatch.setattr(service.history, "read_in_transaction", revoke_first)
    with pytest.raises(DomainError) as revoked:
        service.read(actor, query(), mode="node")
    assert revoked.value.code == "PERMISSION_DENIED"


def test_group_api_authenticates_every_node_and_exports_the_displayed_heads(grouped, settings):
    service, actor, readings = grouped
    surface, _, _ = surface_for(readings, settings)
    surface.history = service.history
    headers = {"Authorization": "Bearer " + TOKEN}
    selected = query()
    body = {
        "node_ids": list(selected.node_ids),
        "mode": "mine",
        "window": {
            "kind": "utc",
            "start_at": selected.start_at.isoformat(),
            "end_at": selected.end_at.isoformat(),
            "timezone": "UTC",
        },
    }
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface)
    ) as client:
        response = client.post("/internal/usage/groups/query", headers=headers, json=body)
        assert response.status_code == 200
        report = response.json()["report"]
        assert (
            report["recorded_microseconds"] == 30000000
            and report["total_usage_microseconds"] is None
        )
        assert len(report["nodes"]) == 2 and report["nodes"][1]["recorded_microseconds"] == 0
        fixed = {**body, "snapshots": report["query"]["snapshots"]}
        first = client.post("/internal/usage/groups/csv", headers=headers, json=fixed)
        assert first.status_code == 200
        rows = list(csv.DictReader(io.StringIO(first.content.decode("utf-8-sig"))))
        assert [(row["node"], row["recorded_microseconds"]) for row in rows] == [
            ("node-a", "30000000"),
            ("node-b", "0"),
        ]
        assert all(
            row["source_coverage"] == "unknown" and row["total_usage_microseconds"] == ""
            for row in rows
        )
        html = client.post("/internal/usage/groups/report", headers=headers, json=fixed)
        assert html.status_code == 200 and report["fingerprint"] in html.text
        assert "<script" not in html.text and "各节点额度" in html.text
        readings[0][0].post(POST, post_window(readings[0], start=40, seq=101))
        assert (
            client.post("/internal/usage/groups/csv", headers=headers, json=fixed).content
            == first.content
        )
        assert client.post("/internal/usage/groups/query", json=body).status_code == 401
        assert (
            client.post("/internal/usage/groups/csv", headers=headers, json=body).status_code == 422
        )
        assert (
            client.post(
                "/internal/usage/groups/query",
                headers=headers,
                json={**body, "node_ids": ["node-a", "outside"]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/usage/groups/query",
                headers=headers,
                json={**body, "owner_id": str(actor.actor_id)},
            ).status_code
            == 422
        )


def test_group_exports_escape_display_labels_and_preserve_empty_nodes():
    value = history("node-a", (measured(),), owner=OWNER)
    empty = history("node-b", (), owner=OWNER)
    report = group_usage_report(
        query(),
        (value, empty),
        owner_id=OWNER,
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=200),
    )
    labels = {str(OWNER): "=SUM(1,1)<img src=x>"}
    rows = list(csv.DictReader(io.StringIO(group_summary_csv(report, labels).decode("utf-8-sig"))))
    assert len(rows) == 2 and all(row["owner_display_name"].startswith("'=SUM") for row in rows)
    html = group_html_report(report, labels).decode()
    assert "<img src=x>" not in html and "&lt;img src=x&gt;" in html


def test_group_export_rechecks_permissions_after_rendering(grouped, settings, monkeypatch):
    import aris.interfaces.usage.group_service as adapter

    service, actor, readings = grouped
    surface, _, _ = surface_for(readings, settings)
    surface.history = service.history
    selected = service.read(actor, query())
    request = adapter.GroupUsageInput(
        node_ids=selected.query.node_ids,
        mode="mine",
        snapshots=selected.query.snapshots,
        window={
            "kind": "utc",
            "start_at": selected.query.start_at,
            "end_at": selected.query.end_at,
            "timezone": "UTC",
        },
    )
    original = adapter.group_summary_csv

    def revoke(*args):
        result = original(*args)
        with service.history.sessions.begin() as session:
            session.execute(
                delete(RoleBinding).where(
                    RoleBinding.user_id == str(actor.actor_id), RoleBinding.scope_id == "node-b"
                )
            )
        return result

    monkeypatch.setattr(adapter, "group_summary_csv", revoke)
    with pytest.raises(DomainError) as denied:
        adapter.group_usage(
            surface, "Bearer " + TOKEN, "fixture-group-export", request, output="csv"
        )
    assert denied.value.code == "PERMISSION_DENIED"
