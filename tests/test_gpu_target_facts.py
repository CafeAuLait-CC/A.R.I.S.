from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text, update

from aris.core.db import build_engine, session_factory
from aris.core.time import utc
from aris.modules.vega.models import GpuDevice, Node
from aris.modules.vega.target_schemas import GpuTargetQuery
from aris.modules.vega.target_service import VegaTargetService

NOW = datetime(2026, 11, 15, 12, tzinfo=UTC)


def node_values(node_id="node-a"):
    return {
        "id": node_id,
        "agent_id": f"agent-{node_id}",
        "hostname": "fixture-host",
        "agent_version": "fixture",
        "last_seq": 12,
        "observed_at": NOW,
        "received_at": NOW,
        "telemetry_at": NOW,
        "telemetry_received_at": NOW,
        "telemetry_seq": 11,
        "collector_status": "ok",
        "collector_health_at": NOW,
    }


@pytest.fixture
def target(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    with sessions.begin() as session:
        session.add(Node(**node_values()))
        session.flush()
        session.add(
            GpuDevice(
                gpu_uuid="GPU-target",
                node_id="node-a",
                index=0,
                model="fixture",
                memory_total_mib=49152,
                memory_used_mib=0,
                utilization_percent=0,
                temperature_c=35,
                present=True,
            )
        )
    try:
        yield VegaTargetService(sessions, settings, clock=lambda: NOW), sessions, engine
    finally:
        engine.dispose()


def test_target_is_a_detached_typed_fact_and_unknown_uuid_is_missing(target):
    service, sessions, _ = target
    fact = service.gpu(GpuTargetQuery(gpu_uuid="GPU-target"))
    assert fact.node_id == "node-a"
    assert fact.gpu_uuid == "GPU-target"
    assert fact.node_status == "ONLINE"
    assert fact.telemetry_status == "CURRENT"
    assert fact.observed_at == NOW
    assert fact.telemetry_seq == 11
    assert "availability" not in fact.model_dump()
    assert "observed_state" not in fact.model_dump()
    assert service.gpu(GpuTargetQuery(gpu_uuid="GPU-unknown")) is None
    assert service.gpu(GpuTargetQuery(gpu_uuid="gpu-target")) is None
    with sessions() as session:
        assert len(session.scalars(select(Node)).all()) == 1


@pytest.mark.parametrize(
    ("node_age", "sample_age", "node_status", "quality"),
    [
        (59.999, 59.999, "ONLINE", "CURRENT"),
        (60, 60, "STALE", "STALE"),
        (180, 180, "OFFLINE", "STALE"),
        (0, 60, "ONLINE", "STALE"),
        (180, 0, "OFFLINE", "CURRENT"),
        (-301, 0, "UNKNOWN", "CURRENT"),
        (0, -301, "ONLINE", "ERROR"),
    ],
)
def test_node_liveness_and_gpu_freshness_are_independent(
    target, node_age, sample_age, node_status, quality
):
    service, sessions, _ = target
    with sessions.begin() as session:
        session.execute(
            update(Node).values(
                observed_at=NOW - timedelta(seconds=node_age),
                telemetry_at=NOW - timedelta(seconds=sample_age),
                telemetry_received_at=NOW,
            )
        )
    fact = service.gpu(GpuTargetQuery(gpu_uuid="GPU-target"))
    assert fact.node_status == node_status
    assert fact.telemetry_status == quality
    assert fact.observed_at == NOW - timedelta(seconds=sample_age)
    # Recent delivery does not refresh the original observation.
    assert fact.received_at == NOW


def test_missing_sample_and_absent_gpu_do_not_disappear_or_become_available(target):
    service, sessions, _ = target
    with sessions.begin() as session:
        session.execute(update(Node).values(telemetry_at=None, telemetry_received_at=None))
        session.execute(update(GpuDevice).values(present=False))
    fact = service.gpu(GpuTargetQuery(gpu_uuid="GPU-target"))
    assert fact.node_id == "node-a"
    assert fact.present is False
    assert fact.telemetry_status == "MISSING"
    assert fact.observed_at is None
    assert fact.received_at is None


@pytest.mark.parametrize(("error_age", "quality"), [(0, "ERROR"), (1, "CURRENT")])
def test_new_collector_error_invalidates_sample_but_old_error_does_not(target, error_age, quality):
    service, sessions, _ = target
    with sessions.begin() as session:
        session.execute(
            update(Node).values(
                collector_status="error", collector_health_at=NOW - timedelta(seconds=error_age)
            )
        )
    assert service.gpu(GpuTargetQuery(gpu_uuid="GPU-target")).telemetry_status == quality


def test_fact_query_does_not_flush_commit_or_open_a_second_session(target):
    service, sessions, engine = target
    statements = []

    def record_statement(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        with sessions.begin() as session:
            pending = Node(**node_values("pending"))
            session.add(pending)
            fact = service.gpu_in_transaction(session, GpuTargetQuery(gpu_uuid="GPU-target"))
            assert fact.node_id == "node-a"
            assert pending in session.new
            assert session.in_transaction()
            assert len(statements) == 1
            assert statements[0].lstrip().upper().startswith("SELECT ")
            session.rollback()
        with sessions() as session:
            assert session.get(Node, "pending") is None
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)


def test_scalar_query_does_not_reuse_a_stale_orm_identity(target):
    service, sessions, _ = target
    with sessions() as session:
        cached = session.get(Node, "node-a")
        assert utc(cached.telemetry_at) == NOW
        with sessions.begin() as writer:
            writer.execute(update(Node).values(telemetry_at=NOW - timedelta(minutes=2)))
        fact = service.gpu_in_transaction(session, GpuTargetQuery(gpu_uuid="GPU-target"))
        assert fact.telemetry_status == "STALE"
        assert fact.observed_at == NOW - timedelta(minutes=2)
        # Reading scalar facts also leaves the caller's tracked entities alone.
        assert utc(cached.telemetry_at) == NOW


def test_target_query_rejects_unbounded_or_empty_ids_and_naive_clock(target):
    service, _, _ = target
    for invalid in ("", "x" * 129):
        with pytest.raises(ValidationError):
            GpuTargetQuery(gpu_uuid=invalid)
    service.clock = lambda: NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="timezone"):
        service.gpu(GpuTargetQuery(gpu_uuid="GPU-target"))


def test_target_facts_preserve_instants_in_a_non_utc_database_session(target):
    service, sessions, engine = target
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL timezone-aware results")
    with sessions.begin() as session:
        session.execute(text("SET LOCAL TIME ZONE 'Pacific/Auckland'"))
        fact = service.gpu_in_transaction(session, GpuTargetQuery(gpu_uuid="GPU-target"))
        assert fact.generated_at == fact.observed_at == fact.last_seen_at == NOW
        assert fact.observed_at.utcoffset() == timedelta(0)
        assert fact.node_status == "ONLINE" and fact.telemetry_status == "CURRENT"
        assert session.scalar(text("SHOW TimeZone")) == "Pacific/Auckland"
