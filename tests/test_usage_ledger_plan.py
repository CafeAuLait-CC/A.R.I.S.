from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aris.core.access.history_schemas import (
    HistoricalNodeUserQuery,
    HistoricalNodeUserResolution,
    MappingHistoryReference,
)
from aris.modules.lyra.derivation_schemas import (
    DerivationSource,
    StoredUsageDerivation,
    UsageDerivationArtifact,
    UsageDerivationReceipt,
    UsageDerivationRequest,
    canonical_derivation,
    derivation_fingerprint,
)
from aris.modules.lyra.ledger_plan import (
    ActiveUsageMeasurement,
    LedgerCoverage,
    LedgerPlanningRules,
    LedgerPostingPlan,
    MeasuredWindow,
    plan_usage_posting,
)
from aris.modules.lyra.reservation_links import (
    LinkedResidency,
    ReservationLinkRequest,
    ResidencyReservationAnalysis,
)
from aris.modules.lyra.residency_attribution import (
    AttributedResidency,
    ResidencyAttributionAnalysis,
)
from aris.modules.lyra.residency_sessions import ResidencyFormationRule, build_residency_sessions
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.vega.usage_schemas import UsageObservationQuery
from tests.test_administration import NOW, admin_state
from tests.test_mapping_history import history_state, remove
from tests.test_residency_attribution import source as observation
from tests.test_usage_derivations import CONTEXT, counts, derivations

__all__ = ["admin_state", "history_state", "derivations"]


def comparison_inputs(source, at, stored=None):
    rules = LedgerPlanningRules(
        recording_start_at=NOW,
        expected_timing_fingerprint=source.artifact.analysis.request.attribution.residency.rule_fingerprint,
    )
    coverage = LedgerCoverage(
        node_id="node-a",
        gpu_uuids=("GPU-a",),
        start_at=NOW,
        end_at=NOW + timedelta(hours=1),
        read_at=at,
        state="complete",
        records=(),
    )
    return source, rules, coverage, at, stored


@pytest.fixture
def planned():
    # Pure comparison matrices consume a fully validated synthetic contract.
    # The stored_plan cases separately exercise real database-issued evidence.
    facts = tuple(observation(seconds, seq) for seconds, seq in ((10, 3), (25, 5), (40, 7)))
    cutoff = NOW + timedelta(seconds=200)
    queries = tuple(
        UsageObservationQuery(
            agent_id=fact.agent_id,
            node_id=fact.node_id,
            message_id=fact.message_id,
            expected_seq=fact.seq,
        )
        for fact in facts
    )
    request = UsageDerivationRequest(
        id=uuid4(),
        sources=queries,
        as_of=cutoff,
        identity_revision=1,
        continuity=UsageContinuityRule(max_gap_seconds=45),
        formation=ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30),
    )
    residency = build_residency_sessions(
        facts, continuity=request.continuity, formation=request.formation
    )
    identity = HistoricalNodeUserResolution(
        query=HistoricalNodeUserQuery(
            node_id="node-a",
            os_username="linux-user",
            start_at=NOW + timedelta(seconds=10),
            end_at=NOW + timedelta(seconds=40),
            as_of=cutoff,
            through_revision=1,
        ),
        status="resolved",
        reason="VERIFIED_USER_COVERS_INTERVAL",
        user_id=uuid4(),
        directory_revision=1,
        evidence=(
            MappingHistoryReference(id=uuid4(), revision=1, effective_at=NOW, fingerprint="a" * 64),
        ),
    )
    attribution = ResidencyAttributionAnalysis(
        as_of=cutoff,
        directory_revision=1,
        residency=residency,
        attributions=(
            AttributedResidency(session_index=0, identity=identity, state="attributed_candidate"),
        ),
    )
    artifact = UsageDerivationArtifact(
        request=request,
        sources=tuple(
            DerivationSource(
                query=query,
                fingerprint=fact.source_fingerprint,
                observed_at=fact.observed_at,
                received_at=fact.received_at,
                compute_observed_at=fact.compute_observed_at,
            )
            for query, fact in zip(queries, facts, strict=True)
        ),
        analysis=ResidencyReservationAnalysis(
            request=ReservationLinkRequest(attribution=attribution),
            links=(LinkedResidency(session_index=0, state="not_provided", reason="NO_REFERENCE"),),
        ),
    )
    at = cutoff + timedelta(seconds=1)
    source = StoredUsageDerivation(
        artifact=artifact,
        receipt=UsageDerivationReceipt(
            id=request.id,
            event_id=uuid4(),
            duplicate=True,
            created_at=at,
            source_count=len(facts),
            request_fingerprint=derivation_fingerprint(canonical_derivation(request), result=False),
            result_fingerprint=derivation_fingerprint(canonical_derivation(artifact), result=True),
        ),
    )
    return comparison_inputs(source, at)


@pytest.fixture
def stored_plan(derivations):
    service, request, state = derivations
    service.record(CONTEXT, request)
    return comparison_inputs(service.exact(request.id), state[5]["now"], derivations)


def plan(state, *, source=None, rules=None, coverage="default", at=None):
    return plan_usage_posting(
        source or state[0],
        rules=rules or state[1],
        coverage=state[2] if coverage == "default" else coverage,
        evaluated_at=at or state[3],
    )


def existing(state, *, start=10, end=40, **changes):
    candidate = plan(state).outcomes[0].candidate
    fields = candidate.model_dump(include=set(MeasuredWindow.model_fields))
    fields.update(
        id=uuid4(),
        start_at=NOW + timedelta(seconds=start),
        end_at=NOW + timedelta(seconds=end),
        observed_microseconds=(end - start) * 1000000,
        **changes,
    )
    return ActiveUsageMeasurement(**fields)


def cover(context, *records, **changes):
    return context[2].model_copy(update={"records": tuple(records), **changes})


def test_known_empty_coverage_only_proposes_unweighted_original_measurement(stored_plan):
    before = counts(stored_plan[4][2][1])
    result = plan(stored_plan)
    row = result.outcomes[0]
    assert row.state == "new_candidate" and row.candidate.observed_microseconds == 30000000
    assert not row.candidate.weighted and row.candidate.end_basis == "last_source_observation"
    assert row.candidate.start_at == NOW + timedelta(seconds=10)
    assert row.candidate.end_at == NOW + timedelta(seconds=40)
    assert row.candidate.source_result_fingerprint == stored_plan[0].receipt.result_fingerprint
    assert (
        result.comparison_only and not result.posting_permitted and not result.quota_usage_available
    )
    assert counts(stored_plan[4][2][1]) == before


def test_missing_or_partial_coverage_is_unknown_instead_of_empty(planned):
    for coverage in (None, cover(planned, state="partial"), cover(planned, state="unavailable")):
        row = plan(planned, coverage=coverage).outcomes[0]
        assert row.state == "unavailable" and row.reason == "LEDGER_COVERAGE_UNAVAILABLE"
        assert row.candidate is not None


def test_exact_existing_measurement_does_not_propose_another_charge(planned):
    record = existing(planned)
    result = plan(planned, coverage=cover(planned, record))
    row = result.outcomes[0]
    assert row.state == "already_recorded" and row.existing_ids == (record.id,)
    assert row.reason == "EXACT_MEASUREMENT_ALREADY_RECORDED" and not result.posting_permitted


@pytest.mark.parametrize("start,end", [(0, 20), (20, 50), (0, 50), (10, 25), (25, 40)])
def test_partial_overlap_never_clips_merges_or_infers_missing_time(planned, start, end):
    record = existing(planned, start=start, end=end)
    row = plan(planned, coverage=cover(planned, record)).outcomes[0]
    assert row.state == "needs_review" and row.reason == "INTERVAL_OVERLAP_REQUIRES_RECONCILIATION"
    assert row.candidate.observed_microseconds == 30000000
    assert row.candidate.start_at == NOW + timedelta(seconds=10)
    assert row.candidate.end_at == NOW + timedelta(seconds=40)


@pytest.mark.parametrize("start,end", [(0, 10), (40, 50)])
def test_touching_half_open_boundaries_are_independent(planned, start, end):
    row = plan(planned, coverage=cover(planned, existing(planned, start=start, end=end))).outcomes[
        0
    ]
    assert row.state == "new_candidate" and row.existing_ids == ()


@pytest.mark.parametrize("change", ["owner", "model", "timing"])
def test_same_window_with_different_owner_model_or_rules_requires_review(planned, change):
    fields = {
        "owner": {"owner_id": uuid4()},
        "model": {"model": "A different model"},
        "timing": {"timing_fingerprint": "f" * 64},
    }[change]
    row = plan(planned, coverage=cover(planned, existing(planned, **fields))).outcomes[0]
    assert row.state == "needs_review"


def test_adjacent_existing_fragments_are_not_automatically_merged_into_a_duplicate(planned):
    first, second = existing(planned, start=10, end=25), existing(planned, start=25, end=40)
    row = plan(planned, coverage=cover(planned, first, second)).outcomes[0]
    assert row.state == "needs_review" and len(row.existing_ids) == 2


def test_overlapping_existing_entries_are_unavailable_even_if_both_look_identical(planned):
    row = plan(planned, coverage=cover(planned, existing(planned), existing(planned))).outcomes[0]
    assert row.state == "unavailable" and row.reason == "EXISTING_MEASUREMENTS_OVERLAP"


def test_duplicate_exact_coverage_rows_are_canonical_but_same_id_conflicts_are_rejected(planned):
    record = existing(planned)
    one = plan(planned, coverage=cover(planned, record))
    repeated = plan(planned, coverage=cover(planned, record, record))
    assert one == repeated
    with pytest.raises(ValidationError):
        plan(
            planned,
            coverage=cover(planned, record, record.model_copy(update={"owner_id": uuid4()})),
        )


def test_coverage_order_is_not_used_to_choose_a_preferred_record(planned):
    first, second = existing(planned, start=0, end=20), existing(planned, start=30, end=50)
    left = plan(planned, coverage=cover(planned, first, second))
    right = plan(planned, coverage=cover(planned, second, first))
    assert left == right and left.outcomes[0].state == "needs_review"


@pytest.mark.parametrize("change", ["node", "gpu", "start", "end"])
def test_insufficient_query_scope_is_unavailable_without_guessing_coverage(planned, change):
    updates = {
        "node": {"node_id": "node-b"},
        "gpu": {"gpu_uuids": ("GPU-other",)},
        "start": {"start_at": NOW + timedelta(seconds=11)},
        "end": {"end_at": NOW + timedelta(seconds=39)},
    }[change]
    row = plan(planned, coverage=cover(planned, **updates)).outcomes[0]
    assert row.state == "unavailable" and row.reason == "LEDGER_QUERY_DOES_NOT_COVER_INTERVAL"


@pytest.mark.parametrize("kind", ["future", "predates"])
def test_coverage_snapshot_time_is_explicit_and_checked(planned, kind):
    read_at = (
        planned[3] + timedelta(seconds=1)
        if kind == "future"
        else planned[0].receipt.created_at - timedelta(microseconds=1)
    )
    row = plan(planned, coverage=cover(planned, read_at=read_at)).outcomes[0]
    assert row.state == "unavailable"
    assert row.reason == (
        "LEDGER_VIEW_NOT_REACHED" if kind == "future" else "LEDGER_VIEW_PREDATES_DERIVATION"
    )


def test_recording_start_cannot_clip_a_source_interval_or_authorize_future_time(planned):
    rule = planned[1].model_copy(update={"recording_start_at": NOW + timedelta(seconds=20)})
    row = plan(planned, rules=rule).outcomes[0]
    assert row.reason == "OUTSIDE_RECORDING_START" and row.state == "needs_review"
    assert row.candidate.observed_microseconds == 30000000
    future = rule.model_copy(update={"recording_start_at": planned[3] + timedelta(seconds=1)})
    assert plan(planned, rules=future).outcomes[0].reason == "RECORDING_START_NOT_REACHED"


def test_different_timing_context_is_not_silently_applied(planned):
    rule = planned[1].model_copy(update={"expected_timing_fingerprint": "f" * 64})
    row = plan(planned, rules=rule).outcomes[0]
    assert row.state == "unavailable" and row.reason == "TIMING_RULES_DIFFER"


def test_evaluation_cannot_precede_derivation_recording_or_omit_timezone(planned):
    row = plan(planned, at=planned[0].receipt.created_at - timedelta(microseconds=1)).outcomes[0]
    assert row.reason == "DERIVATION_NOT_RECORDED_YET"
    with pytest.raises(ValidationError):
        plan(planned, at=planned[3].replace(tzinfo=None))


def test_receipt_replay_marker_does_not_change_measurement_comparison_fingerprint(planned):
    first = plan(planned)
    source = planned[0].model_copy(
        update={"receipt": planned[0].receipt.model_copy(update={"duplicate": False})}
    )
    replay = plan(planned, source=source)
    assert first.input_fingerprint == replay.input_fingerprint and first.outcomes == replay.outcomes


@pytest.mark.parametrize(
    "field", ["id", "source_count", "created_at", "result_fingerprint", "request_fingerprint"]
)
def test_receipt_cannot_be_detached_from_its_stored_artifact(planned, field):
    values = {
        "id": uuid4(),
        "source_count": 1,
        "created_at": NOW - timedelta(days=1),
        "result_fingerprint": "f" * 64,
        "request_fingerprint": "e" * 64,
    }
    source = planned[0].model_copy(
        update={"receipt": planned[0].receipt.model_copy(update={field: values[field]})}
    )
    with pytest.raises(ValidationError):
        plan(planned, source=source)


def test_response_cannot_claim_authority_or_change_its_comparison(planned):
    result = plan(planned)
    for field in ("posting_permitted", "quota_usage_available"):
        body = result.model_dump(mode="json")
        body[field] = True
        with pytest.raises(ValidationError):
            LedgerPostingPlan.model_validate(body)
    body = result.model_dump(mode="json")
    body["outcomes"][0]["state"] = "already_recorded"
    with pytest.raises(ValidationError):
        LedgerPostingPlan.model_validate(body)


def test_timezone_representation_preserves_original_measurement_identity(planned):
    record = existing(planned)
    zone = timezone(timedelta(hours=9))
    record = record.model_copy(
        update={
            "start_at": record.start_at.astimezone(zone),
            "end_at": record.end_at.astimezone(zone),
        }
    )
    assert plan(planned, coverage=cover(planned, record)).outcomes[0].state == "already_recorded"


def test_a_new_derivation_id_cannot_count_the_same_measurement_again(stored_plan):
    service, request, state = stored_plan[4]
    other = request.model_copy(update={"id": uuid4()})
    service.record(CONTEXT, other)
    source = service.exact(other.id)
    row = plan(
        stored_plan, source=source, coverage=cover(stored_plan, existing(stored_plan))
    ).outcomes[0]
    assert row.state == "already_recorded" and row.candidate.source_derivation_id == other.id
    assert row.candidate.source_result_fingerprint != stored_plan[0].receipt.result_fingerprint


def test_ledger_history_is_not_used_to_fill_a_missing_platform_identity(stored_plan):
    service, request, state = stored_plan[4]
    current_time = state[5]["now"]
    changed = remove(state, 8)
    state[5]["now"] = current_time
    other = request.model_copy(update={"id": uuid4(), "identity_revision": changed.revision})
    service.record(CONTEXT, other)
    source = service.exact(other.id)
    row = plan(
        stored_plan, source=source, coverage=cover(stored_plan, existing(stored_plan))
    ).outcomes[0]
    assert row.state == "needs_review" and row.reason == "IDENTITY_UNRESOLVED"
    assert row.candidate is None


def test_short_process_evidence_remains_reviewable_even_with_known_identity(stored_plan):
    service, request, state = stored_plan[4]
    other = request.model_copy(
        update={
            "id": uuid4(),
            "formation": ResidencyFormationRule(minimum_intervals=3, minimum_duration_seconds=30),
        }
    )
    service.record(CONTEXT, other)
    source = service.exact(other.id)
    row = plan(stored_plan, source=source).outcomes[0]
    assert row.state == "needs_review" and row.reason == "INSUFFICIENT_PROCESS_EVIDENCE"
    assert row.candidate is None


def test_empty_source_candidate_list_does_not_become_a_zero_usage_or_quota_claim(stored_plan):
    service, request, state = stored_plan[4]
    other = request.model_copy(update={"id": uuid4(), "sources": request.sources[:1]})
    service.record(CONTEXT, other)
    source = service.exact(other.id)
    result = plan(stored_plan, source=source, coverage=None)
    assert (
        result.outcomes == () and not result.posting_permitted and not result.quota_usage_available
    )


def test_coverage_budget_and_record_scope_are_validated(planned):
    record = existing(planned)
    for changes in (
        {"records": (record,) * 1025},
        {"gpu_uuids": ("GPU-a", "GPU-a")},
        {"records": (record.model_copy(update={"node_id": "node-b"}),)},
    ):
        with pytest.raises(ValidationError):
            plan(planned, coverage=cover(planned, **changes))
