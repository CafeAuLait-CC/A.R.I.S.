"""Pure time/window arithmetic; never a quota approval or a ledger write."""

from datetime import UTC, date, datetime, time, timedelta
from fractions import Fraction
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ConfigDict

from aris.core.contracts import DomainError
from aris.modules.nova.policy_schemas import YEAR_SECONDS, GpuTimeWeights, PolicySpec
from aris.modules.nova.schemas import ReservationWindow

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class PolicyTimeWindow(ReservationWindow):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _unique_midnight(day: date, zone: ZoneInfo) -> datetime:
    wall = datetime.combine(day, time.min)
    candidates = set()
    for fold in (0, 1):
        local = wall.replace(tzinfo=zone, fold=fold)
        candidate = local.astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == wall:
            candidates.add(candidate)
    if len(candidates) != 1:
        raise DomainError(
            "NOT_EVALUATED", "Calendar week boundary is ambiguous or unavailable", 503
        )
    return candidates.pop()


def policy_quota_window(spec: PolicySpec, as_of: datetime) -> PolicyTimeWindow:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise DomainError("VALIDATION_ERROR", "Policy window time requires a timezone", 422)
    try:
        now = as_of.astimezone(UTC)
        if spec.quota.window.kind == "rolling_7d":
            return PolicyTimeWindow(start_at=now - timedelta(days=7), end_at=now)
        zone = ZoneInfo(spec.timezone)
        day = now.astimezone(zone).date()
        offset = (day.weekday() - WEEKDAYS.index(spec.quota.window.week_starts_on)) % 7
        first = day - timedelta(days=offset)
        start, end = (
            _unique_midnight(first, zone),
            _unique_midnight(first + timedelta(days=7), zone),
        )
        result = PolicyTimeWindow(start_at=start, end_at=end)
        if not result.start_at <= now < result.end_at:
            raise DomainError(
                "NOT_EVALUATED", "Calendar week does not contain the requested time", 503
            )
        return result
    except (OverflowError, ValueError, ZoneInfoNotFoundError):
        raise DomainError("NOT_EVALUATED", "Policy time window is unavailable", 503) from None


def weighted_gpu_seconds(
    observed_microseconds: int, model: str | None, weights: GpuTimeWeights
) -> Fraction:
    if (
        type(observed_microseconds) is not int
        or not 0 <= observed_microseconds <= YEAR_SECONDS * 1000000
    ):
        raise DomainError(
            "VALIDATION_ERROR", "Observed duration must be bounded integer microseconds", 422
        )
    if model is not None and (not model or len(model) > 256 or model != model.strip()):
        raise DomainError("VALIDATION_ERROR", "GPU model label is invalid", 422)
    validated = GpuTimeWeights.model_validate(weights.model_dump(mode="json"))
    if model is None and validated.model_weights:
        raise DomainError("NOT_EVALUATED", "GPU model is required for configured model rates", 503)
    weight = next(
        (item.weight for item in validated.model_weights if item.model == model),
        validated.default_weight,
    )
    return Fraction(observed_microseconds * weight.numerator, 1000000 * weight.denominator)
