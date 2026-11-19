from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import Permission, Role, RoleBinding, RolePermission, User
from aris.core.access.schemas import AccessScope
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.scheduler.models import ScheduledJobRow, ScheduledJobRunRow
from aris.core.scheduler.schemas import ScheduledTask, SchedulerContext, SchedulerLimits, TaskClaim
from aris.core.scheduler.service import SchedulerService, TaskDefinition

NOW = datetime(2026, 11, 19, 9, tzinfo=UTC)
CONTEXT = SchedulerContext(request_id="fixture-scheduled-report")
DEFINITION = TaskDefinition(kind="fixture.report", permission="usage.read_own", scope_type="node")


class Handler:
    def __init__(self, error=None, revoke=False):
        self.calls = 0
        self.error = error
        self.revoke = revoke

    def validate(self, request):
        if request.payload != {"report": "fixture"}:
            raise DomainError("VALIDATION_ERROR", "Unknown report input", 422)

    def execute_in_transaction(self, session, request, deadline):
        self.calls += 1
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor="fixture",
                action="fixture.effect",
                target=str(request.id),
                request_id=CONTEXT.request_id,
                recorded_at=NOW,
                changes={},
            )
        )
        session.flush()
        if self.revoke:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == str(request.owner_id)))
        if self.error:
            raise self.error
        return {"report_id": str(request.id)}


@pytest.fixture
def scheduled(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    owner, role, permission = (str(uuid4()) for _ in range(3))
    with sessions.begin() as session:
        session.add_all(
            [
                User(id=owner, display_name="Synthetic owner", status="active", timezone="UTC"),
                Role(id=role, name="fixture reporter"),
                Permission(id=permission, name="usage.read_own"),
            ]
        )
        session.flush()
        session.add_all(
            [
                RolePermission(role_id=role, permission_id=permission),
                RoleBinding(
                    id=str(uuid4()),
                    user_id=owner,
                    role_id=role,
                    scope_type="node",
                    scope_id="node-a",
                ),
            ]
        )
    clock = [NOW]
    handler = Handler()
    request = ScheduledTask(
        key="fixture/weekly/2026-11-09",
        kind=DEFINITION.kind,
        owner_id=owner,
        scope=AccessScope(type="node", id="node-a"),
        scheduled_for=NOW,
        payload={"report": "fixture"},
    )

    def service(active_handler=handler):
        return SchedulerService(
            sessions,
            ((DEFINITION, active_handler),),
            limits=SchedulerLimits(),
            clock=lambda: clock[0],
        )

    try:
        yield service, request, handler, sessions, clock, engine
    finally:
        engine.dispose()


def effects(sessions):
    with sessions() as session:
        return session.scalar(
            select(func.count())
            .select_from(AuditEntry)
            .where(AuditEntry.action == "fixture.effect")
        )


def test_disabled_scheduler_does_not_open_database():
    def no_database():
        pytest.fail("disabled scheduler accessed database")

    assert SchedulerService(no_database).run_once(CONTEXT).state == "idle"
    assert SchedulerService(no_database, limits=SchedulerLimits()).run_once(CONTEXT).state == "idle"


def test_durable_unique_definition_and_future_deadline(scheduled):
    factory, request, handler, sessions, clock, _ = scheduled
    future = request.model_copy(update={"scheduled_for": NOW + timedelta(minutes=1)})
    first = factory().enqueue(CONTEXT, future)
    assert not first.duplicate
    assert factory().enqueue(CONTEXT, future).duplicate
    with pytest.raises(DomainError, match="different input"):
        factory().enqueue(CONTEXT, request)
    assert factory().run_once(CONTEXT).state == "idle"
    clock[0] = future.scheduled_for
    assert factory().run_once(CONTEXT).state == "completed"
    assert factory().run_once(CONTEXT).state == "idle"
    job = factory().exact(request.id)
    assert job.attempts == 1 and job.result == {"report_id": str(request.id)}
    assert handler.calls == effects(sessions) == 1
    assert [run.state for run in factory().runs(request.id)] == ["completed"]
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(DomainEvent)) == 3
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 3


def test_failed_handler_rolls_back_effect_then_retries_after_backoff(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    failing = Handler(DomainError("DEPENDENCY_UNAVAILABLE", "Synthetic failure", 503))
    service = factory(failing)
    service.enqueue(CONTEXT, request)
    assert service.run_once(CONTEXT).state == "retry"
    assert effects(sessions) == 0
    assert service.run_once(CONTEXT).state == "idle"
    clock[0] += timedelta(seconds=5)
    assert factory().run_once(CONTEXT).state == "completed"
    assert [run.state for run in service.runs(request.id)] == ["retry", "completed"]
    assert effects(sessions) == 1


def test_retry_exhaustion_is_terminal_with_attempt_history(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    service = factory(Handler(RuntimeError("do not persist raw error text")))
    service.enqueue(CONTEXT, request)
    for expected, advance in (("retry", 5), ("retry", 10), ("dead", 0)):
        assert service.run_once(CONTEXT).state == expected
        clock[0] += timedelta(seconds=advance)
    assert service.run_once(CONTEXT).state == "idle" and effects(sessions) == 0
    assert [run.error_code for run in service.runs(request.id)] == ["HANDLER_FAILED"] * 3


@pytest.mark.parametrize("during", [False, True])
def test_current_permission_is_checked_before_and_after_handler(scheduled, during):
    factory, request, _, sessions, _, _ = scheduled
    handler = Handler(revoke=during)
    service = factory(handler)
    service.enqueue(CONTEXT, request)
    if not during:
        with sessions.begin() as session:
            session.execute(delete(RoleBinding))
    result = service.run_once(CONTEXT)
    assert result.state == "blocked" and result.error_code == "PERMISSION_DENIED"
    assert effects(sessions) == 0 and handler.calls == int(during)


def test_expired_claim_is_fenced_and_recovered_after_process_restart(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    first = factory()
    first.enqueue(CONTEXT, request)
    old = first.claim(CONTEXT)
    assert isinstance(old, TaskClaim)
    clock[0] = old.expires_at
    restarted = factory()
    new = restarted.claim(CONTEXT)
    assert isinstance(new, TaskClaim) and new.lease_token != old.lease_token
    assert first.execute(CONTEXT, old).state == "lost_claim"
    assert restarted.execute(CONTEXT, new).state == "completed"
    assert [run.state for run in restarted.runs(request.id)] == ["expired", "completed"]
    assert effects(sessions) == 1


def test_unknown_registered_kind_is_left_for_its_worker(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    factory().enqueue(CONTEXT, request)
    empty = SchedulerService(sessions, limits=SchedulerLimits(), clock=lambda: clock[0])
    assert empty.run_once(CONTEXT).state == "idle"
    assert factory().exact(request.id).state == "pending"


def test_definitions_terminal_attempts_and_nonempty_history_are_protected(scheduled):
    factory, request, _, sessions, _, _ = scheduled
    service = factory()
    service.enqueue(CONTEXT, request)
    with pytest.raises(DBAPIError), sessions.begin() as session:
        session.execute(update(ScheduledJobRow).values(job_key="rewritten"))
    service.run_once(CONTEXT)
    with pytest.raises(DBAPIError), sessions.begin() as session:
        session.execute(update(ScheduledJobRunRow).values(error_code="rewritten"))
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0026")
    assert service.exact(request.id).state == "completed"


def test_postgres_workers_enqueue_once_and_claim_distinct_jobs(scheduled, database_url):
    factory, request, _, sessions, _, _ = scheduled
    if not database_url.startswith("postgresql"):
        pytest.skip("requires PostgreSQL advisory and row locks")
    barrier = Barrier(2)

    def enqueue(_):
        barrier.wait(timeout=5)
        return factory().enqueue(CONTEXT, request)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(enqueue, range(2)))
    assert sorted(result.duplicate for result in results) == [False, True]
    second = request.model_copy(update={"key": request.key + "/second"})
    factory().enqueue(CONTEXT, second)

    def claim(_):
        barrier.wait(timeout=5)
        return factory().claim(CONTEXT)

    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert all(isinstance(value, TaskClaim) for value in claims)
    assert {value.job_id for value in claims} == {request.id, second.id}
    for value in claims:
        assert factory().execute(CONTEXT, value).state == "completed"
    assert effects(sessions) == 2


def test_empty_scheduler_and_report_migration_round_trip(settings):
    command.downgrade(Config("alembic.ini"), "0026")
    command.upgrade(Config("alembic.ini"), "head")
    command.check(Config("alembic.ini"))


def test_repeated_abandoned_claims_stop_at_attempt_limit(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    service = factory()
    service.enqueue(CONTEXT, request)
    for _ in range(3):
        claim = service.claim(CONTEXT)
        assert isinstance(claim, TaskClaim)
        clock[0] = claim.expires_at
    assert service.run_once(CONTEXT).state == "dead"
    assert [run.state for run in service.runs(request.id)] == ["expired"] * 3
    assert service.exact(request.id).error_code == "ATTEMPTS_EXHAUSTED"
    assert service.run_once(CONTEXT).state == "idle" and effects(sessions) == 0


def test_reducing_retry_limit_keeps_the_last_attempt_history_intact(scheduled):
    factory, request, _, sessions, clock, _ = scheduled
    service = factory(Handler(RuntimeError("synthetic")))
    service.enqueue(CONTEXT, request)
    assert service.run_once(CONTEXT).state == "retry"
    clock[0] += timedelta(seconds=5)
    reduced = SchedulerService(
        sessions,
        ((DEFINITION, Handler()),),
        limits=SchedulerLimits(max_attempts=1),
        clock=lambda: clock[0],
    )
    assert reduced.run_once(CONTEXT).state == "dead"
    assert reduced.exact(request.id).attempts == 1
    assert [run.state for run in reduced.runs(request.id)] == ["retry"]
