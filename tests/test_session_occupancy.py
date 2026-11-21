import copy
import csv
import io
from datetime import timedelta
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.orm import sessionmaker

from aris.apps.api.main import create_app
from aris.core.contracts import DomainError
from aris.interfaces.slack.config import SlackSettings
from aris.modules.lyra.continuous_usage import continuous_usage_history
from aris.modules.lyra.session_occupancy import assess_gap_sources
from aris.modules.lyra.session_occupancy_service import SessionOccupancyService
from aris.modules.lyra.usage_history import UsageHistoryQuery
from aris.modules.vega.models import AgentMessage, Node
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference
from aris.modules.vega.usage_span import (
    UsageSourceSpan,
    UsageSourceSpanQuery,
    VegaUsageSpans,
    span_fingerprint,
)
from tests.test_continuous_usage import contexts
from tests.test_heartbeat import beat, heartbeat
from tests.test_historical_usage_facts import owner, ownership, projection
from tests.test_recorded_usage import BASE, measured
from tests.test_resource_telemetry import put, resource_batch
from tests.test_usage_history import (
    TOKEN,
    admin_state,
    derivations,
    history_state,
    ledger,
    readings,
    reduce,
    surface_for,
)
from tests.test_usage_ledger import POST, counts, post_window

__all__ = ["admin_state", "derivations", "history_state", "ledger", "readings"]


def frame(seconds, seq, *, idle=False, username="linux-user", uid=1001):
    payload = ownership(
        resource_batch(seq=seq, observed_at=BASE + timedelta(seconds=seconds)),
        [] if idle else [owner(uid=uid, username=username)],
    )
    if idle:
        payload["resources"]["compute"].update(reported_processes=0, gpus=[])
    return payload


def evidence(payloads, *, healthy=True):
    facts = tuple(projection(p) for p in payloads)
    refs = tuple(
        ArchivedUsageReference(
            agent_id=f.agent_id,
            node_id=f.node_id,
            message_id=f.message_id,
            seq=f.seq,
            kind="telemetry",
            fingerprint=f.source_fingerprint,
            observed_at=f.observed_at,
            received_at=f.received_at,
        )
        for f in facts
    )
    value = UsageSourceSpan.model_construct(
        query=UsageSourceSpanQuery(
            agent_id=facts[0].agent_id,
            node_id=facts[0].node_id,
            first_seq=refs[0].seq,
            last_seq=refs[-1].seq,
            as_of=max(f.received_at for f in facts) + timedelta(seconds=200),
        ),
        references=refs,
        observations=facts,
        healthy_heartbeats=healthy,
        source_characters=0,
        fingerprint="0" * 64,
    )
    return UsageSourceSpan.model_validate(
        {**value.model_dump(), "fingerprint": span_fingerprint(value)}
    )


def test_verified_retention_requires_idle_source_continuity_and_exclusive_known_owner():
    a = measured(start=0, end=30, model="NVIDIA RTX A6000")
    b = measured(previous=a, start=60, end=90, model="NVIDIA RTX A6000")
    view = continuous_usage_history(reduce((a, b)), contexts((a, b)))
    gap = view.gaps[0]
    good = [frame(30, 1), frame(45, 2, idle=True), frame(60, 3)]
    assert assess_gap_sources(gap, evidence(good)).state == "retained"
    assert view.history.recorded_microseconds == 60000000
    assert not view.history.quota_usage_available
    cases = [
        (
            [good[0], frame(45, 2, uid=2000, username="other"), good[2]],
            "excluded",
            "OTHER_OWNER_OBSERVED",
        ),
        ([good[0], frame(45, 2), good[2]], "unknown", "NO_IDLE_SAMPLE_REQUIRES_REVIEW"),
        (
            [good[0], good[1], frame(60, 3, username="renamed")],
            "excluded",
            "ENDPOINT_OWNER_CHANGED",
        ),
    ]
    for payloads, state, reason in cases:
        result = assess_gap_sources(gap, evidence(payloads))
        assert (result.state, result.reason) == (state, reason)
    assert assess_gap_sources(gap, evidence(good, healthy=False)).reason == "COLLECTOR_UNHEALTHY"
    conflict = frame(30, 2, idle=True)
    assert (
        assess_gap_sources(gap, evidence([good[0], conflict, good[2]])).reason
        == "CONFLICTING_CACHED_OBSERVATION"
    )
    unknown = copy.deepcopy(good)
    unknown[1]["resources"]["compute"].update(
        quality="error", reported_processes=None, gpus=[], ownership=None, user_scope="unknown"
    )
    assert assess_gap_sources(gap, evidence(unknown)).reason == "SOURCE_QUALITY_UNKNOWN"
    c = measured(previous=a, start=90, end=100, model="NVIDIA RTX A6000")
    boundary = continuous_usage_history(reduce((a, c)), contexts((a, c))).gaps[0]
    assert (
        assess_gap_sources(
            boundary, evidence([frame(30, 1), frame(60, 2, idle=True), frame(90, 3)])
        ).state
        == "retained"
    )
    assert (
        assess_gap_sources(
            boundary, evidence([frame(30, 1), frame(31, 2, idle=True), frame(90, 3)])
        ).reason
        == "OBSERVATION_GAP_OR_CLOCK_REGRESSION"
    )
    with pytest.raises(ValidationError):
        UsageSourceSpan.model_validate({**evidence(good).model_dump(), "fingerprint": "f" * 64})


def test_source_span_verifies_original_heartbeats_missing_sequences_and_cutoff(client):
    assert put(client, frame(30, 1)).status_code == 200
    hb = heartbeat(seq=2, observed_at=(BASE + timedelta(seconds=35)).isoformat())
    assert beat(client, hb).status_code == 200
    assert put(client, frame(45, 3, idle=True)).status_code == 200
    assert put(client, frame(60, 4)).status_code == 200
    sessions = sessionmaker(client.app.state.engine, expire_on_commit=False)
    reader = VegaUsageSpans()
    query = UsageSourceSpanQuery(
        agent_id="agent-a",
        node_id="node-a",
        first_seq=1,
        last_seq=4,
        as_of=BASE + timedelta(days=20),
    )
    with sessions() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        result = reader.span_in_transaction(db, query)
        assert (
            len(result.references) == 4
            and len(result.observations) == 3
            and result.healthy_heartbeats
        )
    with sessions.begin() as db:
        db.execute(update(Node).where(Node.id == "node-a").values(agent_id="new-agent-binding"))
    with sessions() as db:
        assert reader.span_in_transaction(db, query) == result
    with sessions.begin() as db:
        row = db.scalar(select(AgentMessage).where(AgentMessage.seq == 2))
        payload = copy.deepcopy(row.payload)
        payload["collector_status"] = "error"
        import hashlib
        import json

        db.execute(
            update(AgentMessage)
            .where(AgentMessage.id == row.id)
            .values(
                payload=payload,
                fingerprint=hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            )
        )
    with sessions() as db:
        assert not reader.span_in_transaction(db, query).healthy_heartbeats
        with pytest.raises(DomainError):
            reader.span_in_transaction(db, query.model_copy(update={"as_of": BASE}))
    with sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 2))
    with sessions() as db, pytest.raises(DomainError, match="incomplete"):
        reader.span_in_transaction(db, query)


def test_session_quantity_uses_real_ledger_derivations_and_historical_mapping(readings, settings):
    state, _, _ = readings
    assert (
        beat(
            state[4], heartbeat(seq=8, observed_at=(BASE + timedelta(seconds=45)).isoformat())
        ).status_code
        == 200
    )
    assert put(state[4], frame(55, 9, idle=True)).status_code == 200
    assert (
        beat(
            state[4], heartbeat(seq=10, observed_at=(BASE + timedelta(seconds=65)).isoformat())
        ).status_code
        == 200
    )
    state[0].post(POST, post_window(state, start=70, seq=11))
    for start, seq in ((130, 19), (190, 27)):
        assert (
            beat(
                state[4],
                heartbeat(
                    seq=seq - 3, observed_at=(BASE + timedelta(seconds=start - 25)).isoformat()
                ),
            ).status_code
            == 200
        )
        assert put(state[4], frame(start - 15, seq - 2, idle=True)).status_code == 200
        assert (
            beat(
                state[4],
                heartbeat(
                    seq=seq - 1, observed_at=(BASE + timedelta(seconds=start - 5)).isoformat()
                ),
            ).status_code
            == 200
        )
        state[0].post(POST, post_window(state, start=start, seq=seq))
    surface, actor, _ = surface_for(readings, settings)
    identities = state[2][2][-1]
    service = SessionOccupancyService(VegaUsageSpans(), identities)
    surface.session_occupancy = service
    query = UsageHistoryQuery(node_id="node-a", start_at=BASE, end_at=BASE + timedelta(seconds=230))
    before = counts(state)
    with surface.history.sessions() as db:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        view = surface.history.read_continuous_in_transaction(db, actor, query, mode="mine")
        source_queries = []

        def observe(connection, cursor, statement, parameters, context, many):
            if "FROM agent_messages" in statement:
                source_queries.append(statement)

        event.listen(db.get_bind(), "before_cursor_execute", observe)
        try:
            result = service.read_in_transaction(db, view, deadline=monotonic() + 3)
        finally:
            event.remove(db.get_bind(), "before_cursor_execute", observe)
        assert len(view.gaps) == 3 and len(source_queries) <= 2

        assert result.original_recorded_microseconds == 120000000
        assert result.retained_microseconds == 90000000 and result.session_microseconds == 210000000
        assert result.unverified_microseconds == 0 and result.gaps[0].state == "retained"
        assert "linux-user" not in result.model_dump_json() and not result.quota_usage_available
        repeated = service.read_in_transaction(db, view, deadline=monotonic() + 3)
        assert repeated == result
    assert counts(state) == before
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), usage_history=surface)
    ) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        body = dict(
            node_id="node-a",
            mode="mine",
            presentation="continuous",
            include_sessions=True,
            window=dict(
                kind="utc",
                start_at=query.start_at.isoformat(),
                end_at=query.end_at.isoformat(),
                timezone="UTC",
            ),
        )
        response = client.post("/internal/usage/query", headers=headers, json=body)
        assert response.status_code == 200
        page = response.json()
        assert page["session_occupancy"]["session_microseconds"] == 210000000
        fixed = {
            **body,
            "snapshot": page["report"]["snapshot"],
            "session_read_at": page["session_occupancy"]["as_of"],
            "session_identity_revision": page["session_occupancy"]["identity_revision"],
        }
        state[2][2][5]["now"] += timedelta(seconds=60)
        second = client.post("/internal/usage/query", headers=headers, json=fixed)
        assert (
            second.status_code == 200
            and second.json()["session_occupancy"] == page["session_occupancy"]
        )
        exported = client.post("/internal/usage/export", headers=headers, json=fixed)
        assert exported.status_code == 200
        merged = list(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
        assert len(merged) == 1 and int(merged[0]["recorded_microseconds"]) == 120000000
        assert (
            int(merged[0]["retained_microseconds"]) == 90000000
            and int(merged[0]["session_microseconds"]) == 210000000
        )
        raw = client.post(
            "/internal/usage/export",
            headers=headers,
            json={
                **fixed,
                "presentation": "segments",
                "include_sessions": False,
                "session_read_at": None,
                "session_identity_revision": None,
            },
        )
        assert raw.status_code == 200 and "retained_microseconds" not in raw.text
        html = client.post("/internal/usage/report", headers=headers, json=fixed)
        assert html.status_code == 200 and "会话占用" in html.text and "未启用配额扣减" in html.text
        future = {**fixed, "session_identity_revision": 9223372036854775807}
        assert client.post("/internal/usage/query", headers=headers, json=future).status_code == 422
    with surface.history.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 9))
    with surface.history.sessions() as db:
        unverified = service.read_in_transaction(db, view, deadline=monotonic() + 3)
        assert (
            unverified.session_microseconds is None and unverified.retained_microseconds == 60000000
        )
        assert unverified.unverified_microseconds == 30000000
        assert unverified.verified_session_microseconds == 180000000
