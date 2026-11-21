import csv
import io
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, event, select, text, update

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding, RolePermission, User
from aris.core.access.schemas import ExternalSubject
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.service import (
    UsageHistoryInput,
    UsageHistorySurface,
    history_csv,
)
from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
from aris.modules.lyra.ledger_schemas import PostedMeasurement, make_ledger_entry
from aris.modules.lyra.usage_history import UsageHistoryQuery, calendar_boundaries, usage_history
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import BASE, OWNER, head, measured, readings, reversed_measurement
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, counts, ledger, post_window, postgres

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings"]
TOKEN = "fixture-user-history-credential-123456789"


def selection(**changes):
    return UsageHistoryQuery(
        **{
            "node_id": "node-a",
            "start_at": BASE,
            "end_at": BASE + timedelta(seconds=120),
            "timezone": "UTC",
            **changes,
        }
    )


def reduce(entries, *, query=None, owner=None):
    return usage_history(
        query or selection(),
        owner_id=owner,
        head=head(entries),
        entries=entries,
        read_at=max(BASE + timedelta(seconds=200), (query or selection()).end_at),
    )


def test_node_user_totals_and_half_open_intersections_share_the_same_net_records():
    other = uuid4()
    first = measured()
    second = measured(previous=first, gpu_uuid="GPU-b", owner_id=other, start=20, end=60)
    third = measured(previous=second, gpu_uuid="GPU-c", start=30, end=90)
    result = reduce((first, second, third), query=selection(end_at=BASE + timedelta(seconds=50)))
    assert result.recorded_microseconds == (30 + 30 + 20) * 1000000
    assert result.gpu_count == 3 and result.segment_count == 3
    assert (
        sum(owner.recorded_microseconds for owner in result.owners) == result.recorded_microseconds
    )
    assert sum(day.recorded_microseconds for day in result.days) == result.recorded_microseconds
    own = reduce((first, second, third), owner=OWNER)
    assert own.recorded_microseconds == 90000000 and own.gpu_count == 2
    assert str(other) not in own.model_dump_json()
    assert own.total_usage_microseconds is None and not own.quota_usage_available


def test_reversals_are_subtracted_but_fixed_old_prefix_stays_reproducible():
    first = measured()
    adjustment = reversed_measurement(first)
    old = reduce((first,))
    assert reduce((first, adjustment)).recorded_microseconds == 0
    assert reduce((first,), query=selection(snapshot=old.snapshot)).fingerprint == old.fingerprint
    assert reduce(()).source_coverage == "unknown"
    assert reduce(()).total_usage_microseconds is None


def full_day_entry(start, end):
    template = measured()
    measurement = template.details.measurement.model_copy(
        update={
            "start_at": start,
            "end_at": end,
            "observed_microseconds": (end - start) // timedelta(microseconds=1),
        }
    )
    details = PostedMeasurement(
        measurement=measurement,
        configuration=template.details.configuration.model_copy(
            update={"recording_start_at": start}
        ),
    )
    return make_ledger_entry(
        **template.model_dump(
            exclude={
                "details",
                "recorded_at",
                "fingerprint",
                "assessment_scope",
                "quota_usage_available",
            }
        ),
        details=details,
        recorded_at=end + timedelta(seconds=1),
    )


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)])
def test_calendar_summaries_count_elapsed_utc_time_on_dst_days(day, hours):
    bounds = calendar_boundaries(day, day + timedelta(days=1), "America/New_York")
    entry = full_day_entry(bounds[0], bounds[-1])
    query = selection(start_at=bounds[0], end_at=bounds[-1], timezone="America/New_York")
    result = usage_history(
        query,
        owner_id=None,
        head=head((entry,)),
        entries=(entry,),
        read_at=bounds[-1] + timedelta(seconds=2),
    )
    assert [item.recorded_microseconds for item in result.days] == [
        hours * 3600000000,
        24 * 3600000000,
    ]
    assert result.recorded_microseconds == (hours + 24) * 3600000000


def test_midnight_clip_assigns_each_microsecond_to_exactly_one_local_day():
    zone = ZoneInfo("Asia/Shanghai")
    midnight = datetime(2026, 11, 18, tzinfo=zone).astimezone(UTC)
    entry = full_day_entry(midnight - timedelta(seconds=5), midnight + timedelta(seconds=7))
    result = usage_history(
        selection(
            start_at=midnight - timedelta(seconds=2),
            end_at=midnight + timedelta(seconds=3),
            timezone="Asia/Shanghai",
        ),
        owner_id=None,
        head=head((entry,)),
        entries=(entry,),
        read_at=midnight + timedelta(seconds=10),
    )
    assert [item.recorded_microseconds for item in result.days] == [2000000, 3000000]
    assert result.segment_count == 1


def input_query(**changes):
    return UsageHistoryInput.model_validate(
        {
            "node_id": "node-a",
            "mode": "node",
            "window": {
                "kind": "utc",
                "start_at": BASE.isoformat(),
                "end_at": (BASE + timedelta(seconds=120)).isoformat(),
                "timezone": "UTC",
            },
            **changes,
        }
    )


def test_calendar_and_page_inputs_reject_ambiguous_or_unbounded_requests():
    for changes in (
        {"offset": 1},
        {"actor_id": str(OWNER)},
        {"mode": "mine", "owner_id": str(OWNER)},
        {
            "window": {
                "kind": "calendar",
                "start_date": "2026-01-01",
                "end_date": "2027-01-02",
                "timezone": "UTC",
            }
        },
        {
            "window": {
                "kind": "calendar",
                "start_date": "2026-11-18",
                "end_date": "2026-11-17",
                "timezone": "UTC",
            }
        },
        {
            "window": {
                "kind": "utc",
                "start_at": BASE.isoformat(),
                "end_at": BASE.isoformat(),
                "timezone": "UTC",
            },
            "limit": 101,
        },
    ):
        with pytest.raises(ValidationError):
            input_query(**changes)


def surface_for(readings, settings):
    state, (original, actor, _, ids), _ = readings
    history = IndexedUsageHistoryService(original.sessions, clock=original.clock)
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
    return UsageHistorySurface(history, auth, ("node-a", "node-b")), actor, ids


def test_authenticated_page_pagination_and_csv_keep_one_snapshot_during_new_posts(
    readings, settings
):
    state, _, _ = readings
    surface, actor, _ = surface_for(readings, settings)
    state[0].post(POST, post_window(state, start=40, seq=101))
    before = counts(state)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface)
    ) as client:
        usage_page = client.get("/internal/usage/ui")
        assert usage_page.status_code == 200
        assert 'id="portal-return"' not in usage_page.text
        assert 'data-member-sessions="true"' not in usage_page.text
        headers = {"Authorization": "Bearer " + TOKEN}
        info = client.post("/internal/usage/session", headers=headers).json()
        assert info["actor"]["user_id"] == str(actor.actor_id)
        assert info["nodes"] == [{"node_id": "node-a", "mine": True, "node": True}]
        first = client.post(
            "/internal/usage/query",
            headers=headers,
            json=input_query(limit=1).model_dump(mode="json"),
        )
        assert first.status_code == 200
        page = first.json()
        assert page["report"]["recorded_microseconds"] == 60000000
        assert page["has_more"] and len(page["report"]["rows"]) == 1
        assert counts(state) == before
        fixed = input_query(limit=1, offset=1, snapshot=page["report"]["snapshot"]).model_dump(
            mode="json"
        )
        state[0].post(POST, post_window(state, start=80, seq=201))
        next_page = client.post("/internal/usage/query", headers=headers, json=fixed).json()
        assert next_page["report"]["fingerprint"] == page["report"]["fingerprint"]
        assert not next_page["has_more"]
        assert next_page["report"]["rows"][0]["entry_id"] != page["report"]["rows"][0]["entry_id"]
        exported = client.post("/internal/usage/export", headers=headers, json=fixed)
        assert exported.status_code == 200 and exported.content.startswith(b"\xef\xbb\xbf")
        records = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert (
            len(records) == 2
            and sum(int(row["recorded_microseconds"]) for row in records) == 60000000
        )
        assert {row["report_fingerprint"] for row in records} == {page["report"]["fingerprint"]}
        human_report = client.post("/internal/usage/report", headers=headers, json=fixed)
        assert human_report.status_code == 200
        assert page["report"]["fingerprint"] in human_report.text
        assert "0.017" in human_report.text
        assert "GPU 汇总" in human_report.text
        assert "attachment" in human_report.headers["content-disposition"]
        latest = client.post(
            "/internal/usage/query", headers=headers, json=input_query().model_dump(mode="json")
        ).json()
        assert latest["report"]["recorded_microseconds"] == 90000000
        assert "no-store" in first.headers["cache-control"]


def test_read_own_does_not_leak_other_users_or_bypass_node_permission(readings, settings):
    surface, actor, ids = surface_for(readings, settings)
    with surface.history.sessions.begin() as db:
        db.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
    own = surface.read("Bearer " + TOKEN, "fixture-history", input_query(mode="mine"))
    assert own.report.owner_id == actor.actor_id
    for query in (input_query(), input_query(node_id="node-b", mode="mine")):
        with pytest.raises(DomainError) as denied:
            surface.read("Bearer " + TOKEN, "fixture-history", query)
        assert denied.value.code == "PERMISSION_DENIED"
    for credential in (
        None,
        "Bearer " + settings.read_token.get_secret_value(),
        "Bearer " + settings.agent_credentials["agent-a"].token.get_secret_value(),
    ):
        with pytest.raises(DomainError) as denied:
            surface.session(credential)
        assert denied.value.status == 401


def test_display_names_are_current_labels_and_csv_text_cannot_become_a_formula(readings, settings):
    surface, actor, _ = surface_for(readings, settings)
    first = surface.read("Bearer " + TOKEN, "fixture-history", input_query())
    with surface.history.sessions.begin() as db:
        db.execute(
            update(User).where(User.id == str(actor.actor_id)).values(display_name=" =SUM(1,2)")
        )
    second = surface.read("Bearer " + TOKEN, "fixture-history", input_query())
    assert second.report.fingerprint == first.report.fingerprint
    records = list(
        csv.DictReader(io.StringIO(history_csv(second.report, second.labels).decode("utf-8-sig")))
    )
    assert records[0]["display_name"] == "' =SUM(1,2)"


def test_calendar_request_clips_today_but_invalid_dates_and_unfixed_export_are_rejected(
    readings, settings
):
    surface, _, _ = surface_for(readings, settings)
    result = surface.read(
        "Bearer " + TOKEN,
        "fixture-history",
        input_query(
            window={
                "kind": "calendar",
                "start_date": "2026-11-16",
                "end_date": surface.history._now().date().isoformat(),
                "timezone": "UTC",
            }
        ),
    )
    assert result.report.end_at == surface.history._now()
    with pytest.raises(DomainError) as error:
        surface.read("Bearer " + TOKEN, "fixture-history", input_query(), output="csv")
    assert error.value.code == "VALIDATION_ERROR"


def test_default_app_has_no_usage_entry(settings):
    with TestClient(create_app(settings, SlackSettings(_env_file=None))) as client:
        assert client.get("/internal/usage/ui").status_code == 404


def test_postgresql_history_is_select_only_and_rechecks_current_permissions(
    readings, settings, monkeypatch
):
    state, _, _ = readings
    postgres(state)
    surface, actor, ids = surface_for(readings, settings)
    statements = []
    engine = surface.history.sessions.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with surface.history.sessions() as db:
            db.execute(text("SET TRANSACTION READ ONLY"))
            report = surface.history.read_in_transaction(db, actor, selection(), mode="node")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert report.recorded_microseconds == 30000000
    assert len(statements) <= 20
    assert all(sql.startswith(("SELECT", "SET TRANSACTION READ ONLY")) for sql in statements)
    from aris.interfaces.usage import service as module

    original = module.history_csv

    def revoke(report, labels):
        with surface.history.sessions.begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))
        return original(report, labels)

    monkeypatch.setattr(module, "history_csv", revoke)
    first = surface.read("Bearer " + TOKEN, "fixture-history", input_query())
    with pytest.raises(DomainError) as denied:
        surface.read(
            "Bearer " + TOKEN,
            "fixture-history",
            input_query(snapshot=first.report.snapshot),
            output="csv",
        )
    assert denied.value.code == "PERMISSION_DENIED"
