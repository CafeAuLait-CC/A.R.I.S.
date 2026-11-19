from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, text, update

from aris.core.contracts import DomainError
from aris.modules.vega.gpu_hardware_schemas import GpuHardwareBatchQuery, GpuHardwareObservation
from aris.modules.vega.gpu_hardware_service import VegaGpuHardwareObservations
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.repo import VegaRepository
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_archive import archive, pure_page, query, seed


def setup(client):
    seed(client)
    source = archive(client)
    page = source.page(query())
    wanted = GpuHardwareBatchQuery(
        references=tuple(item.reference for item in page.items if item.observation)
    )
    return source, VegaGpuHardwareObservations(source.sessions), wanted, page


def test_original_gpu_components_remain_separate_from_compute_time_and_current_inventory(client):
    base = datetime.now(UTC) - timedelta(seconds=60)
    first = resource_batch(seq=1, observed_at=base, compute_age=10)
    first["resources"]["gpu"]["observed_at"] = (base - timedelta(seconds=5)).isoformat()
    first["resources"]["gpu"]["gpus"][0].update(
        utilization_percent=67, memory_used_mib=1234, temperature_c=None
    )
    assert put(client, first).status_code == 200
    source = archive(client)
    old = source.page(query(through_seq=1))
    request = GpuHardwareBatchQuery(references=(old.items[0].reference,))
    reader = VegaGpuHardwareObservations(source.sessions)
    result = reader.batch(request)
    value = result.observations[0]
    assert value.gpu_observed_at == base - timedelta(seconds=5)
    assert value.gpu_observed_at != old.items[0].observation.compute_observed_at
    assert value.gpus[0].utilization_percent == 67 and value.gpus[0].memory_used_mib == 1234
    assert value.gpus[0].temperature_c is None
    assert not hasattr(value, "owners") and "username" not in result.model_dump_json()
    current = resource_batch(seq=2)
    current["resources"]["gpu"]["gpus"][0].update(
        model="different current GPU", memory_used_mib=1, temperature_c=0
    )
    assert put(client, current).status_code == 200
    assert reader.batch(request) == result
    assert source.page(query(through_seq=1)).fingerprint == old.fingerprint
    assert put(client, resource_batch(seq=3, gpu_quality="error")).status_code == 200
    error = source.page(query(after_seq=2, through_seq=3)).items[0]
    failed = reader.batch(GpuHardwareBatchQuery(references=(error.reference,))).observations[0]
    assert failed.gpu_quality == "error" and failed.gpus == ()


def test_hardware_query_requires_ordered_bounded_telemetry_and_original_components():
    reference = pure_page().items[0].reference
    for values in (
        (),
        (reference, reference),
        (reference.model_copy(update={"kind": "heartbeat"}),),
        (
            reference,
            reference.model_copy(update={"seq": 2, "message_id": uuid4(), "node_id": "another"}),
        ),
        (reference, reference.model_copy(update={"seq": 8194, "message_id": uuid4()})),
        tuple(
            reference.model_copy(update={"seq": i + 1, "message_id": uuid4()}) for i in range(129)
        ),
    ):
        with pytest.raises(ValidationError):
            GpuHardwareBatchQuery(references=values)
    with pytest.raises(ValidationError):
        GpuHardwareBatchQuery(references=(reference,), source_character_budget=True)
    with pytest.raises(ValidationError):
        GpuHardwareObservation(
            reference=reference,
            gpu_observed_at=reference.observed_at + timedelta(seconds=1),
            gpu_quality="ok",
            gpus=(),
        )


def test_hardware_read_rejects_missing_or_changed_full_source_references_and_corrupt_originals(
    client,
):
    source, reader, wanted, _ = setup(client)
    first = wanted.references[0]
    for changes in (
        {"fingerprint": "a" * 64},
        {"received_at": first.received_at + timedelta(seconds=1)},
        {"observed_at": first.observed_at - timedelta(seconds=1)},
        {"seq": first.seq + 1},
        {"node_id": "other"},
        {"agent_id": "other"},
    ):
        with pytest.raises(DomainError) as error:
            reader.batch(GpuHardwareBatchQuery(references=(first.model_copy(update=changes),)))
        assert error.value.code == "CONFLICT"
    with pytest.raises(DomainError) as error:
        reader.batch(
            GpuHardwareBatchQuery(references=(first.model_copy(update={"message_id": uuid4()}),))
        )
    assert error.value.code == "NOT_FOUND"
    with source.sessions.begin() as db:
        db.execute(
            update(AgentMessage)
            .where(AgentMessage.message_id == str(first.message_id))
            .values(fingerprint="f" * 64)
        )
    with pytest.raises(DomainError) as error:
        reader.batch(wanted)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_hardware_read_checks_body_budget_first_and_fetches_a_batch_with_constant_queries(
    client, monkeypatch
):
    source, reader, wanted, _ = setup(client)

    def forbidden(*args, **kwargs):
        raise AssertionError("over-budget source bodies were loaded")

    with monkeypatch.context() as patch:
        patch.setattr(VegaRepository, "usage_messages", forbidden)
        with pytest.raises(DomainError) as error:
            reader.batch(wanted.model_copy(update={"source_character_budget": 1}))
        assert error.value.code == "USAGE_SOURCE_TOO_LARGE"
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement)

    engine = client.app.state.engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        with source.sessions() as db:
            value = reader.batch_in_transaction(db, wanted)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(value.observations) == 3 and len(statements) == 3
    assert all(s.startswith("SELECT") for s in statements)
    assert not any(
        "gpu_devices" in s or "node_resources" in s or "node_user" in s for s in statements
    )


def test_postgresql_original_hardware_read_is_read_only_and_recovers_after_source_lock(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires real PostgreSQL source read and lock")
    source, reader, wanted, _ = setup(client)
    with source.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        assert db.scalar(text("SHOW transaction_read_only")) == "on"
        expected = reader.batch_in_transaction(db, wanted)
    with source.sessions() as blocker:
        blocker.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            reader.batch(wanted)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert reader.batch(wanted).fingerprint == expected.fingerprint
