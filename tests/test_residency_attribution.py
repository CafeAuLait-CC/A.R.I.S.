from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import event, text

from aris.core.access.history_schemas import (
    HistoricalNodeUserBatch,
)
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_attribution import (
    ResidencyAttributionAnalysis,
    ResidencyAttributionService,
)
from aris.modules.lyra.residency_sessions import ResidencyFormationRule
from aris.modules.lyra.trace_state import UsageContinuityRule
from tests.test_administration import NOW, admin_state
from tests.test_mapping_history import assign, counts, history_state, query, remove
from tests.test_usage_interval_candidates import frame

__all__ = ["admin_state", "history_state"]
CONTINUITY = UsageContinuityRule(max_gap_seconds=45)
FORMATION = ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30)


def source(seconds=10, seq=1, **kwargs):
    fact = frame(seconds, seq, username="linux-user", **kwargs)
    shift = NOW - frame().observed_at
    return fact.model_copy(
        update={
            "node_id": "node-a",
            "observed_at": fact.observed_at + shift,
            "gpu_observed_at": fact.gpu_observed_at + shift,
            "compute_observed_at": fact.compute_observed_at + shift,
            "received_at": NOW + timedelta(seconds=seconds + 1),
        }
    )


def analyze(state, facts=None, *, formation=FORMATION, revision=None, cutoff=200, reader=None):
    _, sessions, _, _, _, clock, history = state
    clock["now"] = NOW + timedelta(seconds=200)
    return ResidencyAttributionService(sessions, reader or history).analyze(
        facts if facts is not None else [source(), source(25, 3), source(40, 5)],
        continuity=CONTINUITY,
        formation=formation,
        as_of=NOW + timedelta(seconds=cutoff),
        through_revision=revision,
    )


def test_batch_pins_one_directory_revision_and_caches_exact_duplicate_queries(history_state):
    *_, clock, history = history_state
    first = assign(history_state, 0)
    clock["now"] = NOW + timedelta(seconds=200)
    result = history.resolve_many(HistoricalNodeUserBatch(queries=(query(10, 20),) * 128))
    assert len(result.answers) == 128 and result.directory_revision == first.revision
    assert all(answer == result.answers[0] for answer in result.answers)
    assert result.answers[0].query.through_revision == first.revision


def test_batch_rejects_mixed_cutoffs_revisions_and_oversized_input():
    for values in (
        (query(10, 20), query(10, 20, as_of=199)),
        (query(10, 20), query(10, 20, revision=1)),
        (query(10, 20),) * 129,
        (),
    ):
        with pytest.raises(ValidationError):
            HistoricalNodeUserBatch(queries=values)


def test_batch_uninitialized_directory_and_future_cutoff_remain_unknown(admin_state):
    _, sessions, _ = admin_state
    history = NodeUserHistoryService(sessions, clock=lambda: NOW + timedelta(seconds=200))
    result = history.resolve_many(HistoricalNodeUserBatch(queries=(query(10, 20),)))
    assert result.directory_revision is None
    assert result.answers[0].reason == "DIRECTORY_UNINITIALIZED"
    history.clock = lambda: NOW
    future = history.resolve_many(HistoricalNodeUserBatch(queries=(query(10, 20),)))
    assert future.directory_revision is None and future.answers[0].reason == "AS_OF_NOT_REACHED"


def test_attribution_preserves_source_windows_and_immutable_history_without_writing(history_state):
    _, sessions, _, _, users, _, _ = history_state
    assign(history_state, 0)
    before = counts(sessions)
    result = analyze(history_state)
    assert result.candidate_only and not result.formal_usage_available
    row = result.attributions[0]
    assert row.state == "attributed_candidate" and str(row.identity.user_id) == users[0]
    assert row.identity.query.start_at == NOW + timedelta(seconds=10)
    assert row.identity.query.end_at == NOW + timedelta(seconds=40)
    assert row.identity.query.through_revision == result.directory_revision
    assert counts(sessions) == before


def test_insufficient_process_evidence_does_not_become_eligible_just_because_identity_resolves(
    history_state,
):
    assign(history_state, 0)
    result = analyze(history_state, [source(), source(25, 3)])
    row = result.attributions[0]
    assert row.identity.status == "resolved" and row.state == "insufficient_evidence"
    assert not result.formal_usage_available


def test_missing_or_later_mapping_does_not_backfill_old_process_residency(history_state):
    assert analyze(history_state).attributions[0].state == "identity_unresolved"
    assign(history_state, 50)
    result = analyze(history_state)
    assert result.attributions[0].identity.reason == "HISTORY_DOES_NOT_COVER_INTERVAL"
    assert result.attributions[0].identity.user_id is None


def test_reassignment_removal_and_frozen_revision_preserve_original_identity(history_state):
    *_, users, clock, history = history_state
    first = assign(history_state, 0)
    assign(history_state, 50, 1, expected=users[0])
    assert str(analyze(history_state).attributions[0].identity.user_id) == users[0]
    crossing = analyze(history_state, [source(40), source(55, 3), source(70, 5)])
    assert crossing.attributions[0].identity.reason == "MAPPING_CHANGED_DURING_INTERVAL"
    assert crossing.attributions[0].state == "identity_unresolved"
    frozen = analyze(
        history_state, [source(40), source(55, 3), source(70, 5)], revision=first.revision
    )
    assert str(frozen.attributions[0].identity.user_id) == users[0]
    remove(history_state, 80, user=1)
    assert str(analyze(history_state).attributions[0].identity.user_id) == users[0]


class NoIdentityReads:
    def resolve_many_in_transaction(self, session, request):
        raise AssertionError("unexpected identity lookup")


@pytest.mark.parametrize("kind", ["idle", "shared", "single"])
def test_empty_candidate_set_does_not_guess_identity_or_zero_usage(history_state, kind):
    options = {"count": 0} if kind == "idle" else {"shared": True, "count": 2}
    facts = [source()] if kind == "single" else [source(**options), source(25, 3, **options)]
    result = analyze(history_state, facts, reader=NoIdentityReads())
    assert result.attributions == () and result.directory_revision is None
    assert not result.formal_usage_available


@pytest.mark.parametrize("field", ["received_at", "observed_at"])
def test_future_knowledge_is_rejected_before_any_identity_queries(history_state, field):
    fact = source().model_copy(update={field: NOW + timedelta(seconds=201)})
    with pytest.raises(DomainError, match="knowledge cutoff"):
        analyze(history_state, [fact], reader=NoIdentityReads())


def test_empty_candidate_set_still_requires_timezone_and_valid_revision(history_state):
    _, sessions, _, _, _, _, history = history_state
    service = ResidencyAttributionService(sessions, history)
    for changes in ({"as_of": NOW.replace(tzinfo=None)}, {"through_revision": 0}):
        values = dict(as_of=NOW + timedelta(seconds=200), through_revision=None)
        values.update(changes)
        with pytest.raises(ValidationError):
            service.analyze([source(count=0)], continuity=CONTINUITY, formation=FORMATION, **values)


def test_source_reordering_and_duplicates_preserve_attributions(history_state):
    assign(history_state, 0)
    facts = [source(), source(25, 3), source(40, 5)]
    first = analyze(history_state, facts)
    replay = analyze(history_state, list(reversed(facts)) + [facts[0]])
    assert (
        first.attributions == replay.attributions
        and first.directory_revision == replay.directory_revision
    )
    assert replay.residency.trace.duplicate_messages == 1


def test_oversized_candidate_set_is_refused_without_partial_results(history_state):
    facts = []
    for i in range(4):
        fact = source(10 + i * 15, 1 + i * 2, model="A" if i < 2 else "B")
        gpus = tuple(
            fact.gpus[0].model_copy(update={"gpu_uuid": f"GPU-{j}", "index": j}) for j in range(65)
        )
        facts.append(fact.model_copy(update={"gpus": gpus}))
    with pytest.raises(DomainError, match="candidate budget"):
        analyze(history_state, facts, reader=NoIdentityReads())


def test_response_cannot_attach_another_window_or_omit_a_candidate(history_state):
    assign(history_state, 0)
    result = analyze(history_state)
    body = result.model_dump(mode="json")
    body["attributions"][0]["identity"]["query"]["os_username"] = "some-other-user"
    with pytest.raises(ValidationError):
        ResidencyAttributionAnalysis.model_validate(body)
    body = result.model_dump(mode="json")
    body["attributions"] = []
    with pytest.raises(ValidationError):
        ResidencyAttributionAnalysis.model_validate(body)


def test_provider_cannot_return_identity_for_a_different_source_window(history_state):
    *_, clock, history = history_state
    assign(history_state, 0)
    clock["now"] = NOW + timedelta(seconds=200)
    wrong = history.resolve_many(HistoricalNodeUserBatch(queries=(query(11, 40),)))

    class WrongWindow:
        def resolve_many_in_transaction(self, session, request):
            return wrong

    with pytest.raises(ValidationError, match="original residency window"):
        analyze(history_state, reader=WrongWindow())


def test_postgresql_batch_read_only_has_a_bounded_duplicate_query_budget(history_state):
    _, sessions, engine, _, _, clock, history = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL identity batch READ ONLY and query budget")
    assign(history_state, 0)
    clock["now"] = NOW + timedelta(seconds=200)
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        event.listen(engine, "before_cursor_execute", record)
        try:
            result = history.resolve_many_in_transaction(
                session, HistoricalNodeUserBatch(queries=(query(10, 20),) * 128)
            )
        finally:
            event.remove(engine, "before_cursor_execute", record)
    assert len(result.answers) == 128 and len(statements) == 8


def test_postgresql_batch_change_after_cached_answer_invalidates_whole_batch(
    history_state, monkeypatch
):
    _, _, engine, _, users, clock, history = history_state
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL identity batch concurrent snapshot change")
    assign(history_state, 0)
    clock["now"] = NOW + timedelta(seconds=200)
    original = history.resolve_in_transaction

    def changed(session, selection):
        result = original(session, selection)
        assign(history_state, 100, 1, expected=users[0])
        clock["now"] = NOW + timedelta(seconds=200)
        return result

    monkeypatch.setattr(history, "resolve_in_transaction", changed)
    with pytest.raises(DomainError) as error:
        history.resolve_many(HistoricalNodeUserBatch(queries=(query(10, 20),) * 2))
    assert error.value.code == "CONFLICT"
