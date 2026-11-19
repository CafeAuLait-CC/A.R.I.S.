from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aris.core.access.schemas import AccessScope
from aris.modules.nova.report_calendar import (
    WeeklyReportPlan,
    due_report_weeks,
    weekly_report_period,
)


def plan(**changes):
    return WeeklyReportPlan(
        **{
            "namespace": "lab-reports",
            "report_type": "recorded-node-weekly",
            "owner_id": uuid4(),
            "scope": AccessScope(type="node", id="node-a"),
            "timezone": "America/New_York",
            "first_week": date(2026, 3, 2),
            **changes,
        }
    )


@pytest.mark.parametrize(
    "monday,hours", [(date(2026, 3, 2), 167), (date(2026, 10, 26), 169), (date(2026, 11, 2), 168)]
)
def test_natural_monday_week_uses_actual_utc_elapsed_time(monday, hours):
    value = weekly_report_period(plan(), monday)
    assert value.end_at - value.start_at == timedelta(hours=hours)
    assert value.week == monday and value.start_at.tzinfo == UTC


def test_period_key_is_stable_and_changed_scope_owner_or_kind_is_distinct():
    cfg = plan()
    first = weekly_report_period(cfg, cfg.first_week)
    assert weekly_report_period(cfg, cfg.first_week) == first
    changed = cfg.model_copy(update={"timezone": "Europe/London"})
    other = weekly_report_period(changed, changed.first_week)
    assert other.id == first.id and other.key == first.key and other.start_at != first.start_at
    for field, value in (
        ("scope", AccessScope(type="node", id="node-b")),
        ("owner_id", uuid4()),
        ("report_type", "recorded-personal-weekly"),
    ):
        assert (
            weekly_report_period(cfg.model_copy(update={field: value}), cfg.first_week).id
            != first.id
        )


def test_only_completed_due_weeks_are_emitted_and_catchup_is_bounded():
    cfg = plan(delay_minutes=30)
    first = weekly_report_period(cfg, cfg.first_week)
    early = due_report_weeks(cfg, now=first.scheduled_for - timedelta(microseconds=1))
    assert early.periods == () and early.caught_up and early.next_week == cfg.first_week
    exact = due_report_weeks(cfg, now=first.scheduled_for)
    assert exact.periods == (first,) and exact.next_week == date(2026, 3, 9)
    later = datetime(2026, 4, 1, tzinfo=UTC)
    batch = due_report_weeks(cfg, now=later, limit=2)
    assert len(batch.periods) == 2 and not batch.caught_up
    following = due_report_weeks(cfg, now=later, from_week=batch.next_week, limit=2)
    assert len(following.periods) == 2 and following.caught_up
    assert len({value.id for value in (*batch.periods, *following.periods)}) == 4


def test_calendar_input_rejects_wrong_week_invalid_zone_or_naive_clock():
    with pytest.raises(ValidationError):
        plan(first_week=date(2026, 3, 3))
    with pytest.raises(ValidationError):
        plan(timezone="Missing/Zone")
    with pytest.raises(ValueError):
        weekly_report_period(plan(), date(2026, 3, 3))
    with pytest.raises(ValueError):
        due_report_weeks(plan(), now=datetime(2026, 4, 1))
    with pytest.raises(ValueError):
        due_report_weeks(plan(), now=datetime(2026, 4, 1, tzinfo=UTC), limit=0)
