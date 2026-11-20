"""Bounded stable-prefix layout; every planned window still needs original verification."""

import json
from typing import Literal

from pydantic import Field, model_validator

from aris.modules.lyra.residency_observed_window_schemas import (
    ResidencyObservedWindowReference,
    VerifiedResidencyObservedWindow,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract

_MAXIMUM = 9223372036854775807


class StableResidencyLayoutMethod(IntervalContract):
    method: Literal["stable_prefix_reserved_tail"] = "stable_prefix_reserved_tail"
    max_intervals: int = Field(ge=1, le=128, strict=True)

    @property
    def fingerprint(self) -> str:
        return trace_fingerprint("stable_residency_layout_method", canonical_trace(self))


def residency_layout_counts(
    observed_intervals: int, state: Literal["open", "closed"], capacity: int
) -> tuple[int, int, int]:
    """Return stable prefix count, total planned windows and still-unplanned tail size."""
    if (
        type(observed_intervals) is not int
        or not 1 <= observed_intervals <= _MAXIMUM
        or state not in ("open", "closed")
        or type(capacity) is not int
        or not 1 <= capacity <= 128
    ):
        raise ValueError("layout requires a fixed observed prefix and bounded capacity")
    prefix = max(observed_intervals // capacity - 1, 0)
    tail = observed_intervals - prefix * capacity
    if state == "open":
        return prefix, prefix, tail
    return prefix, prefix + (tail + capacity - 1) // capacity, 0


class PlannedResidencyWindow(IntervalContract):
    assessment_scope: Literal["planned_original_residency_window"] = (
        "planned_original_residency_window"
    )
    formal_usage_available: Literal[False] = False
    index: int = Field(ge=0, le=_MAXIMUM - 1, strict=True)
    kind: Literal["stable_prefix", "final_tail"]
    key: str = Field(pattern=r"^[a-f0-9]{64}$")
    original: ResidencyObservedWindowReference


class ResidencyLayoutPage(IntervalContract):
    assessment_scope: Literal["fixed_observed_residency_layout"] = "fixed_observed_residency_layout"
    formal_usage_available: Literal[False] = False
    quota_usage_available: Literal[False] = False
    platform_owner_id: None = None
    time_window_coverage: Literal["not_assessed"] = "not_assessed"
    consumer_id: str = Field(min_length=1, max_length=128)
    witness: ResidencyObservedWindowReference
    witness_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    state_at_record: Literal["open", "closed"]
    observed_intervals: int = Field(ge=1, le=_MAXIMUM, strict=True)
    method: StableResidencyLayoutMethod
    layout_key: str = Field(pattern=r"^[a-f0-9]{64}$")
    stable_prefix_windows: int = Field(ge=0, le=_MAXIMUM, strict=True)
    total_planned_windows: int = Field(ge=0, le=_MAXIMUM, strict=True)
    unplanned_tail_intervals: int = Field(ge=0, le=255, strict=True)
    first_window: int = Field(ge=0, le=_MAXIMUM, strict=True)
    limit: int = Field(ge=1, le=8, strict=True)
    windows: tuple[PlannedResidencyWindow, ...] = Field(max_length=8)
    next_window: int | None = Field(default=None, ge=1, le=_MAXIMUM, strict=True)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def fixed_page(self) -> "ResidencyLayoutPage":
        prefix, total, tail = residency_layout_counts(
            self.observed_intervals, self.state_at_record, self.method.max_intervals
        )
        end = min(self.first_window + self.limit, total)
        if (
            (self.stable_prefix_windows, self.total_planned_windows, self.unplanned_tail_intervals)
            != (prefix, total, tail)
            or self.witness.through_interval > self.observed_intervals
            or self.first_window > total
            or self.layout_key != _layout_key(self.consumer_id, self.witness, self.method)
            or len(self.windows) != end - self.first_window
            or self.next_window != (end if end < total else None)
            or any(
                window != _window(self, index)
                for index, window in enumerate(self.windows, start=self.first_window)
            )
            or self.fingerprint != _page_fingerprint(self)
        ):
            raise ValueError("residency layout differs from its fixed prefix and method")
        return self


def _layout_key(
    consumer_id: str,
    witness: ResidencyObservedWindowReference,
    method: StableResidencyLayoutMethod,
) -> str:
    return trace_fingerprint(
        "stable_residency_layout",
        json.dumps(
            {
                "consumer_id": consumer_id,
                "trace_configuration": witness.expected_trace_configuration_fingerprint,
                "run_id": witness.reference.run_id,
                "method": method.fingerprint,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _window(page: ResidencyLayoutPage, index: int) -> PlannedResidencyWindow:
    capacity, prefix = page.method.max_intervals, page.stable_prefix_windows
    if index < prefix:
        after, through, kind = index * capacity, (index + 1) * capacity, "stable_prefix"
    else:
        start = prefix * capacity
        remaining, parts = page.observed_intervals - start, page.total_planned_windows - prefix
        local = index - prefix
        after = start + local * remaining // parts
        through = start + (local + 1) * remaining // parts
        kind = "final_tail"
    reference = ResidencyObservedWindowReference.model_validate(
        {**page.witness.model_dump(), "after_interval": after, "through_interval": through}
    )
    return PlannedResidencyWindow(
        index=index,
        kind=kind,
        key=trace_fingerprint(
            "stable_residency_layout_window",
            json.dumps(
                {"layout_key": page.layout_key, "after": after, "through": through},
                sort_keys=True,
                separators=(",", ":"),
            ),
        ),
        original=reference,
    )


def _page_fingerprint(page: ResidencyLayoutPage) -> str:
    return trace_fingerprint(
        "stable_residency_layout_page", canonical_trace(page, exclude={"fingerprint"})
    )


def plan_residency_windows(
    observed: VerifiedResidencyObservedWindow,
    method: StableResidencyLayoutMethod,
    *,
    first_window: int = 0,
    limit: int = 8,
) -> ResidencyLayoutPage:
    """Plan from an already verified fixed record; never verify or post unseen windows."""
    observed = VerifiedResidencyObservedWindow.model_validate(observed.model_dump(mode="json"))
    method = StableResidencyLayoutMethod.model_validate(method.model_dump(mode="json"))
    if (
        type(first_window) is not int
        or first_window < 0
        or type(limit) is not int
        or not 1 <= limit <= 8
    ):
        raise ValueError("layout pages require an explicit bounded offset and limit")
    prefix, total, tail = residency_layout_counts(
        observed.parent_run.intervals, observed.state_at_record, method.max_intervals
    )
    if first_window > total:
        raise ValueError("layout page starts beyond this fixed prefix")
    end = min(first_window + limit, total)
    draft = ResidencyLayoutPage.model_construct(
        consumer_id=observed.consumer_id,
        witness=observed.request,
        witness_fingerprint=observed.fingerprint,
        state_at_record=observed.state_at_record,
        observed_intervals=observed.parent_run.intervals,
        method=method,
        layout_key=_layout_key(observed.consumer_id, observed.request, method),
        stable_prefix_windows=prefix,
        total_planned_windows=total,
        unplanned_tail_intervals=tail,
        first_window=first_window,
        limit=limit,
        windows=(),
        next_window=end if end < total else None,
        fingerprint="0" * 64,
    )
    draft = draft.model_copy(
        update={"windows": tuple(_window(draft, index) for index in range(first_window, end))}
    )
    return ResidencyLayoutPage.model_validate(
        {**draft.model_dump(), "fingerprint": _page_fingerprint(draft)}
    )
