"""Verified short retention gaps are distinct from original process residency."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import AwareDatetime, Field, model_validator

from aris.modules.lyra.continuous_usage import UsagePresentationGap
from aris.modules.lyra.ledger_schemas import canonical_ledger, ledger_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.usage_schemas import GpuUsageFact
from aris.modules.vega.usage_span import UsageSourceSpan


class SessionOccupancyRule(IntervalContract):
    maximum_interruption_seconds: Literal[60] = 60
    maximum_observation_gap_seconds: Literal[45] = 45
    basis: Literal["verified_short_session_retention"] = "verified_short_session_retention"
    quota_usage_available: Literal[False] = False

    @property
    def fingerprint(self) -> str:
        return ledger_fingerprint("session_occupancy_rule", canonical_ledger(self))


GapState = Literal["retained", "excluded", "unknown"]


@dataclass(frozen=True)
class GapSourceAssessment:
    state: GapState
    reason: str
    uid: int | None = None
    username: str | None = None


class SessionGapAssessment(IntervalContract):
    interval_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    before_entry_id: str
    after_entry_id: str
    duration_microseconds: int = Field(gt=0, le=60000000)
    state: GapState
    reason: str
    source_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    identity_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def retained_requires_proof(self) -> "SessionGapAssessment":
        if self.state == "retained" and (
            self.source_fingerprint is None or self.identity_fingerprint is None
        ):
            raise ValueError("retained time requires source and historical identity evidence")
        return self


def assess_gap_sources(gap: UsagePresentationGap, span: UsageSourceSpan) -> GapSourceAssessment:
    """No current identity or GPU state is accepted as evidence for the gap."""
    start, end = gap.before.end_at, gap.after.start_at
    rule = SessionOccupancyRule()

    def unknown(reason: str) -> GapSourceAssessment:
        return GapSourceAssessment("unknown", reason)

    if (
        not timedelta(0) < end - start <= timedelta(seconds=rule.maximum_interruption_seconds)
        or gap.before.owner_id != gap.after.owner_id
        or (gap.before.gpu_uuid, gap.before.model) != (gap.after.gpu_uuid, gap.after.model)
    ):
        return unknown("GAP_BINDING_DIFFERS")
    if not span.healthy_heartbeats:
        return unknown("COLLECTOR_UNHEALTHY")
    if any(
        b.observed_at < a.observed_at
        for a, b in zip(span.references, span.references[1:], strict=False)
    ):
        return unknown("SOURCE_CLOCK_REGRESSION")
    frames: list[tuple[datetime, GpuUsageFact, dict[str, Any]]] = []
    for fact in span.observations:
        if not start <= fact.compute_observed_at <= end:
            return unknown("SOURCE_TIME_OUTSIDE_GAP")
        gpu = next((g for g in fact.gpus if g.gpu_uuid == gap.before.gpu_uuid), None)
        if (
            fact.node_id != span.query.node_id
            or fact.gpu_quality != "ok"
            or fact.compute_quality != "ok"
            or not fact.compute_counts_consistent
            or abs(fact.compute_observed_at - fact.gpu_observed_at) > timedelta(seconds=45)
            or gpu is None
            or not gpu.inventory_present
            or gpu.model != gap.before.model
            or gpu.ownership_quality != "complete"
            or gpu.unattributed_processes != 0
        ):
            return unknown("SOURCE_QUALITY_UNKNOWN")
        signature = gpu.model_dump(mode="json")
        if frames and fact.compute_observed_at == frames[-1][0]:
            if signature != frames[-1][2]:
                return unknown("CONFLICTING_CACHED_OBSERVATION")
            continue
        if frames and not timedelta(0) < fact.compute_observed_at - frames[-1][0] <= timedelta(
            seconds=45
        ):
            return unknown("OBSERVATION_GAP_OR_CLOCK_REGRESSION")
        frames.append((fact.compute_observed_at, gpu, signature))
    if not frames or (frames[0][0], frames[-1][0]) != (start, end):
        return unknown("EXACT_ENDPOINTS_MISSING")
    first, last = frames[0][1], frames[-1][1]
    if len(first.owners) != 1 or len(last.owners) != 1:
        return unknown("ENDPOINT_OWNER_UNKNOWN")
    owner = first.owners[0]
    if (owner.uid, owner.username) != (last.owners[0].uid, last.owners[0].username):
        return GapSourceAssessment("excluded", "ENDPOINT_OWNER_CHANGED")
    for _, gpu, _ in frames:
        if any((o.uid, o.username) != (owner.uid, owner.username) for o in gpu.owners):
            return GapSourceAssessment("excluded", "OTHER_OWNER_OBSERVED")
    if not any(g.reported_processes == 0 for _, g, _ in frames[1:-1]):
        return unknown("NO_IDLE_SAMPLE_REQUIRES_REVIEW")
    return GapSourceAssessment("retained", "VERIFIED_SHORT_IDLE_GAP", owner.uid, owner.username)


class SessionOccupancySummary(IntervalContract):
    assessment_scope: Literal["recorded_sessions_only"] = "recorded_sessions_only"
    rule: SessionOccupancyRule = Field(default_factory=SessionOccupancyRule)
    as_of: AwareDatetime
    identity_revision: int | None = Field(default=None, ge=1, le=9223372036854775807)
    history_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_recorded_microseconds: int = Field(ge=0)
    retained_microseconds: int = Field(ge=0)
    excluded_microseconds: int = Field(ge=0)
    unverified_microseconds: int = Field(ge=0)
    verified_session_microseconds: int = Field(ge=0)
    session_microseconds: int | None = Field(default=None, ge=0)
    gaps: tuple[SessionGapAssessment, ...]
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    quota_usage_available: Literal[False] = False

    @model_validator(mode="after")
    def honest_totals(self) -> "SessionOccupancySummary":
        if (
            self.verified_session_microseconds
            != self.original_recorded_microseconds + self.retained_microseconds
        ):
            raise ValueError("session retention must remain separate from process residency")
        for state, total in (
            ("retained", self.retained_microseconds),
            ("excluded", self.excluded_microseconds),
            ("unknown", self.unverified_microseconds),
        ):
            if sum(g.duration_microseconds for g in self.gaps if g.state == state) != total:
                raise ValueError("session gap totals differ from evidence")
        expected = self.verified_session_microseconds if self.unverified_microseconds == 0 else None
        if self.session_microseconds != expected:
            raise ValueError("unverified gaps must not claim a complete session total")
        if self.fingerprint != session_fingerprint(self):
            raise ValueError("session occupancy fingerprint differs")
        return self


def session_fingerprint(value: SessionOccupancySummary) -> str:
    return ledger_fingerprint("session_occupancy", canonical_ledger(value, exclude={"fingerprint"}))
