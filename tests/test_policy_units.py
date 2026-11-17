from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from zoneinfo import ZoneInfo

import pytest

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import GpuTimeWeights
from aris.modules.nova.policy_units import policy_quota_window, weighted_gpu_seconds
from tests.test_policy_source import parse, policy


def spec(zone="America/Los_Angeles", start="monday", rolling=False):
    source = policy(timezone=zone)
    source["spec"]["quota"]["window"] = (
        {"kind": "rolling_7d"} if rolling else {"kind": "calendar_week", "week_starts_on": start}
    )
    return parse(source).spec


@pytest.mark.parametrize(
    "at,start,end,hours",
    [
        (
            "2020-03-08T12:00:00+00:00",
            "2020-03-02T08:00:00+00:00",
            "2020-03-09T07:00:00+00:00",
            167,
        ),
        (
            "2020-11-01T12:00:00+00:00",
            "2020-10-26T07:00:00+00:00",
            "2020-11-02T08:00:00+00:00",
            169,
        ),
        (
            "2020-07-08T12:00:00+00:00",
            "2020-07-06T07:00:00+00:00",
            "2020-07-13T07:00:00+00:00",
            168,
        ),
    ],
)
def test_calendar_weeks_use_local_boundaries_and_actual_utc_elapsed_time(at, start, end, hours):
    result = policy_quota_window(spec(), datetime.fromisoformat(at))
    assert result.start_at == datetime.fromisoformat(
        start
    ) and result.end_at == datetime.fromisoformat(end)
    assert result.end_at - result.start_at == timedelta(hours=hours)
    assert result.start_at.tzinfo is UTC and result.end_at.tzinfo is UTC


def test_monday_boundary_belongs_to_new_half_open_week_and_offsets_are_equivalent():
    instant = datetime(2020, 11, 2, 8, tzinfo=UTC)
    result = policy_quota_window(spec(), instant)
    assert result.start_at == instant
    assert policy_quota_window(spec(), instant.astimezone(timezone(timedelta(hours=9)))) == result
    before = policy_quota_window(spec(), instant - timedelta(microseconds=1))
    assert before.end_at == result.start_at


def test_weekday_selection_and_non_integer_hour_timezone_are_explicit():
    now = datetime(2020, 7, 8, 12, tzinfo=UTC)
    zone = ZoneInfo("Asia/Kathmandu")
    result = policy_quota_window(spec("Asia/Kathmandu", "wednesday"), now)
    assert result.start_at.astimezone(zone) == datetime(2020, 7, 8, 0, tzinfo=zone)
    assert result.start_at == datetime(2020, 7, 7, 18, 15, tzinfo=UTC)


def test_rolling_week_is_exactly_seven_utc_days_even_across_dst():
    at = datetime(2020, 11, 2, 8, tzinfo=UTC)
    result = policy_quota_window(spec(rolling=True), at)
    assert result.end_at == at and result.start_at == at - timedelta(days=7)


@pytest.mark.parametrize(
    "zone,at,start",
    [
        ("Pacific/Apia", datetime(2011, 12, 31, 12, tzinfo=ZoneInfo("Pacific/Apia")), "friday"),
        ("America/Havana", datetime(2020, 11, 2, 12, tzinfo=ZoneInfo("America/Havana")), "sunday"),
    ],
)
def test_nonexistent_or_ambiguous_civil_midnight_remains_not_evaluated(zone, at, start):
    with pytest.raises(DomainError) as error:
        policy_quota_window(spec(zone, start), at)
    assert error.value.code == "NOT_EVALUATED"


def test_naive_time_is_rejected_instead_of_assuming_server_timezone():
    with pytest.raises(DomainError) as error:
        policy_quota_window(spec(), datetime(2020, 7, 8, 12))
    assert error.value.code == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "at,rolling",
    [
        (datetime(1, 1, 1, tzinfo=UTC), True),
        (datetime(9999, 12, 31, tzinfo=UTC), False),
        (datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=14))), False),
    ],
)
def test_unrepresentable_time_windows_remain_not_evaluated(at, rolling):
    with pytest.raises(DomainError) as error:
        policy_quota_window(spec(rolling=rolling), at)
    assert error.value.code == "NOT_EVALUATED"


def rates(default=(1, 1), models=()):
    return GpuTimeWeights.model_validate(
        {
            "default_weight": {"numerator": default[0], "denominator": default[1]},
            "model_weights": [
                {"model": model, "weight": {"numerator": num, "denominator": den}}
                for model, num, den in models
            ],
        }
    )


def test_uniform_and_model_time_conversion_preserve_subsecond_precision():
    assert weighted_gpu_seconds(3600000000, "Fixture GPU", rates()) == Fraction(3600)
    assert weighted_gpu_seconds(
        1000000, "Fixture GPU", rates(models=(("Fixture GPU", 3, 2),))
    ) == Fraction(3, 2)
    assert sum(weighted_gpu_seconds(1, None, rates()) for _ in range(7)) == Fraction(7, 1000000)
    assert weighted_gpu_seconds(0, None, rates()) == 0


def test_explicit_default_rate_applies_to_known_unlisted_model_but_missing_model_cannot_guess():
    weights = rates(default=(2, 1), models=(("Fixture expensive GPU", 3, 1),))
    assert weighted_gpu_seconds(1000000, "Fixture regular GPU", weights) == 2
    with pytest.raises(DomainError) as error:
        weighted_gpu_seconds(1000000, None, weights)
    assert error.value.code == "NOT_EVALUATED"


@pytest.mark.parametrize("duration", [True, -1, 0.5, "1000000", 366 * 86400 * 1000000 + 1])
def test_time_converter_rejects_bool_coercion_negative_and_unbounded_duration(duration):
    with pytest.raises(DomainError) as error:
        weighted_gpu_seconds(duration, None, rates())
    assert error.value.code == "VALIDATION_ERROR"
