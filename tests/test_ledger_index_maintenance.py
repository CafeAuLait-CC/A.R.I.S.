from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event

from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_index_maintenance import LedgerIndexMaintenance
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import PostedMeasurement, UsageLedgerHead, make_ledger_entry
from aris.modules.lyra.ledger_segment_schemas import ledger_read_configuration
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from tests.test_ledger_segments import (
    admin_state,
    derivations,
    fixture,
    history_state,
    ledger,
    measurement,
    raw_append,
    segment_count,
)
from tests.test_ledger_window_reading import window
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger"]


def test_idle_and_partial_tail_do_not_write_or_query_every_tick_then_activity_advances(ledger):
    service, first, now = fixture(ledger)
    clock = {"now": now}
    worker = LedgerIndexMaintenance(
        service.sessions,
        (first.node_id, "empty-node"),
        clock=lambda: clock["now"],
        batch_entries=2,
    )
    statements = []

    def seen(connection, cursor, statement, parameters, context, many):
        statements.append(statement.lstrip().split()[0].upper())

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", seen)
    try:
        assert worker.step(POST).remaining_entries == 1
        assert worker.step(POST).observed_head.revision == 0
        before = len(statements)
        assert worker.step(POST) is None and len(statements) == before
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert not {"INSERT", "UPDATE", "DELETE"}.intersection(statements)
    assert segment_count(service) == 0
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    _, head = measurement(service, first, head, now)
    step = worker.step(POST, activity=True)
    assert step.state == "advanced" and step.indexed_revision == 2 and step.remaining_entries == 0
    assert segment_count(service) == 1
    _, head = measurement(service, first, head, now)
    rebuilt = LedgerIndexMaintenance(
        service.sessions, (first.node_id,), clock=lambda: clock["now"], batch_entries=2
    )
    assert rebuilt.step(POST).indexed_revision == 2 and segment_count(service) == 1
    reads = LedgerWindowReader(
        service.sessions, ledger_read_configuration(first.node_id), clock=lambda: now
    )
    assert reads.read(window(first, head=head)).active[0].entry_id == first.id
    clock["now"] += timedelta(seconds=31)
    assert rebuilt.step(POST).remaining_entries == 1


def test_failed_node_does_not_starve_another_and_lost_commit_response_restarts_from_index(
    ledger, monkeypatch
):
    service, first, now = fixture(ledger)
    one = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    _, head = measurement(service, first, one, now)
    worker = LedgerIndexMaintenance(
        service.sessions, (first.node_id, "empty-node"), clock=lambda: now, batch_entries=2
    )
    target = worker.services[first.node_id]
    capture = target.capture

    def unavailable(*args, **kwargs):
        raise DomainError("DEPENDENCY_UNAVAILABLE", "fixture index dependency", 503)

    with monkeypatch.context() as patch:
        patch.setattr(target, "capture", unavailable)
        with pytest.raises(DomainError):
            worker.step(POST)
    assert worker.step(POST).node_id == "empty-node" and segment_count(service) == 0

    def lost(*args, **kwargs):
        capture(*args, **kwargs)
        raise RuntimeError("fixture committed index response lost")

    with monkeypatch.context() as patch:
        patch.setattr(target, "capture", lost)
        with pytest.raises(RuntimeError, match="response lost"):
            worker.step(POST, activity=True)
    assert segment_count(service) == 1
    rebuilt = LedgerIndexMaintenance(
        service.sessions, (first.node_id,), clock=lambda: now, batch_entries=2
    )
    result = rebuilt.step(POST)
    assert result.state == "within_tail" and result.indexed_revision == head.revision
    assert segment_count(service) == 1
    with service.sessions() as db:
        assert UsageLedgerRepository(db).head(first.node_id) == head


def test_actual_encoded_index_budget_reduces_one_batch_per_step_without_partial_writes(ledger):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    for revision in range(127):
        start = first.details.measurement.end_at + timedelta(seconds=45 * revision)
        proposal = first.details.measurement.model_copy(
            update={
                "gpu_uuid": "界" * 128,
                "model": "界" * 256,
                "start_at": start,
                "end_at": start + timedelta(seconds=30),
                "observed_microseconds": 30000000,
            }
        )
        entry = make_ledger_entry(
            id=uuid4(),
            event_id=uuid4(),
            node_id=head.node_id,
            revision=head.revision + 1,
            previous_fingerprint=head.fingerprint,
            actor="system:lyra",
            request_id="fixture-large-index",
            recorded_at=now,
            details=PostedMeasurement(
                measurement=proposal, configuration=first.details.configuration
            ),
        )
        head = raw_append(service, entry, head)
    worker = LedgerIndexMaintenance(service.sessions, (first.node_id,), clock=lambda: now)
    with pytest.raises(DomainError) as error:
        worker.step(POST)
    assert error.value.code == "LEDGER_SEGMENT_TOO_LARGE" and segment_count(service) == 0
    first_step = worker.step(POST)
    assert first_step.batch_entries == 64 and first_step.indexed_revision == 64
    second_step = worker.step(POST)
    assert second_step.batch_entries == 64 and second_step.indexed_revision == 128
    assert second_step.remaining_entries == 0 and segment_count(service) == 2
    assert (
        worker.services[first.node_id].reverify(first_step.segment.id).reference()
        == first_step.segment
    )
