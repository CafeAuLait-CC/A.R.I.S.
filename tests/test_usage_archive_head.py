import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event, insert, select, text, update
from sqlalchemy.orm import sessionmaker

from aris.core.contracts import DomainError
from aris.modules.vega.models import AgentMessage, Node
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_archive_schemas import (
    UsageArchiveHead,
    UsageArchiveHeadQuery,
    UsageArchiveQuery,
    archive_head_fingerprint,
)
from aris.modules.vega.usage_archive_service import VegaUsageArchive
from tests.test_heartbeat import beat, heartbeat
from tests.test_resource_telemetry import put, resource_batch

QUERY = UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")


def archive(client):
    return VegaUsageArchive(sessionmaker(client.app.state.engine, expire_on_commit=False))


def test_source_ceiling_contract_cannot_claim_content_or_continuity_authority():
    values = {
        "query": QUERY,
        "highest_committed_seq": 3,
        "read_at": datetime.now(UTC),
        "fingerprint": "0" * 64,
    }
    unchecked = UsageArchiveHead.model_construct(**values)
    values["fingerprint"] = archive_head_fingerprint(unchecked)
    head = UsageArchiveHead.model_validate(values)
    later = UsageArchiveHead.model_validate(
        {**values, "read_at": values["read_at"] + timedelta(seconds=1)}
    )
    assert later.fingerprint == head.fingerprint and head.sequence_coverage == "not_assessed"
    for changes in (
        {"highest_committed_seq": 4},
        {"payload_integrity": "verified"},
        {"quota_usage_available": True},
    ):
        with pytest.raises(ValidationError):
            UsageArchiveHead.model_validate({**values, **changes})


def test_high_ceiling_does_not_skip_a_gap_and_same_ceiling_can_gain_missing_messages(client):
    assert put(client, resource_batch(seq=3)).status_code == 200
    service = archive(client)
    before = service.head(QUERY)
    assert before.highest_committed_seq == 3 and not before.quota_usage_available
    query = UsageArchiveQuery(agent_id="agent-a", node_id="node-a", after_seq=0, through_seq=3)
    missing = service.page(query)
    assert missing.state == "gap" and missing.next_after_seq == 0 and not missing.items
    assert (
        put(client, resource_batch(seq=1)).status_code
        == put(client, resource_batch(seq=2)).status_code
        == 200
    )
    after = service.head(QUERY)
    assert after.fingerprint == before.fingerprint
    complete = service.page(query)
    assert complete.state == "range_end" and tuple(x.reference.seq for x in complete.items) == (
        1,
        2,
        3,
    )


def test_unknown_binding_is_not_empty_and_heartbeat_only_is_not_gpu_usage(client):
    service = archive(client)
    with pytest.raises(DomainError) as error:
        service.head(QUERY)
    assert error.value.code == "NOT_FOUND"
    with service.sessions.begin() as session:
        session.add(
            Node(
                id="node-a",
                agent_id="agent-a",
                hostname="fixture.invalid",
                agent_version="fixture",
                last_seq=0,
                observed_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
            )
        )
    assert service.head(QUERY).highest_committed_seq == 0
    with pytest.raises(DomainError) as error:
        service.head(QUERY.model_copy(update={"node_id": "other-node"}))
    assert error.value.code == "CONFLICT"
    assert beat(client, heartbeat(seq=1)).status_code == 200
    assert service.head(QUERY).highest_committed_seq == 1
    page = service.page(
        UsageArchiveQuery(agent_id="agent-a", node_id="node-a", after_seq=0, through_seq=1)
    )
    assert page.items[0].observation is None


def test_projection_mismatch_or_lost_originals_never_become_an_empty_source(client):
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    with service.sessions.begin() as session:
        session.execute(update(Node).values(last_seq=9))
    with pytest.raises(DomainError) as error:
        service.head(QUERY)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"
    with service.sessions.begin() as session:
        session.execute(update(Node).values(last_seq=1))
        session.execute(delete(AgentMessage))
    with pytest.raises(DomainError) as error:
        service.head(QUERY)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_head_is_metadata_only_and_does_not_replace_original_payload_verification(client):
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    with service.sessions.begin() as session:
        row = session.scalar(select(AgentMessage))
        row.fingerprint = "f" * 64
    head = service.head(QUERY)
    assert head.highest_committed_seq == 1 and head.payload_integrity == "not_assessed"
    with pytest.raises(DomainError):
        service.page(
            UsageArchiveQuery(agent_id="agent-a", node_id="node-a", after_seq=0, through_seq=1)
        )


def test_head_reads_one_scalar_statement_without_loading_bodies_or_flushing_pending_orm(client):
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    engine = client.app.state.engine
    statements = []

    def capture(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    def reject(*args):
        raise AssertionError("head must not flush pending owner work")

    with service.sessions() as session:
        node = session.get(Node, "node-a")
        node.agent_id = "pending-identity"
        node.last_seq = 999
        event.listen(session, "before_flush", reject)
        event.listen(engine, "before_cursor_execute", capture)
        try:
            head = service.head_in_transaction(session, QUERY)
        finally:
            event.remove(session, "before_flush", reject)
            event.remove(engine, "before_cursor_execute", capture)
    assert head.highest_committed_seq == 1 and len(statements) == 1
    assert statements[0].startswith("SELECT") and "agent_messages.payload" not in statements[0]


def test_postgresql_head_preserves_read_only_owner_budgets(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL source head read-only budget")
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    with service.sessions() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        session.execute(text("SET LOCAL transaction_timeout='2s'"))
        session.execute(text("SET LOCAL statement_timeout='750ms'"))
        assert service.head_in_transaction(session, QUERY).highest_committed_seq == 1
        assert session.scalar(text("SHOW transaction_timeout")) == "2s"
        assert session.scalar(text("SHOW statement_timeout")) == "750ms"


def test_postgresql_uncommitted_source_and_node_ceiling_are_not_visible(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL committed source ceiling")
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    batch = ResourceTelemetryBatch.model_validate(resource_batch(seq=3))
    payload = batch.model_dump(mode="json")
    with service.sessions() as writer:
        writer.execute(update(Node).where(Node.id == "node-a").values(last_seq=3))
        writer.execute(
            insert(AgentMessage).values(
                id=str(uuid4()),
                agent_id=batch.agent_id,
                node_id=batch.node_id,
                message_id=str(batch.message_id),
                seq=3,
                message_kind="telemetry",
                observed_at=batch.observed_at,
                received_at=datetime.now(UTC),
                fingerprint=hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
                applied_to_current=False,
                payload=payload,
            )
        )
        assert service.head(QUERY).highest_committed_seq == 1
        writer.commit()
    assert service.head(QUERY).highest_committed_seq == 3
    page = service.page(
        UsageArchiveQuery(agent_id="agent-a", node_id="node-a", after_seq=1, through_seq=3)
    )
    assert page.state == "gap" and page.next_after_seq == 1


def test_postgresql_busy_source_head_returns_retryable_and_recovers_after_lock_release(client):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL bounded head source lock")
    assert put(client, resource_batch(seq=1)).status_code == 200
    service = archive(client)
    with service.sessions() as blocker:
        blocker.execute(text("LOCK TABLE agent_messages IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            service.head(QUERY)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert service.head(QUERY).highest_committed_seq == 1
