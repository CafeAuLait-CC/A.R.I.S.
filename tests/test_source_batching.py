from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, event

from aris.apps.composition.lyra import LyraRuntimeConfig, build_lyra_components
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_partition_schemas import ResidencyPartitionMethod
from aris.modules.lyra.source_batching import SourceBatchRule, should_buffer_source
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.usage_archive_schemas import UsageArchiveQuery
from tests.test_recording_pump import recorded
from tests.test_stable_pipeline import incoming
from tests.test_stable_recording_worker import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    stable_config,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


def graph_for(state, minimum=32, delay=240):
    config = stable_config(state).model_copy(
        update={
            "source_limit": 128,
            "source_batching": SourceBatchRule(
                minimum_messages=minimum, maximum_delay_seconds=delay
            ),
        }
    )
    return build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"]), config


def test_buffering_does_not_write_and_persisted_arrival_deadline_survives_rebuilding(
    partition_recording,
):
    state = partition_recording
    graph, config = graph_for(state)
    statements = []

    def observe(db, cursor, statement, params, context, many):
        if statement.startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(statement)

    engine = state[0].sessions.kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        report = graph.run_once()
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert report["source"]["state"] == "buffering" and not statements
    assert report["source"]["after_seq"] == 2 and not report["failures"]
    assert recorded(state[0]) == (0, 0)
    # New process state must not restart the wait; the original DB arrival supplies the deadline.
    state[4]["now"] += timedelta(seconds=240)
    rebuilt = build_lyra_components(state[0].sessions, config, clock=lambda: state[4]["now"])
    released = rebuilt.run_once()
    assert released["source"]["state"] == "prepared" and released["source"]["after_seq"] == 24
    assert not released["failures"]


def test_buffering_does_not_block_prepared_layout_or_recording_jobs(partition_recording):
    state = partition_recording
    graph, _ = graph_for(state)
    producer = graph.pump.pipelines[0]
    producer.consume(POST, incoming(producer, limit=16))
    report = graph.run_once()
    assert report["source"]["state"] == "buffering"
    assert report["dispatch"]["state"] == "enqueued" and report["job"]["state"] == "completed"
    assert recorded(state[0]) == (1, 45_000_000)


def test_full_batch_skips_preview_but_real_source_gap_is_never_hidden(partition_recording):
    state = partition_recording
    graph, _ = graph_for(state, minimum=4)

    class NoPreview:
        def page(self, *args):
            raise AssertionError("ready source read a preview")

    graph.pump.batch_reader = NoPreview()
    report = graph.run_once()
    assert report["source"]["state"] == "prepared" and not report["failures"]
    # A new explicit source is not needed: remove the first pending message in a fresh fixture
    # scenario by checking the same archive through the public helper at its original position.
    with state[0].sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
    trace = graph.pump.pipelines[0].configuration.trace
    assert not should_buffer_source(
        SourceBatchRule(minimum_messages=32, maximum_delay_seconds=240),
        graph.pump.archive,
        trace,
        after_seq=2,
        through_seq=24,
        now=state[4]["now"],
    )


def test_wrong_preview_or_learned_limit_fails_source_without_advancing(partition_recording):
    state = partition_recording
    graph, _ = graph_for(state)
    producer = graph.pump.pipelines[0]
    archive = graph.pump.archive

    class WrongPreview:
        def page(self, query):
            page = archive.page(query)
            return page.model_copy(update={"query": query.model_copy(update={"node_id": "wrong"})})

    graph.pump.batch_reader = WrongPreview()
    report = graph.run_once()
    assert (
        report["failures"][0]["stage"] == "source" and report["failures"][0]["code"] == "CONFLICT"
    )
    assert producer.trace.position().after_seq == 2
    graph.pump.batch_reader = archive
    graph.pump._source_limits[producer.configuration.trace.consumer_id] = 16
    report = graph.run_once()
    assert report["failures"][0]["code"] == "SOURCE_BATCH_LIMIT_TOO_SMALL"
    assert producer.trace.position().after_seq == 2
    first = (
        archive.page(
            UsageArchiveQuery(
                agent_id="agent-a", node_id="node-a", after_seq=2, through_seq=24, limit=1
            )
        )
        .items[0]
        .reference
    )
    with pytest.raises(DomainError, match="arrival exceeds"):
        should_buffer_source(
            SourceBatchRule(minimum_messages=32, maximum_delay_seconds=240),
            archive,
            producer.configuration.trace,
            after_seq=2,
            through_seq=24,
            now=first.received_at - timedelta(microseconds=1),
        )


def test_batch_rule_is_explicit_stable_only_and_old_default_fingerprint_is_unchanged(
    partition_recording,
):
    default = LyraRuntimeConfig()
    assert "source_batching" not in default.model_dump()
    assert default.fingerprint == "2648f0530b1640e7bc7fc252c24956e6facf09602337cdc97f68d228e42dc0cf"
    config = stable_config(partition_recording)
    for extra in (
        {
            "method": ResidencyPartitionMethod(max_intervals=3),
            "source_batching": SourceBatchRule(minimum_messages=4, maximum_delay_seconds=240),
        },
        {
            "source_limit": 8,
            "source_batching": SourceBatchRule(minimum_messages=32, maximum_delay_seconds=240),
        },
    ):
        with pytest.raises(ValidationError):
            LyraRuntimeConfig.model_validate({**config.model_dump(), **extra})
