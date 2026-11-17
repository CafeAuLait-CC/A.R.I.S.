import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.modules.vega.models import AgentMessage, GpuDevice
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_schemas import GpuUsageFact, UsageObservationQuery
from aris.modules.vega.usage_service import VegaUsageObservations, _usage_fact
from tests.test_heartbeat import beat, heartbeat
from tests.test_resource_telemetry import put, resource_batch


def query(payload, **changes):
    return UsageObservationQuery(
        agent_id=payload["agent_id"],
        node_id=payload["node_id"],
        message_id=payload["message_id"],
        expected_seq=payload["seq"],
        **changes,
    )


def service(client):
    return VegaUsageObservations(sessionmaker(client.app.state.engine, expire_on_commit=False))


def owner(uid=1001, username="fixture-user", gpus=None):
    return {
        "uid": uid,
        "username": username,
        "reported_processes": 1,
        "gpus": gpus or [{"gpu_uuid": "GPU-a", "reported_processes": 1}],
    }


def ownership(payload, users, *, quality="ok", unresolved=0):
    payload["resources"]["compute"].update(
        user_scope="reported_linux_users",
        ownership={"quality": quality, "users": users, "unresolved_processes": unresolved},
    )
    return payload


def projection(payload):
    batch = ResourceTelemetryBatch.model_validate(payload)
    fingerprint = hashlib.sha256(
        json.dumps(batch.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return _usage_fact(batch, fingerprint=fingerprint, received_at=datetime.now(UTC))


def test_historical_source_time_and_identity_do_not_follow_current_inventory_or_heartbeat(client):
    old_time = datetime.now(UTC) - timedelta(minutes=10)
    payload = ownership(resource_batch(observed_at=old_time, compute_age=7), [owner()])
    assert put(client, payload).status_code == 200
    original = service(client).observation(query(payload))
    assert original.assessment_scope == "historical_observation"
    assert original.compute_observed_at == old_time - timedelta(seconds=7)
    assert original.gpus[0].owners[0].username == "fixture-user"
    assert beat(client, heartbeat(seq=2)).status_code == 200
    current = resource_batch(seq=3)
    current["resources"]["gpu"]["gpus"][0]["model"] = "Changed current model"
    assert put(client, current).status_code == 200
    again = service(client).observation(query(payload))
    assert again == original
    assert again.gpus[0].model != "Changed current model"
    assert again.observed_at == old_time


def test_original_cached_compute_time_is_preserved_across_different_envelopes(client):
    base = datetime.now(UTC) - timedelta(seconds=20)
    first = resource_batch(observed_at=base)
    second = resource_batch(seq=2, observed_at=base + timedelta(seconds=15), compute_age=15)
    assert put(client, first).status_code == put(client, second).status_code == 200
    a, b = service(client).observation(query(first)), service(client).observation(query(second))
    assert a.seq != b.seq and a.message_id != b.message_id
    assert a.compute_observed_at == b.compute_observed_at == base
    assert b.observed_at > a.observed_at


def test_heartbeat_and_missing_messages_never_become_idle_usage_samples(client):
    payload = heartbeat(seq=1)
    assert beat(client, payload).status_code == 200
    assert service(client).observation(query(payload)) is None
    assert service(client).observation(query({**payload, "message_id": str(uuid4())})) is None


@pytest.mark.parametrize("change", [{"node_id": "other-node"}, {"expected_seq": 2}])
def test_lookup_rejects_node_or_sequence_binding_mismatch(client, change):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    lookup = query(payload).model_copy(update=change)
    with pytest.raises(DomainError) as error:
        service(client).observation(lookup)
    assert error.value.code == "CONFLICT"


@pytest.mark.parametrize(
    "corruption", ["payload", "fingerprint", "stored_time", "schema_with_new_hash"]
)
def test_corrupted_historical_sources_fail_instead_of_returning_zero(client, corruption):
    payload = resource_batch()
    assert put(client, payload).status_code == 200
    with Session(client.app.state.engine) as session, session.begin():
        row = session.scalar(select(AgentMessage))
        if corruption == "fingerprint":
            row.fingerprint = "0" * 64
        elif corruption == "stored_time":
            row.observed_at += timedelta(seconds=1)
        else:
            value = copy.deepcopy(row.payload)
            value["resources"]["compute"]["reported_processes"] = 999
            if corruption == "schema_with_new_hash":
                value["resources"]["compute"]["quality"] = "made-up-quality"
                row.fingerprint = hashlib.sha256(
                    json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            row.payload = value
    with pytest.raises(DomainError) as error:
        service(client).observation(query(payload))
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_no_autoflush_or_dirty_orm_current_state_can_replace_original_scalar_facts(client):
    payload = ownership(resource_batch(), [owner()])
    assert put(client, payload).status_code == 200
    observations = service(client)
    original = observations.observation(query(payload))
    with observations.sessions() as session:
        row = session.scalar(select(AgentMessage))
        gpu = session.scalar(select(GpuDevice))
        row.payload = {"uncommitted": "must not leak into historical query"}
        row.fingerprint = "f" * 64
        gpu.model = "Uncommitted current label"

        def reject_flush(*args):
            raise AssertionError("usage query must not flush caller state")

        event.listen(session, "before_flush", reject_flush)
        try:
            assert observations.observation_in_transaction(session, query(payload)) == original
        finally:
            event.remove(session, "before_flush", reject_flush)
        session.rollback()
    assert observations.observation(query(payload)) == original


def test_explicit_zero_driver_processes_is_known_without_requiring_owner_names():
    payload = resource_batch()
    payload["resources"]["compute"].update(reported_processes=0, gpus=[])
    result = projection(payload)
    assert result.compute_counts_consistent and result.gpus[0].reported_processes == 0
    assert result.gpus[0].ownership_quality == "complete" and result.gpus[0].owners == ()


@pytest.mark.parametrize("quality", ["unknown", "error", "unsupported"])
def test_unavailable_compute_is_unknown_with_no_fake_zero_counts(quality):
    payload = resource_batch()
    payload["resources"]["compute"].update(quality=quality, reported_processes=None, gpus=[])
    result = projection(payload)
    assert not result.compute_counts_consistent
    assert (
        result.gpus[0].reported_processes is None and result.gpus[0].ownership_quality == "unknown"
    )


def test_inconsistent_global_coverage_is_unknown_even_when_raw_schema_accepts_it():
    payload = resource_batch()
    payload["resources"]["compute"]["gpus"] = []
    result = projection(payload)
    assert not result.compute_counts_consistent and result.gpus[0].reported_processes is None


def test_missing_ownership_keeps_known_occupied_count_and_unattributed_processes():
    result = projection(resource_batch())
    gpu = result.gpus[0]
    assert gpu.reported_processes == gpu.unattributed_processes == 1
    assert gpu.ownership_quality == "partial" and gpu.owners == ()


def test_per_gpu_owner_coverage_is_checked_even_if_global_ownership_says_ok():
    payload = ownership(resource_batch(), [owner()])
    payload["resources"]["gpu"]["gpus"].append(
        {**payload["resources"]["gpu"]["gpus"][0], "gpu_uuid": "GPU-b", "index": 1}
    )
    payload["resources"]["compute"]["gpus"].append({"gpu_uuid": "GPU-b", "reported_processes": 1})
    result = projection(payload)
    a, b = result.gpus
    assert a.ownership_quality == "complete" and a.unattributed_processes == 0
    assert b.ownership_quality == "partial" and b.unattributed_processes == 1


def test_multiple_uids_and_unresolved_names_remain_facts_without_platform_identity_or_charges():
    payload = ownership(resource_batch(), [owner(), owner(1002, None)], quality="partial")
    payload["resources"]["compute"].update(
        reported_processes=2, gpus=[{"gpu_uuid": "GPU-a", "reported_processes": 2}]
    )
    result = projection(payload)
    gpu = result.gpus[0]
    assert [o.uid for o in gpu.owners] == [1001, 1002]
    assert gpu.owners[1].username is None and gpu.unattributed_processes == 0
    assert gpu.ownership_quality == "partial"
    assert all(
        key not in result.model_dump() for key in ("user_id", "gpu_seconds", "reservation_id")
    )


def test_compute_gpu_outside_independent_inventory_is_preserved_with_unknown_label():
    payload = ownership(resource_batch(gpu_quality="error"), [owner()])
    result = projection(payload)
    assert len(result.gpus) == 1 and result.gpus[0].gpu_uuid == "GPU-a"
    assert not result.gpus[0].inventory_present
    assert result.gpus[0].model is None and result.gpus[0].index is None
    assert result.gpus[0].reported_processes == 1


def test_historical_fact_contract_rejects_inconsistent_quality_and_partial_inventory():
    for changes in (
        {"model": "partial label"},
        {"ownership_quality": "complete"},
        {"unattributed_processes": 0},
    ):
        with pytest.raises(ValidationError):
            GpuUsageFact.model_validate(
                {
                    "gpu_uuid": "GPU-a",
                    "inventory_present": False,
                    "reported_processes": 1,
                    "unattributed_processes": 1,
                    "ownership_quality": "partial",
                    **changes,
                }
            )


def test_historical_lookup_obeys_postgresql_read_only_transaction(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY enforcement")
    payload = ownership(resource_batch(), [owner()])
    assert put(client, payload).status_code == 200
    observations = service(client)
    with observations.sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        result = observations.observation_in_transaction(session, query(payload))
        assert result.gpus[0].owners[0].uid == 1001
