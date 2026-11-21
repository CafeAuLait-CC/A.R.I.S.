import pytest
from sqlalchemy import event

from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_head_proof import LedgerHeadVerifier
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from tests.test_ledger_segments import (
    CONTEXT,
    admin_state,
    derivations,
    fixture,
    history_state,
    ledger,
    measurement,
    request,
    segment_count,
)
from tests.test_ledger_window_reading import window

__all__ = ["admin_state", "derivations", "history_state", "ledger"]


def test_heads_outside_time_selection_are_verified_and_index_growth_keeps_semantic_proof(ledger):
    service, first, now = fixture(ledger)
    one = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    second, two = measurement(service, first, one, now)
    reader = LedgerWindowReader(service.sessions, service.configuration, clock=lambda: now)
    view = reader.read(window(second, head=two))
    assert [entry.entry_id for entry in view.entries] == [second.id]
    verifier = LedgerHeadVerifier(reader)
    values = []

    def seen(connection, cursor, statement, parameters, context, many):
        values.append(statement.lstrip().split()[0].upper())

    engine = service.sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", seen)
    try:
        before = verifier.verify(view, (two, one, one))
    finally:
        event.remove(engine, "before_cursor_execute", seen)
    assert before.heads == (one, two) and before.original_tail_heads == (one, two)
    assert before.contains(one) and before.contains(two) and not before.quota_usage_available
    assert not {"INSERT", "UPDATE", "DELETE", "TRUNCATE"}.intersection(values)
    assert segment_count(service) == 0
    first_segment = service.capture(CONTEXT, request(service, one))
    middle = verifier.verify(view, (one, two))
    assert len(middle.index_segments) == 1 and middle.original_tail_heads == (two,)
    assert middle.fingerprint == before.fingerprint
    service.capture(CONTEXT, request(service, two, first_segment))
    fresh_view = reader.read(window(second, head=two))
    indexed = verifier.verify(fresh_view, (one, two))
    assert len(indexed.index_segments) == 2 and not indexed.original_tail_heads
    assert indexed.fingerprint == before.fingerprint and indexed.through == two


def test_wrong_scope_point_or_uncommitted_head_cannot_become_receipt_evidence(ledger):
    service, first, now = fixture(ledger)
    one = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    reader = LedgerWindowReader(service.sessions, service.configuration, clock=lambda: now)
    view = reader.read(window(first, head=one))
    verifier = LedgerHeadVerifier(reader)
    wrong = one.model_copy(update={"fingerprint": "f" * 64})
    for heads in (
        (wrong,),
        (one, wrong),
        (one.model_copy(update={"node_id": "other-node"}),),
        (one.model_copy(update={"revision": 2}),),
        (UsageLedgerHead(node_id=one.node_id, revision=0),),
    ):
        with pytest.raises(DomainError) as error:
            verifier.verify(view, heads)
        assert error.value.code == "CONFLICT"
    assert segment_count(service) == 0


def test_dependency_count_encoded_bytes_and_expired_deadline_keep_their_bounds(ledger):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    reader = LedgerWindowReader(service.sessions, service.configuration, clock=lambda: now)
    view = reader.read(window(first, head=head))
    verifier = LedgerHeadVerifier(reader)
    with pytest.raises(DomainError) as error:
        verifier.verify(view, (head,) * 129)
    assert error.value.code == "USAGE_REPORT_TOO_LARGE"
    with service.sessions() as db, pytest.raises(DomainError) as error:
        verifier.verify_in_transaction(db, view, (head,), deadline=0.0)
    assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    service.capture(CONTEXT, request(service, head))
    with service.sessions() as db, pytest.raises(DomainError) as error:
        LedgerSegmentRepository(db).covering_many(head.node_id, (1,), max_bytes=1)
    assert error.value.code == "USAGE_REPORT_TOO_LARGE"
    assert segment_count(service) == 1
