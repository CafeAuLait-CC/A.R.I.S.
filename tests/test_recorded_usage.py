from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, text

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_plan import MeasuredWindow, UsageMeasurementProposal
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import (
    PostedMeasurement,
    ReversedMeasurement,
    UsageLedgerHead,
    UsagePostingConfiguration,
    make_ledger_entry,
)
from aris.modules.lyra.models import UsageLedgerClaim, UsageLedgerState
from aris.modules.lyra.recorded_usage import (
    RecordedUsageMetrics,
    RecordedUsageQuery,
    recorded_usage_metrics,
)
from aris.modules.lyra.recorded_usage_service import RecordedUsageService
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import POST, counts, ledger, post_window, postgres
from tests.test_usage_reversals import adjustments, confirm, pending

__all__ = ["admin_state", "history_state", "derivations", "ledger", "adjustments"]
BASE = datetime(2026, 11, 16, 12, tzinfo=UTC)
OWNER = UUID("f52604b8-a985-450e-a895-260ad75d0512")


def measured(*, previous=None, start=10, end=40, **changes):
    values = dict(
        node_id="node-a",
        gpu_uuid="GPU-a",
        owner_id=OWNER,
        model="fixture GPU",
        start_at=BASE + timedelta(seconds=start),
        end_at=BASE + timedelta(seconds=end),
        observed_microseconds=(end - start) * 1000000,
        timing_fingerprint="a" * 64,
        source_derivation_id=uuid4(),
        source_result_fingerprint="b" * 64,
        source_session_index=0,
        identity_revision=1,
    )
    values.update(changes)
    value = UsageMeasurementProposal(**values)
    return make_ledger_entry(
        id=uuid4(),
        event_id=uuid4(),
        node_id=value.node_id,
        revision=previous.revision + 1 if previous else 1,
        previous_fingerprint=previous.fingerprint if previous else None,
        actor="system:lyra",
        request_id="fixture-recorded-report",
        recorded_at=BASE + timedelta(seconds=120),
        details=PostedMeasurement(
            measurement=value,
            configuration=UsagePostingConfiguration(
                source_ref="fixture-recording-context",
                recording_start_at=BASE,
                timing_fingerprint=value.timing_fingerprint,
            ),
        ),
    )


def reversed_measurement(target, *, previous=None, **changes):
    previous = previous or target
    details = ReversedMeasurement(
        target_id=target.id,
        target_fingerprint=target.fingerprint,
        measurement=MeasuredWindow.model_validate(
            target.details.measurement.model_dump(include=set(MeasuredWindow.model_fields))
        ),
        reason="fixture reviewed correction",
    ).model_copy(update=changes)
    return make_ledger_entry(
        id=uuid4(),
        event_id=uuid4(),
        node_id=target.node_id,
        revision=previous.revision + 1,
        previous_fingerprint=previous.fingerprint,
        actor=str(OWNER),
        request_id="fixture-recorded-reversal",
        recorded_at=BASE + timedelta(seconds=121),
        details=details,
    )


def head(entries):
    return UsageLedgerHead(
        node_id="node-a",
        revision=len(entries),
        fingerprint=entries[-1].fingerprint if entries else None,
    )


def query(start=0, end=60, **changes):
    return RecordedUsageQuery(
        node_id="node-a",
        start_at=BASE + timedelta(seconds=start),
        end_at=BASE + timedelta(seconds=end),
        **changes,
    )


def report(entries, *, selection=None, owner=OWNER, at=None):
    return recorded_usage_metrics(
        selection or query(),
        owner_id=owner,
        head=head(entries),
        entries=entries,
        read_at=at or BASE + timedelta(seconds=200),
    )


@pytest.mark.parametrize(
    "start,end,expected", [(0, 60, 30), (20, 30, 10), (25, 50, 15), (0, 10, 0), (40, 50, 0)]
)
def test_reports_intersect_half_open_windows_without_changing_source_measurements(
    start, end, expected
):
    entry = measured()
    result = report((entry,), selection=query(start, end))
    assert result.recorded_microseconds == expected * 1000000
    assert result.source_coverage == "unknown" and result.total_usage_microseconds is None
    assert not result.quota_usage_available and not result.weighted
    assert entry.details.measurement.observed_microseconds == 30000000
    if result.contributions:
        item = result.contributions[0]
        assert item.original_start_at == BASE + timedelta(seconds=10)
        assert item.original_end_at == BASE + timedelta(seconds=40)
        assert item.source_derivation_id == entry.details.measurement.source_derivation_id


def test_exact_microseconds_and_equivalent_timezones_have_a_reproducible_fingerprint():
    entry = measured()
    start = BASE + timedelta(seconds=20, microseconds=1)
    end = start + timedelta(microseconds=1)
    selection = RecordedUsageQuery(node_id="node-a", start_at=start, end_at=end)
    first = report((entry,), selection=selection)
    shifted = selection.model_dump()
    shifted["start_at"] = start.astimezone(timezone(timedelta(hours=8)))
    shifted["end_at"] = end.astimezone(timezone(timedelta(hours=8)))
    second = report(
        (entry,), selection=RecordedUsageQuery(**shifted), at=BASE + timedelta(seconds=210)
    )
    assert first.recorded_microseconds == 1 and first.fingerprint == second.fingerprint
    assert first.read_at != second.read_at


def test_multiple_gpus_sum_residency_once_per_measurement_and_do_not_leak_other_owners():
    one = measured()
    two = measured(previous=one, gpu_uuid="GPU-b")
    other = measured(previous=two, gpu_uuid="GPU-c", owner_id=uuid4())
    result = report((one, two, other))
    assert result.recorded_microseconds == 60000000
    assert {x.gpu_uuid for x in result.contributions} == {"GPU-a", "GPU-b"}
    assert str(other.details.measurement.owner_id) not in result.model_dump_json()
    empty = report((one,), owner=uuid4())
    assert empty.recorded_microseconds == 0 and empty.total_usage_microseconds is None


def test_reversal_changes_current_report_while_the_original_prefix_is_reproducible():
    one = measured()
    two = reversed_measurement(one)
    old = report((one,), selection=query(snapshot=head((one,))))
    current = report((one, two))
    assert old.recorded_microseconds == 30000000 and current.recorded_microseconds == 0
    assert old.fingerprint != current.fingerprint
    assert report(()).total_usage_microseconds is None
    assert report(()).snapshot.revision == 0


@pytest.mark.parametrize("corruption", ["gap", "order", "head", "node", "clock", "fingerprint"])
def test_bad_prefixes_are_unavailable_instead_of_returning_partial_totals(corruption):
    one = measured()
    two = measured(previous=one, start=40, end=70)
    entries = (one, two)
    fixed = head(entries)
    at = BASE + timedelta(seconds=200)
    if corruption == "gap":
        entries = (two,)
    elif corruption == "order":
        entries = (two, one)
    elif corruption == "head":
        fixed = fixed.model_copy(update={"fingerprint": "c" * 64})
    elif corruption == "node":
        fixed = fixed.model_copy(update={"node_id": "node-b"})
    elif corruption == "clock":
        at = BASE + timedelta(seconds=119)
    else:
        entries = (one.model_copy(update={"fingerprint": "c" * 64}), two)
    with pytest.raises(ValueError):
        recorded_usage_metrics(query(), owner_id=OWNER, head=fixed, entries=entries, read_at=at)


@pytest.mark.parametrize("corruption", ["overlap", "repeat", "target", "reversed_twice"])
def test_immutable_history_requires_valid_window_and_reversal_semantics(corruption):
    first = measured()
    if corruption == "overlap":
        entries = (first, measured(previous=first, start=20, end=50, owner_id=uuid4()))
    elif corruption == "repeat":
        entries = (first, measured(previous=first))
    elif corruption == "target":
        entries = (first, reversed_measurement(first, target_fingerprint="f" * 64))
    else:
        second = reversed_measurement(first)
        entries = (first, second, reversed_measurement(first, previous=second))
    with pytest.raises(ValueError):
        report(entries)


def test_query_and_report_cannot_supply_authority_or_fake_complete_usage():
    for changes in (
        {"actor_id": str(OWNER)},
        {"owner_id": str(OWNER)},
        {"complete": True},
        {"as_of": BASE.isoformat()},
        {"snapshot": UsageLedgerHead(node_id="node-b", revision=0)},
        {"end_at": BASE},
        {"end_at": BASE + timedelta(days=367)},
    ):
        with pytest.raises(ValidationError):
            RecordedUsageQuery.model_validate({**query().model_dump(), **changes})
    result = report((measured(),))
    for changes in (
        {"quota_usage_available": True},
        {"source_coverage": "complete"},
        {"total_usage_microseconds": result.recorded_microseconds},
        {"recorded_microseconds": 0},
    ):
        with pytest.raises(ValidationError):
            RecordedUsageMetrics.model_validate({**result.model_dump(), **changes})


def reading_service(state):
    sessions = state[2][2][1]
    owner, other = state[2][2][4]
    ids = {name: str(uuid4()) for name in ("role", "own", "all", "binding")}
    with sessions.begin() as session:
        session.add(Role(id=ids["role"], name="fixture-ledger-reader-" + str(uuid4())))
        session.add_all(
            [
                Permission(id=ids["own"], name="usage.read_own"),
                Permission(id=ids["all"], name="usage.read_all"),
            ]
        )
        session.flush()
        session.add_all(
            [RolePermission(role_id=ids["role"], permission_id=ids[key]) for key in ("own", "all")]
        )
        session.add(
            RoleBinding(
                id=ids["binding"],
                user_id=owner,
                role_id=ids["role"],
                scope_type="node",
                scope_id="node-a",
            )
        )
    return (
        RecordedUsageService(sessions, clock=lambda: state[2][2][5]["now"]),
        ActorContext(actor_id=owner, request_id="fixture-usage-read"),
        other,
        ids,
    )


@pytest.fixture
def readings(ledger):
    receipt = ledger[0].post(POST, ledger[1])
    return ledger, reading_service(ledger), receipt


def test_scoped_service_uses_only_immutable_records_and_never_infers_full_usage(readings):
    state, (service, actor, other, ids), receipt = readings
    before = counts(state)
    result = service.mine(actor, query())
    assert result.snapshot == receipt.head and result.recorded_microseconds == 30000000
    assert result.total_usage_microseconds is None and counts(state) == before
    with service.sessions.begin() as session:
        session.execute(delete(UsageLedgerClaim))
    # A broken current projection does not rewrite a committed historical report.
    assert service.mine(actor, query()).fingerprint == result.fingerprint
    empty = service.for_user(actor, UUID(other), query())
    assert empty.recorded_microseconds == 0 and empty.total_usage_microseconds is None
    with service.sessions.begin() as session:
        session.execute(delete(UsageLedgerState))
    with pytest.raises(DomainError) as error:
        service.mine(actor, query())
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_service_reproduces_the_old_prefix_after_confirmed_reversal(adjustments):
    state = adjustments
    service, actor, _, _ = reading_service(state[0])
    old = service.mine(actor, query())
    confirm(state, pending(state))
    assert service.mine(actor, query()).recorded_microseconds == 0
    historical = service.mine(actor, query(snapshot=old.snapshot))
    assert historical.fingerprint == old.fingerprint
    assert historical.contributions == old.contributions
    assert historical.total_usage_microseconds is None


def test_own_scope_is_not_another_user_or_node_permission(readings):
    state, (service, actor, other, ids), _ = readings
    with service.sessions.begin() as session:
        session.execute(delete(RolePermission).where(RolePermission.permission_id == ids["all"]))
    assert service.mine(actor, query()).owner_id == actor.actor_id
    for call in (
        lambda: service.for_user(actor, UUID(other), query()),
        lambda: service.mine(actor, query().model_copy(update={"node_id": "node-b"})),
        lambda: service.mine(ActorContext(actor_id=other, request_id="fixture-other"), query()),
    ):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == "PERMISSION_DENIED"


def test_permission_is_rechecked_after_loading_the_snapshot(readings, monkeypatch):
    state, (service, actor, _, ids), _ = readings
    original = UsageLedgerRepository.prefix

    def revoked(repo, head):
        rows = original(repo, head)
        repo.session.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))
        return rows

    monkeypatch.setattr(UsageLedgerRepository, "prefix", revoked)
    with pytest.raises(DomainError) as error:
        service.mine(actor, query())
    assert error.value.code == "PERMISSION_DENIED"


def test_uncommitted_forged_and_oversized_snapshots_never_return_a_total(readings, monkeypatch):
    state, (service, actor, _, _), receipt = readings
    for snapshot in (
        receipt.head.model_copy(update={"revision": 2}),
        receipt.head.model_copy(update={"fingerprint": "c" * 64}),
    ):
        with pytest.raises(DomainError) as error:
            service.mine(actor, query(snapshot=snapshot))
        assert error.value.code in {"CONFLICT", "DEPENDENCY_UNAVAILABLE"}
    monkeypatch.setattr("aris.modules.lyra.ledger_repo.MAX_REPORT_BYTES", 1)
    with pytest.raises(DomainError) as error:
        service.mine(actor, query())
    assert error.value.code == "USAGE_REPORT_TOO_LARGE"


def test_read_service_rejects_future_windows_and_expired_python_budget(readings, monkeypatch):
    state, (service, actor, _, _), _ = readings
    with pytest.raises(DomainError) as error:
        service.mine(
            actor,
            RecordedUsageQuery(
                node_id="node-a", start_at=BASE, end_at=service._now() + timedelta(seconds=60)
            ),
        )
    assert error.value.code == "VALIDATION_ERROR"
    ticks = iter((0.0, 4.0))
    monkeypatch.setattr("aris.modules.lyra.recorded_usage_service.monotonic", lambda: next(ticks))
    with pytest.raises(DomainError) as error:
        service.mine(actor, query())
    assert error.value.code == "RETRYABLE_DATABASE_ERROR"


def test_postgresql_report_is_select_only_with_a_bounded_query_count(readings):
    state, (service, actor, _, _), _ = readings
    postgres(state)
    statements = []
    engine = service.sessions.kw["bind"]

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with service.sessions() as session:
            session.execute(text("SET TRANSACTION READ ONLY"))
            result = service.mine_in_transaction(session, actor, query())
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert result.recorded_microseconds == 30000000
    assert len(statements) <= 8
    assert all(
        statement.startswith(("SELECT", "SET TRANSACTION READ ONLY")) for statement in statements
    )
    assert all("usage_ledger_claims" not in statement for statement in statements)


def test_postgresql_uncommitted_revision_is_never_a_report_snapshot(readings):
    state, (service, actor, _, _), _ = readings
    sessions = postgres(state)
    before = service.mine(actor, query(end=100))
    request = post_window(state, start=40, seq=101)
    with sessions.begin() as writer:
        pending_entry = state[0].post_in_transaction(writer, POST, request)
        assert pending_entry.head.revision == 2
        during = service.mine(actor, query(end=100))
        assert during.fingerprint == before.fingerprint
        with pytest.raises(DomainError) as error:
            service.mine(actor, query(end=100, snapshot=pending_entry.head))
        assert error.value.code == "CONFLICT"
    after = service.mine(actor, query(end=100))
    assert after.snapshot.revision == 2 and after.recorded_microseconds == 60000000
    assert (
        service.mine(actor, query(end=100, snapshot=before.snapshot)).fingerprint
        == before.fingerprint
    )


def test_postgresql_concurrent_revocation_is_seen_before_returning_the_report(
    readings, monkeypatch
):
    state, (service, actor, _, ids), _ = readings
    sessions = postgres(state)
    original = UsageLedgerRepository.prefix

    def revoke():
        with sessions.begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.id == ids["binding"]))

    def changed(repo, head):
        rows = original(repo, head)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(revoke).result(timeout=3)
        return rows

    monkeypatch.setattr(UsageLedgerRepository, "prefix", changed)
    with pytest.raises(DomainError) as error:
        service.mine(actor, query())
    assert error.value.code == "PERMISSION_DENIED"


def test_postgresql_locked_ledger_returns_retryable_failure_then_recovers(readings):
    state, (service, actor, _, _), _ = readings
    sessions = postgres(state)
    with sessions.begin() as locker:
        locker.execute(text("LOCK TABLE usage_ledger_entries IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            service.mine(actor, query())
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
    assert service.mine(actor, query()).recorded_microseconds == 30000000
