import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.lyra.ledger_plan import MeasuredWindow
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    ReversedMeasurement,
    UsageLedgerHead,
    make_ledger_entry,
)
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository
from aris.modules.lyra.ledger_segment_schemas import (
    CaptureLedgerSegment,
    LedgerSegmentConfiguration,
)
from aris.modules.lyra.ledger_segment_service import LedgerSegmentService
from aris.modules.lyra.models import LedgerSegmentRow
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeContext
from tests.test_usage_ledger import POST, admin_state, derivations, history_state, ledger

__all__ = ["admin_state", "derivations", "history_state", "ledger"]
CONTEXT = UsageTraceConsumeContext(request_id="fixture-ledger-segment")


def fixture(ledger):
    writer = ledger[0]
    receipt = writer.post(POST, ledger[1])
    first = writer.exact(receipt.entries[0])
    now = max(datetime.now(UTC), first.recorded_at) + timedelta(seconds=1)
    config = LedgerSegmentConfiguration(node_id=first.node_id, source_ref="fixture-original-ledger")
    service = LedgerSegmentService(writer.sessions, configuration=config, clock=lambda: now)
    return service, first, now


def raw_append(service, entry, head):
    # Synthetic original ledger rows exercise chain invariants, not GPU source qualification.
    # No claim projection is created: the verifier must use immutable original history.
    with service.sessions.begin() as db:
        db.add(
            DomainEvent(
                event_id=str(entry.event_id),
                event_type="fixture.original_ledger",
                event_version=1,
                aggregate_type="usage_ledger",
                aggregate_id=entry.node_id,
                aggregate_version=entry.revision,
                occurred_at=entry.recorded_at,
                recorded_at=entry.recorded_at,
                actor="system:lyra",
                correlation_id="fixture-ledger-segment",
                causation_id=str(uuid4()),
                payload={},
            )
        )
        db.flush()
        UsageLedgerRepository(db).append(entry, head)
        db.flush()
    return UsageLedgerHead(
        node_id=entry.node_id, revision=entry.revision, fingerprint=entry.fingerprint
    )


def measurement(service, original, head, now, *, start=None):
    begin = (
        start
        if start is not None
        else original.details.measurement.end_at + timedelta(seconds=45 * head.revision)
    )
    proposal = original.details.measurement.model_copy(
        update={
            "start_at": begin,
            "end_at": begin + timedelta(seconds=30),
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
        request_id="fixture-ledger-segment",
        recorded_at=now,
        details=PostedMeasurement(
            measurement=proposal, configuration=original.details.configuration
        ),
    )
    return entry, raw_append(service, entry, head)


def request(service, head, previous=None):
    return CaptureLedgerSegment(
        id=uuid4(),
        target=head,
        previous=previous.reference() if previous else None,
        expected_configuration_fingerprint=service.configuration.fingerprint,
    )


def segment_count(service):
    with service.sessions() as db:
        return db.scalar(select(func.count()).select_from(LedgerSegmentRow))


def test_segment_binds_original_chain_retries_and_keeps_old_anchor_after_growth(ledger):
    service, first, now = fixture(ledger)
    initial = request(
        service, UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    )
    one = service.capture(CONTEXT, initial)
    assert one.verification_scope == "previously_verified_original_ledger_chain"
    assert not one.quota_usage_available and one.entries[0].entry_id == first.id
    assert service.capture(CONTEXT, initial) == one and segment_count(service) == 1
    second, head = measurement(service, first, one.request.target, now)
    two = service.capture(CONTEXT, request(service, head, one))
    assert two.entries[0].entry_id == second.id and len(two.entries) == 1
    assert service.reverify(one.request.id) == one and service.reverify(two.request.id) == two
    assert LedgerSegmentService(service.sessions).exact(one.request.id) == one
    with pytest.raises(DomainError) as error:
        LedgerSegmentService(service.sessions).capture(CONTEXT, initial)
    assert error.value.code == "USAGE_NOT_CONFIGURED"
    assert segment_count(service) == 2


@pytest.mark.parametrize("wrong_target", [False, True])
def test_cross_segment_reversal_uses_original_target_and_does_not_change_old_proof(
    ledger, wrong_target
):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    one = service.capture(CONTEXT, request(service, head))
    measured = MeasuredWindow.model_validate(
        first.details.measurement.model_dump(include=set(MeasuredWindow.model_fields))
    )
    reversal = make_ledger_entry(
        id=uuid4(),
        event_id=uuid4(),
        node_id=first.node_id,
        revision=2,
        previous_fingerprint=first.fingerprint,
        actor="system:lyra",
        request_id="fixture-ledger-segment",
        recorded_at=now,
        details=ReversedMeasurement(
            measurement=measured,
            target_id=first.id,
            target_fingerprint="f" * 64 if wrong_target else first.fingerprint,
            reason="fixture reversal",
        ),
    )
    head = raw_append(service, reversal, head)
    if wrong_target:
        with pytest.raises(DomainError):
            service.capture(CONTEXT, request(service, head, one))
        assert segment_count(service) == 1 and service.reverify(one.request.id) == one
        return
    two = service.capture(CONTEXT, request(service, head, one))
    assert two.entries[0].target_id == first.id and service.reverify(two.request.id) == two
    assert service.reverify(one.request.id) == one
    # A different, later measured window remains valid even though a past target was reversed.
    other, head = measurement(service, first, head, now)
    three = service.capture(CONTEXT, request(service, head, two))
    assert three.entries[0].entry_id == other.id


def test_wrong_fixed_head_or_frontier_is_rejected_without_writes(ledger):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    for target in (
        head.model_copy(update={"fingerprint": "f" * 64}),
        head.model_copy(update={"revision": 2}),
    ):
        with pytest.raises(DomainError):
            service.capture(CONTEXT, request(service, target))
        assert segment_count(service) == 0
    one = service.capture(CONTEXT, request(service, head))
    _, head = measurement(service, first, head, now)
    with pytest.raises(DomainError, match="frontier"):
        service.capture(CONTEXT, request(service, head))
    assert segment_count(service) == 1 and service.reverify(one.request.id) == one


def test_original_active_overlap_is_rejected_even_without_mutable_claims(ledger):
    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    one = service.capture(CONTEXT, request(service, head))
    _, head = measurement(
        service, first, head, now, start=first.details.measurement.start_at + timedelta(seconds=10)
    )
    with pytest.raises(DomainError):
        service.capture(CONTEXT, request(service, head, one))
    assert segment_count(service) == 1 and service.reverify(one.request.id) == one


def test_write_failure_rolls_back_and_lost_commit_response_reuses_original(ledger, monkeypatch):
    service, first, _ = fixture(ledger)
    target = request(
        service, UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    )
    original = LedgerSegmentRepository.append

    def fail(repo, value):
        original(repo, value)
        repo.session.flush()
        raise RuntimeError("fixture after segment write")

    with monkeypatch.context() as patch:
        patch.setattr(LedgerSegmentRepository, "append", fail)
        with pytest.raises(RuntimeError):
            service.capture(CONTEXT, target)
    assert segment_count(service) == 0
    import aris.modules.lyra.ledger_segment_service as module

    commit = module.commit_owned_transaction

    def lost(db):
        commit(db)
        raise RuntimeError("fixture response lost")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", lost)
        with pytest.raises(RuntimeError):
            service.capture(CONTEXT, target)
    saved = service.capture(CONTEXT, target)
    assert segment_count(service) == 1 and service.reverify(saved.request.id) == saved


def test_segment_history_is_immutable_and_prevents_downgrade(ledger):
    from alembic import command
    from alembic.config import Config

    service, first, _ = fixture(ledger)
    value = service.capture(
        CONTEXT,
        request(
            service,
            UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint),
        ),
    )
    statements = [
        "UPDATE usage_ledger_segments SET last_revision=last_revision",
        "DELETE FROM usage_ledger_segments",
    ]
    if service.sessions.kw["bind"].dialect.name == "postgresql":
        statements.append("TRUNCATE usage_ledger_segments")
    for statement in statements:
        with pytest.raises(DBAPIError), service.sessions.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="history"):
        command.downgrade(Config("alembic.ini"), "0041")
    assert service.reverify(value.request.id) == value


@pytest.mark.skipif(
    not os.environ.get("ARIS_TEST_DATABASE_URL"),
    reason="large ledger integration uses owned PostgreSQL",
)
def test_segment_verification_continues_past_the_legacy_4096_history_limit(ledger, settings):
    import csv
    import io
    from unittest.mock import patch

    from aris.interfaces.usage.service import UsageHistoryInput
    from aris.modules.lyra.indexed_usage_service import IndexedUsageHistoryService
    from aris.modules.lyra.ledger_window_service import LedgerWindowReader
    from aris.modules.lyra.usage_history import UsageHistoryQuery
    from tests.test_ledger_window_reading import reverse, window
    from tests.test_recorded_usage import reading_service
    from tests.test_usage_history import TOKEN, surface_for

    service, first, now = fixture(ledger)
    head = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    for _ in range(4096):
        last, head = measurement(service, first, head, now)
    with service.sessions() as db:
        with pytest.raises(DomainError) as error:
            UsageLedgerRepository(db).prefix(head)
        assert error.value.code == "USAGE_REPORT_TOO_LARGE"
    previous = None
    for through in range(256, 4097, 256):
        with service.sessions() as db:
            point = UsageLedgerRepository(db).revision(first.node_id, through)
        target = UsageLedgerHead(
            node_id=first.node_id, revision=through, fingerprint=point.fingerprint
        )
        previous = service.capture(CONTEXT, request(service, target, previous))
        assert len(previous.entries) == 256
    final = service.capture(CONTEXT, request(service, head, previous))
    assert segment_count(service) == 17 and final.request.target.revision == 4097
    assert len(final.entries) == 1 and service.reverify(final.request.id) == final
    assert not final.quota_usage_available
    reads = LedgerWindowReader(service.sessions, service.configuration, clock=lambda: now)
    old = reads.read(window(first, head=head))
    assert [value.entry_id for value in old.active] == [first.id]
    with service.sessions() as db:
        assert reads.originals_in_transaction(db, old, old.active) == (first,)
    # The relevant window itself may exceed the former whole-prefix count limit.
    week = window(first, head=head).model_copy(
        update={
            "start_at": first.details.measurement.start_at,
            "end_at": last.details.measurement.end_at,
        }
    )
    full = reads.read(week)
    assert len(full.active) == 4097
    assert (
        sum((value.end_at - value.start_at) // timedelta(microseconds=1) for value in full.active)
        == first.details.measurement.observed_microseconds + 4096 * 30000000
    )
    scope = reading_service(ledger)
    surface, actor, _ = surface_for((ledger, scope, None), settings)
    surface.history = IndexedUsageHistoryService(
        service.sessions,
        clock=lambda: now,
        timeout_ms=10000,
        index_configuration=lambda node: service.configuration,
    )
    selected = UsageHistoryInput(
        node_id=head.node_id,
        mode="node",
        snapshot=head,
        limit=100,
        window={
            "kind": "utc",
            "start_at": week.start_at,
            "end_at": week.end_at,
            "timezone": "UTC",
        },
    )
    with (
        patch.object(UsageLedgerRepository, "prefix", side_effect=AssertionError("whole history")),
        patch.object(
            LedgerSegmentRepository, "entries", side_effect=AssertionError("original bodies")
        ),
    ):
        page = surface.read("Bearer " + TOKEN, "fixture-long-stats", selected)
        assert page.report.segment_count == 4097 and len(page.report.rows) == 100
        assert page.has_more and page.report.snapshot == head
        last_page = surface.read(
            "Bearer " + TOKEN, "fixture-long-stats", selected.model_copy(update={"offset": 4096})
        )
        assert len(last_page.report.rows) == 1 and not last_page.has_more
        assert last_page.report.fingerprint == page.report.fingerprint
        exported = surface.read("Bearer " + TOKEN, "fixture-long-stats", selected, output="csv")
        rows = list(csv.DictReader(io.StringIO(exported.decode("utf-8-sig"))))
        assert len(rows) == 4097
        assert (
            sum(int(row["recorded_microseconds"]) for row in rows)
            == page.report.recorded_microseconds
        )
        assert {row["report_fingerprint"] for row in rows} == {page.report.fingerprint}
        assert {row["source_derivation_id"] for row in rows} == {
            str(first.details.measurement.source_derivation_id)
        }
        html = surface.read("Bearer " + TOKEN, "fixture-long-stats", selected, output="html")
        assert page.report.fingerprint.encode() in html
        stats = surface.history.read(
            actor,
            UsageHistoryQuery(**week.model_dump(exclude={"owner_id"})),
            mode="node",
        )
        assert (
            stats.segment_count == 4097
            and sum(day.recorded_microseconds for day in stats.days) == stats.recorded_microseconds
        )
    _, changed = reverse(service, first, head, now)
    adjusted = reads.read(window(first, head=changed))
    assert not adjusted.active and len(adjusted.reversals) == 1
    assert reads.read(window(first, head=head)).fingerprint == old.fingerprint
    service.capture(CONTEXT, request(service, changed, final))
    assert reads.read(window(first, head=changed)).fingerprint == adjusted.fingerprint


@pytest.mark.skipif(
    not os.environ.get("ARIS_TEST_DATABASE_URL"), reason="requires PostgreSQL node locks"
)
@pytest.mark.parametrize("same_request", [True, False])
def test_node_frontier_race_returns_one_commit_or_original_idempotent_result(ledger, same_request):
    service, first, _ = fixture(ledger)
    target = UsageLedgerHead(node_id=first.node_id, revision=1, fingerprint=first.fingerprint)
    one = request(service, target)
    two = one if same_request else request(service, target)
    barrier = Barrier(2)

    def capture(value):
        barrier.wait(timeout=10)
        try:
            return service.capture(CONTEXT, value)
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(capture, (one, two)))
    accepted = [value for value in results if not isinstance(value, str)]
    assert segment_count(service) == 1
    if same_request:
        assert len(accepted) == 2 and accepted[0] == accepted[1]
    else:
        assert len(accepted) == 1 and results.count("CONFLICT") == 1
