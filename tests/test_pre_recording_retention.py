import hashlib
import json
import sys
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from aris.apps.ops import storage
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.lyra.models import UsageDerivation
from aris.modules.lyra.reporting_gap_history_repo import ReportingGapHistoryRepository
from aris.modules.lyra.reporting_gap_history_schemas import ReportingGapScanPosition
from aris.modules.lyra.reporting_gaps import ReportingPoint
from aris.modules.lyra.retention_origin_service import preview_pre_recording_retention
from aris.modules.lyra.retention_repo import RetentionRepository
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConsumeRequest
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery, UsageArchiveQuery
from tests.test_administration import admin_state
from tests.test_mapping_history import history_state
from tests.test_trace_consumer import CONTEXT, configuration, consumer, rebuild, request, seed
from tests.test_usage_derivations import CONTEXT as DERIVATION_CONTEXT
from tests.test_usage_derivations import derivations

__all__ = ["consumer", "admin_state", "history_state", "derivations"]
SOURCE = UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a")


def test_pre_recording_prefix_requires_every_origin_and_saved_health(
    consumer, settings, monkeypatch, capsys
):
    state = consumer
    now = datetime.now(UTC)
    monday = now.date() - timedelta(days=now.weekday())
    cutoff = datetime.combine(monday, time.min, UTC)
    state.base = cutoff - timedelta(days=2)
    state.config = configuration(initial_after_seq=2)
    state.service = rebuild(state)
    seed(state)
    with state.sessions.begin() as db:
        for row in db.scalars(select(AgentMessage).where(AgentMessage.seq <= 2)):
            row.received_at = state.base + timedelta(seconds=60 + row.seq)
            if row.seq == 2:
                row.observed_at = state.base + timedelta(seconds=20)
                row.payload = {**row.payload, "observed_at": row.observed_at.isoformat()}
                row.fingerprint = hashlib.sha256(
                    json.dumps(row.payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()

    def preview(**changes):
        return preview_pre_recording_retention(
            state.sessions, SOURCE, before=changes.get("before", cutoff), timezone="UTC", now=now
        )

    assert "recording_origins_missing" in preview().blockers
    state.service.consume(CONTEXT, request(state))
    assert preview().blockers == ("early_reporting_history_not_scanned",)
    with state.sessions.begin() as db:
        page = state.archive.reporting_page_in_transaction(
            db, UsageArchiveQuery(**SOURCE.model_dump(), after_seq=5, through_seq=6)
        )
        draft = ReportingGapScanPosition.model_construct(
            source=SOURCE,
            first_seq=1,
            last_report=ReportingPoint.from_archive(page.reports[0]),
            uncertain_clock_pairs=0,
            fingerprint="0" * 64,
        )
        saved = ReportingGapScanPosition.model_validate(
            {
                **draft.model_dump(mode="json"),
                "fingerprint": draft.content_fingerprint(),
            }
        )
        ReportingGapHistoryRepository(db).advance(saved, None)
    statements = []
    engine = state.sessions.kw["bind"]

    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        first = preview()
        assert first.read_only and not first.deletion_enabled and not first.blockers
        assert first.recording_exclusion_through_seq == 2
        assert first.reviewed_derivations == first.reviewed_builds == 0
        assert (first.payload_prefix.first_seq, first.payload_prefix.through_seq) == (1, 2)
        assert first.payload_prefix.stopped_before_seq == 3
        assert preview().fingerprint == first.fingerprint
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert all(sql.lstrip().startswith(("select", "set", "begin")) for sql in statements)
    assert not any("agent_messages.payload" in sql for sql in statements)

    early_config = configuration(consumer_id="second-earlier-consumer", initial_after_seq=0)
    earlier = rebuild(state, config=early_config)
    earlier.consume(
        CONTEXT,
        UsageTraceConsumeRequest(
            id=uuid4(),
            expected_configuration_fingerprint=early_config.fingerprint,
            expected_revision=0,
            through_seq=6,
        ),
    )
    protected = preview()
    assert len(protected.recording_origins) == 2
    assert protected.recording_exclusion_through_seq == 0
    assert protected.payload_prefix.first_seq is None
    assert protected.payload_prefix.stopped_before_seq == 1
    assert protected.fingerprint != first.fingerprint
    for invalid in (
        cutoff.replace(tzinfo=None),
        cutoff + timedelta(seconds=1),
        cutoff + timedelta(days=7),
    ):
        with pytest.raises(ValueError):
            preview(before=invalid)
    monkeypatch.setenv("ARIS_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "aris-storage",
            "--pre-recording-source",
            "agent-a",
            "node-a",
            "--retention-before",
            cutoff.isoformat(),
            "--retention-timezone",
            "UTC",
        ],
    )
    storage.run()
    assert json.loads(capsys.readouterr().out)["fingerprint"] == protected.fingerprint


def test_pre_recording_derivations_use_verified_original_requests_without_result_bodies(
    derivations,
):
    service, incoming, state = derivations
    service.record(DERIVATION_CONTEXT, incoming)
    sessions, engine = state[1:3]
    statements = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(engine, "before_cursor_execute", capture)
    try:
        with sessions() as db:
            repo = RetentionRepository(db)
            count, characters, first = repo.derivation_source_bounds(
                "agent-a", "node-a", maximum_records=8, maximum_characters=8192
            )
            assert count == 1 and 0 < characters < 8192 and first == 3
            for records, budget in ((0, 8192), (8, 1)):
                with pytest.raises(DomainError, match="exceeds its budget"):
                    repo.derivation_source_bounds(
                        "agent-a", "node-a", maximum_records=records, maximum_characters=budget
                    )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert not any("canonical_result" in sql for sql in statements)
    assert all(sql.lstrip().startswith("select") for sql in statements)
    with sessions.begin() as db:
        original = dict(db.execute(select(*UsageDerivation.__table__.c)).mappings().one())
        unused_event = db.scalar(
            select(DomainEvent.event_id)
            .where(DomainEvent.event_id.not_in(select(UsageDerivation.event_id)))
            .limit(1)
        )
        assert unused_event is not None
        original.update(id=str(uuid4()), event_id=unused_event, canonical_request="[]")
        db.execute(UsageDerivation.__table__.insert().values(**original))
    with sessions() as db:
        with pytest.raises(DomainError, match="sources are inconsistent"):
            RetentionRepository(db).derivation_source_bounds(
                "agent-a", "node-a", maximum_records=8, maximum_characters=8192
            )
