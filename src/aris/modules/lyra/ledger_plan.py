"""Comparison only: a posting writer must obtain and enforce its own database authority."""

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.modules.lyra.derivation_schemas import StoredUsageDerivation
from aris.modules.lyra.usage_intervals import IntervalContract


def _micros(start: datetime, end: datetime) -> int:
    delta = end - start
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


class MeasuredWindow(IntervalContract):
    node_id: str = Field(min_length=1, max_length=128)
    gpu_uuid: str = Field(min_length=1, max_length=128)
    owner_id: UUID
    model: str = Field(min_length=1, max_length=256)
    start_at: AwareDatetime
    end_at: AwareDatetime
    observed_microseconds: int = Field(gt=0, strict=True)
    timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("start_at", "end_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def exact_observed_window(self) -> "MeasuredWindow":
        if self.end_at <= self.start_at or self.observed_microseconds != _micros(
            self.start_at, self.end_at
        ):
            raise ValueError("measurement must retain its exact positive observed window")
        return self


class ActiveUsageMeasurement(MeasuredWindow):
    id: UUID


class UsageMeasurementProposal(MeasuredWindow):
    source_derivation_id: UUID
    source_result_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_session_index: int = Field(ge=0, lt=128, strict=True)
    identity_revision: int = Field(ge=1, le=9223372036854775807, strict=True)
    basis: Literal["compute_process_residency"] = "compute_process_residency"
    end_basis: Literal["last_source_observation"] = "last_source_observation"
    weighted: Literal[False] = False


class LedgerCoverage(IntervalContract):
    """An explicit comparison input, never a caller-controlled write or quota ticket."""

    node_id: str = Field(min_length=1, max_length=128)
    gpu_uuids: tuple[str, ...] = Field(min_length=1, max_length=128)
    start_at: AwareDatetime
    end_at: AwareDatetime
    read_at: AwareDatetime
    state: Literal["complete", "partial", "unavailable"]
    records: tuple[ActiveUsageMeasurement, ...] = Field(max_length=1024)

    @field_validator("start_at", "end_at", "read_at")
    @classmethod
    def utc_times(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("gpu_uuids")
    @classmethod
    def bounded_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values) or any(not 1 <= len(item) <= 128 for item in values):
            raise ValueError("ledger coverage requires bounded unique GPU targets")
        return tuple(sorted(values))

    @field_validator("records")
    @classmethod
    def canonical_records(
        cls, values: tuple[ActiveUsageMeasurement, ...]
    ) -> tuple[ActiveUsageMeasurement, ...]:
        by_id: dict[UUID, ActiveUsageMeasurement] = {}
        for item in values:
            if item.id in by_id and by_id[item.id] != item:
                raise ValueError("ledger record ID has conflicting metadata")
            by_id[item.id] = item
        return tuple(sorted(by_id.values(), key=lambda item: str(item.id)))

    @model_validator(mode="after")
    def coherent_scope(self) -> "LedgerCoverage":
        if self.start_at >= self.end_at or any(
            item.node_id != self.node_id
            or item.gpu_uuid not in self.gpu_uuids
            or item.start_at >= self.end_at
            or item.end_at <= self.start_at
            for item in self.records
        ):
            raise ValueError("ledger coverage records differ from the selected scope")
        return self


class LedgerPlanningRules(IntervalContract):
    recording_start_at: AwareDatetime
    expected_timing_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("recording_start_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PostingOutcome(IntervalContract):
    session_index: int = Field(ge=0, lt=128, strict=True)
    state: Literal["new_candidate", "already_recorded", "needs_review", "unavailable"]
    reason: Literal[
        "UNWEIGHTED_MEASUREMENT_CANDIDATE",
        "EXACT_MEASUREMENT_ALREADY_RECORDED",
        "INSUFFICIENT_PROCESS_EVIDENCE",
        "IDENTITY_UNRESOLVED",
        "OUTSIDE_RECORDING_START",
        "TIMING_RULES_DIFFER",
        "RECORDING_START_NOT_REACHED",
        "DERIVATION_NOT_RECORDED_YET",
        "LEDGER_COVERAGE_UNAVAILABLE",
        "LEDGER_VIEW_NOT_REACHED",
        "LEDGER_VIEW_PREDATES_DERIVATION",
        "LEDGER_QUERY_DOES_NOT_COVER_INTERVAL",
        "EXISTING_MEASUREMENTS_OVERLAP",
        "INTERVAL_OVERLAP_REQUIRES_RECONCILIATION",
        "SOURCE_CANDIDATES_OVERLAP",
    ]
    candidate: UsageMeasurementProposal | None = Field(default=None, repr=False)
    existing_ids: tuple[UUID, ...] = Field(default=(), max_length=1024)


class _Evaluation(IntervalContract):
    at: AwareDatetime


def _proposal(source: StoredUsageDerivation, index: int) -> UsageMeasurementProposal:
    attribution = source.artifact.analysis.request.attribution
    candidate = attribution.residency.sessions[index]
    identity = attribution.attributions[index].identity
    assert identity.user_id is not None
    return UsageMeasurementProposal(
        node_id=attribution.residency.trace.node_id,
        gpu_uuid=candidate.gpu_uuid,
        owner_id=identity.user_id,
        model=candidate.model,
        start_at=candidate.evidence[0].previous.compute_observed_at,
        end_at=candidate.evidence[-1].current.compute_observed_at,
        observed_microseconds=candidate.observed_microseconds,
        timing_fingerprint=attribution.residency.rule_fingerprint,
        source_derivation_id=source.receipt.id,
        source_result_fingerprint=source.receipt.result_fingerprint,
        source_session_index=index,
        identity_revision=source.artifact.request.identity_revision,
    )


def _overlap(a: MeasuredWindow, b: MeasuredWindow) -> bool:
    return (
        a.node_id == b.node_id
        and a.gpu_uuid == b.gpu_uuid
        and a.start_at < b.end_at
        and b.start_at < a.end_at
    )


def _same_measurement(a: MeasuredWindow, b: MeasuredWindow) -> bool:
    return a.model_dump(include=set(MeasuredWindow.model_fields)) == b.model_dump(
        include=set(MeasuredWindow.model_fields)
    )


def _outcomes(
    source: StoredUsageDerivation,
    rules: LedgerPlanningRules,
    coverage: LedgerCoverage | None,
    evaluated_at: datetime,
) -> tuple[PostingOutcome, ...]:
    attributed = source.artifact.analysis.request.attribution
    proposals = {
        i: _proposal(source, i)
        for i, item in enumerate(attributed.attributions)
        if item.state == "attributed_candidate"
    }
    outcomes = []
    for index, item in enumerate(attributed.attributions):
        candidate = proposals.get(index)
        state, reason = "unavailable", "LEDGER_COVERAGE_UNAVAILABLE"
        existing_ids: tuple[UUID, ...] = ()
        if source.receipt.created_at > evaluated_at:
            reason = "DERIVATION_NOT_RECORDED_YET"
        elif item.state == "insufficient_evidence":
            state, reason = "needs_review", "INSUFFICIENT_PROCESS_EVIDENCE"
        elif item.identity.status != "resolved":
            state, reason = "needs_review", "IDENTITY_UNRESOLVED"
        else:
            assert candidate is not None
            if rules.recording_start_at > evaluated_at:
                reason = "RECORDING_START_NOT_REACHED"
            elif candidate.timing_fingerprint != rules.expected_timing_fingerprint:
                reason = "TIMING_RULES_DIFFER"
            elif candidate.start_at < rules.recording_start_at:
                state, reason = "needs_review", "OUTSIDE_RECORDING_START"
            elif any(i != index and _overlap(candidate, other) for i, other in proposals.items()):
                state, reason = "needs_review", "SOURCE_CANDIDATES_OVERLAP"
            elif coverage is None or coverage.state != "complete":
                pass
            elif coverage.read_at > evaluated_at:
                reason = "LEDGER_VIEW_NOT_REACHED"
            elif coverage.read_at < source.receipt.created_at:
                reason = "LEDGER_VIEW_PREDATES_DERIVATION"
            elif (
                coverage.node_id != candidate.node_id
                or candidate.gpu_uuid not in coverage.gpu_uuids
                or coverage.start_at > candidate.start_at
                or coverage.end_at < candidate.end_at
            ):
                reason = "LEDGER_QUERY_DOES_NOT_COVER_INTERVAL"
            else:
                records = tuple(
                    row for row in coverage.records if row.gpu_uuid == candidate.gpu_uuid
                )
                ordered = sorted(records, key=lambda row: (row.start_at, row.end_at, str(row.id)))
                intersecting = tuple(row for row in records if _overlap(candidate, row))
                existing_ids = tuple(row.id for row in intersecting)
                if any(_overlap(a, b) for a, b in zip(ordered, ordered[1:], strict=False)):
                    reason = "EXISTING_MEASUREMENTS_OVERLAP"
                elif not intersecting:
                    state, reason = "new_candidate", "UNWEIGHTED_MEASUREMENT_CANDIDATE"
                elif len(intersecting) == 1 and _same_measurement(candidate, intersecting[0]):
                    state, reason = "already_recorded", "EXACT_MEASUREMENT_ALREADY_RECORDED"
                else:
                    state, reason = "needs_review", "INTERVAL_OVERLAP_REQUIRES_RECONCILIATION"
        outcomes.append(
            PostingOutcome(
                session_index=index,
                state=state,
                reason=reason,
                candidate=candidate,
                existing_ids=existing_ids,
            )
        )
    return tuple(outcomes)


def _fingerprint(
    source: StoredUsageDerivation,
    rules: LedgerPlanningRules,
    coverage: LedgerCoverage | None,
    evaluated_at: datetime,
) -> str:
    source_data = source.model_dump(mode="json")
    source_data["receipt"]["duplicate"] = True
    payload = dict(
        source=source_data,
        rules=rules.model_dump(mode="json"),
        coverage=coverage.model_dump(mode="json") if coverage is not None else None,
        evaluated_at=evaluated_at.astimezone(UTC).isoformat(),
    )
    return hashlib.sha256(
        b"aris.lyra.ledger.comparison\x00"
        + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class LedgerPostingPlan(IntervalContract):
    comparison_only: Literal[True] = True
    posting_permitted: Literal[False] = False
    quota_usage_available: Literal[False] = False
    source: StoredUsageDerivation = Field(repr=False)
    rules: LedgerPlanningRules
    coverage: LedgerCoverage | None = Field(repr=False)
    evaluated_at: AwareDatetime
    input_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    outcomes: tuple[PostingOutcome, ...] = Field(max_length=128)

    @model_validator(mode="after")
    def exact_comparison(self) -> "LedgerPostingPlan":
        if self.input_fingerprint != _fingerprint(
            self.source, self.rules, self.coverage, self.evaluated_at
        ) or self.outcomes != _outcomes(self.source, self.rules, self.coverage, self.evaluated_at):
            raise ValueError(
                "ledger comparison differs from its source evidence and explicit context"
            )
        return self


def plan_usage_posting(
    source: StoredUsageDerivation,
    *,
    rules: LedgerPlanningRules,
    coverage: LedgerCoverage | None,
    evaluated_at: datetime,
) -> LedgerPostingPlan:
    source = StoredUsageDerivation.model_validate(source.model_dump(mode="json"))
    rules = LedgerPlanningRules.model_validate(rules.model_dump(mode="json"))
    coverage = (
        LedgerCoverage.model_validate(coverage.model_dump(mode="json"))
        if coverage is not None
        else None
    )
    evaluated_at = _Evaluation(at=evaluated_at).at.astimezone(UTC)
    return LedgerPostingPlan(
        source=source,
        rules=rules,
        coverage=coverage,
        evaluated_at=evaluated_at,
        input_fingerprint=_fingerprint(source, rules, coverage, evaluated_at),
        outcomes=_outcomes(source, rules, coverage, evaluated_at),
    )
