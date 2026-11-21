from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_plan import MeasuredWindow
from aris.modules.lyra.ledger_schemas import (
    ReversedMeasurement,
    UsageLedgerHead,
    make_ledger_entry,
)
from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
from aris.modules.lyra.ledger_window_read import LedgerWindowQuery
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from tests.test_ledger_segments import (
    CONTEXT,
    admin_state,
    derivations,
    fixture,
    history_state,
    ledger,
    measurement,
    raw_append,
    request,
    segment_count,
)

__all__ = ["admin_state", "derivations", "history_state", "ledger"]


def window(first, *, head=None, owner_id=None):
    value = first.details.measurement
    return LedgerWindowQuery(
        node_id=first.node_id,
        start_at=value.start_at + timedelta(seconds=5),
        end_at=value.end_at - timedelta(seconds=5),
        owner_id=owner_id,
        snapshot=head,
    )


def reader(service, now):
    return LedgerWindowReader(service.sessions, service.configuration, clock=lambda: now)


def reverse(service, first, head, now):
    reversal = make_ledger_entry(
        id=uuid4(),
        event_id=uuid4(),
        node_id=head.node_id,
        revision=head.revision + 1,
        previous_fingerprint=head.fingerprint,
        actor="system:lyra",
        request_id="fixture-ledger-window",
        recorded_at=now,
        details=ReversedMeasurement(
            measurement=MeasuredWindow.model_validate(
                first.details.measurement.model_dump(include=set(MeasuredWindow.model_fields))
            ),
            target_id=first.id,
            target_fingerprint=first.fingerprint,
            reason="fixture late reversal",
        ),
    )
    return reversal, raw_append(service, reversal, head)


def test_short_original_tail_read_is_read_only_and_details_are_exact_members(ledger):
    service, first, now = fixture(ledger)
    values = []

    def executed(connection, cursor, statement, parameters, context, executemany):
        values.append(statement.lstrip().split()[0].upper())

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", executed)
    try:
        reads = reader(service, now)
        result = reads.read(window(first))
        assert result.anchor is None and result.segments == ()
        assert [entry.entry_id for entry in result.active] == [first.id]
        assert not result.quota_usage_available and result.reversals == ()
        with service.sessions() as db:
            assert reads.originals_in_transaction(db, result, result.active) == (first,)
            with pytest.raises(DomainError, match="differs"):
                reads.originals_in_transaction(
                    db, result, (result.active[0].model_copy(update={"entry_id": uuid4()}),)
                )
    finally:
        event.remove(engine, "before_cursor_execute", executed)
    assert not {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}.intersection(values)
    assert segment_count(service) == 0
    own = reads.read(window(first, owner_id=first.details.measurement.owner_id))
    other = reads.read(window(first, owner_id=uuid4()))
    assert own.active == result.active and not other.entries and not other.active
    assert own.fingerprint != other.fingerprint


def test_index_behind_is_explicit_and_fixed_old_or_origin_heads_still_read(ledger):
    original, first, now = fixture(ledger)
    config = original.configuration.model_copy(update={"max_entries": 2})
    service = LedgerSegmentService(original.sessions, configuration=config, clock=lambda: now)
    one = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    _, two = measurement(service, first, one, now)
    _, three = measurement(service, first, two, now)
    reads = reader(service, now)
    with pytest.raises(DomainError) as error:
        reads.read(window(first))
    assert error.value.code == "LEDGER_INDEX_BEHIND" and segment_count(service) == 0
    old = reads.read(window(first, head=one))
    assert len(old.active) == 1
    origin = UsageLedgerHead(node_id=first.node_id, revision=0)
    assert reads.read(window(first, head=origin)).entries == ()
    indexed = service.capture(CONTEXT, request(service, two))
    result = reads.read(window(first, head=three))
    assert result.head == three and result.anchor == indexed.reference()
    assert result.active == old.active
    # An old head can lie inside a later verified segment without including its later entries.
    again = reads.read(window(first, head=one))
    assert again.active == old.active and again.fingerprint == old.fingerprint
    for invalid in (
        one.model_copy(update={"fingerprint": "f" * 64}),
        three.model_copy(update={"revision": 4}),
    ):
        with pytest.raises(DomainError) as error:
            reads.read(window(first, head=invalid))
        assert error.value.code == "CONFLICT"


def test_semantic_result_survives_index_growth_after_original_data_read_time(ledger):
    service, first, data_at = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    before = reader(service, data_at).read(window(first, head=head), read_at=data_at)
    verified_at = data_at + timedelta(hours=1)
    indexing = LedgerSegmentService(
        service.sessions, configuration=service.configuration, clock=lambda: verified_at
    )
    proof = indexing.capture(CONTEXT, request(indexing, head))
    later_reader = reader(indexing, verified_at)
    after = later_reader.read(window(first, head=head), read_at=data_at)
    assert after.anchor == proof.reference() and after.verified_at > after.read_at
    assert after.entries == before.entries and after.fingerprint == before.fingerprint
    _, grown = measurement(indexing, first, head, verified_at)
    with pytest.raises(DomainError) as error:
        later_reader.read(window(first, head=grown), read_at=data_at)
    assert error.value.code == "VALIDATION_ERROR"
    assert (
        later_reader.read(window(first, head=head), read_at=data_at).fingerprint
        == before.fingerprint
    )


def test_late_reversal_selects_original_usage_window_and_preserves_old_head(ledger):
    service, first, now = fixture(ledger)
    original = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    proof = service.capture(CONTEXT, request(service, original))
    reads = reader(service, now)
    old = reads.read(window(first, head=original))
    reversal, changed = reverse(service, first, original, now)
    new = reads.read(window(first, head=changed))
    assert not new.active and [value.entry_id for value in new.reversals] == [reversal.id]
    assert new.fingerprint != old.fingerprint
    service.capture(CONTEXT, request(service, changed, proof))
    indexed = reads.read(window(first, head=changed))
    assert indexed.entries == new.entries and indexed.fingerprint == new.fingerprint
    assert reads.read(window(first, head=original)).fingerprint == old.fingerprint
    outside = window(first).model_copy(
        update={"start_at": first.details.measurement.end_at, "end_at": now}
    )
    assert reads.read(outside).entries == ()


def test_overlapping_original_short_tail_is_rejected_before_partial_statistics(ledger):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    service.capture(CONTEXT, request(service, head))
    measurement(
        service, first, head, now, start=first.details.measurement.start_at + timedelta(seconds=5)
    )
    with pytest.raises(DomainError) as error:
        reader(service, now).read(window(first))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_live_query_can_see_a_commit_and_index_completed_after_the_request_started(ledger):
    service, first, started = fixture(ledger)
    original = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    committed = started + timedelta(seconds=1)
    indexing = LedgerSegmentService(
        service.sessions, configuration=service.configuration, clock=lambda: committed
    )
    _, grown = measurement(indexing, first, original, committed)
    indexing.capture(CONTEXT, request(indexing, grown))
    ticks = iter((started, committed + timedelta(seconds=1), committed + timedelta(seconds=2)))
    reads = LedgerWindowReader(service.sessions, service.configuration, clock=lambda: next(ticks))
    result = reads.read(window(first))
    assert result.head == grown and result.read_at >= committed
    assert len(result.active) == 1 and result.verified_at >= result.read_at
    # A deliberately fixed historical data cutoff still excludes that later business commit.
    with pytest.raises(DomainError) as error:
        reader(indexing, committed + timedelta(seconds=3)).read(window(first), read_at=started)
    assert error.value.code == "VALIDATION_ERROR"
