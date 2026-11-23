from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.coverage_build_repo import CoverageBuildRepository
from aris.modules.lyra.coverage_build_schemas import AdvanceCoverageBuild, CreateCoverageBuild
from aris.modules.lyra.coverage_build_service import CoverageBuildService
from aris.modules.lyra.derivation_schemas import UsageDerivationContext, UsageDerivationRequest
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsagePostingRequest
from aris.modules.lyra.models import (
    CoverageBuildPlanRow,
    CoverageBuildStepRow,
    UsageCoverageManifestRow,
    UsageWindowSnapshotRow,
)
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.lyra.window_snapshot_service import UsageWindowSnapshotService
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from tests.test_administration import NOW
from tests.test_heartbeat import beat, heartbeat
from tests.test_mapping_history import assign
from tests.test_stable_coverage import (
    acceptance,
    admin_state,
    derivations,
    history_state,
    ledger,
    open_reference,
    partition_recording,
    post_remaining,
    service_for,
    stable_recording,
)
from tests.test_usage_ledger import POST

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_recording",
]


def build_service(coverage, *, timeout_ms=30000):
    windows = UsageWindowSnapshotService(
        coverage.sessions,
        coverage,
        coverage.archive,
        coverage.identities,
        configuration=coverage.configuration,
        clock=coverage.clock,
        timeout_ms=timeout_ms,
    )
    return CoverageBuildService(coverage, windows, clock=coverage.clock, timeout_ms=timeout_ms)


def build_request(build, start=10, end=145):
    config = build.coverage.configuration
    with build.sessions() as db:
        head = UsageLedgerRepository(db).head(config.trace.node_id)
        source = build.windows.archive.head_in_transaction(
            db,
            UsageArchiveHeadQuery(
                agent_id=config.trace.agent_id,
                node_id=config.trace.node_id,
            ),
        )
    return CreateCoverageBuild(
        id=uuid4(),
        query=UsageWindowQuery(
            node_id=config.trace.node_id,
            start_at=NOW + timedelta(seconds=start),
            end_at=NOW + timedelta(seconds=end),
        ),
        expected_configuration_fingerprint=config.fingerprint,
        expected_source_fingerprint=source.fingerprint,
        ledger=head,
    )


def next_step(plan, revision):
    return AdvanceCoverageBuild(
        id=plan.request.id, expected_plan_fingerprint=plan.fingerprint, expected_revision=revision
    )


def drain(build, plan):
    for revision in range(build.progress(plan.request.id).processed_revision, plan.target_revision):
        step = build.advance(POST, next_step(plan, revision))
        assert step.revision == revision + 1
    return build.finish(POST, plan.request.id)


def counts(build):
    with build.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                CoverageBuildPlanRow,
                CoverageBuildStepRow,
                UsageCoverageManifestRow,
                UsageWindowSnapshotRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
            )
        )


def test_resumed_build_keeps_admission_boundary_while_new_ledger_and_identity_arrive(
    stable_recording,
):
    writer, closed, (app, _, fixture) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    build = build_service(service_for(writer))
    plan = build.create(POST, build_request(build))
    assert build.create(POST, plan.request) == plan
    first = build.advance(POST, next_step(plan, 0))
    assert beat(fixture[2][4], heartbeat(seq=25)).status_code == 200
    post_remaining(writer, closed)
    history, clock = fixture[2][2][2], fixture[4]
    present = clock["now"]
    assign(history, 80, 1, expected=history[4][0])
    clock["now"] = present
    rebuilt = build_service(build.coverage)
    assert rebuilt.exact(plan.request.id) == plan
    assert rebuilt.advance(POST, next_step(plan, 0)) == first
    result = drain(rebuilt, plan)
    assert not result.duplicate
    assert result.snapshot.window.ledger == plan.request.ledger
    assert result.snapshot.window.source.highest_committed_seq == 24
    assert result.snapshot.window.identity_revision == plan.identity_revision
    assert result.snapshot.window.verified_usage_microseconds == 45_000_000
    assert result.snapshot.window.unknown_gpu_microseconds == 90_000_000
    assert result.snapshot.window.total_usage_microseconds is None
    assert rebuilt.windows.reverify(plan.window_id) == result.snapshot
    progress = rebuilt.progress(plan.request.id)
    assert progress.sources_built and progress.window_saved and not progress.quota_usage_available
    with pytest.raises(DomainError) as error:
        rebuilt.create(POST, build_request(rebuilt))
    assert error.value.code == "COVERAGE_SOURCE_NOT_CAUGHT_UP"
    rebuilt.coverage.configuration = rebuilt.windows.configuration = None
    assert rebuilt.advance(POST, next_step(plan, 0)) == first
    assert rebuilt.finish(POST, plan.request.id).duplicate
    assert rebuilt.exact(plan.request.id) == plan


def test_late_proof_alias_with_same_physical_ledger_head_cannot_enter_an_admitted_build(
    stable_recording,
):
    writer, closed, (app, _, fixture) = stable_recording
    # A real lower-level measurement has no closing receipt; a stable alias reuses its time.
    proof = fixture[0].evidence.verify(fixture[1].partition)
    derived = writer.derivations.record(
        UsageDerivationContext(request_id="raw-without-proof"),
        UsageDerivationRequest(
            id=uuid4(),
            sources=proof.sources,
            as_of=fixture[1].partition.reference.source_cutoff,
            identity_revision=closed.identity_revision,
            continuity=proof.continuity,
            formation=proof.formation,
        ),
    )
    artifact = writer.derivations.exact(derived.id).artifact
    index = artifact.analysis.request.attribution.residency.sessions.index(proof.candidate)
    measured = writer.ledger.post(
        POST,
        UsagePostingRequest(
            id=uuid4(),
            derivation_id=derived.id,
            session_index=index,
            expected_result_fingerprint=derived.result_fingerprint,
            expected_configuration_fingerprint=writer.ledger.configuration.fingerprint,
        ),
    )
    build = build_service(service_for(writer))
    plan = build.create(POST, build_request(build))
    assert plan.recording_revision == 0
    alias = writer.accept_and_record(POST, open_reference(app, closed))
    assert alias.ledger.effect == "already_recorded"
    assert alias.ledger.entries == measured.entries
    assert build_request(build).ledger == plan.request.ledger
    old = drain(build, plan).snapshot.window
    assert old.recorded_microseconds == 45_000_000 and old.verified_usage_microseconds == 0
    current = build.create(POST, build_request(build))
    assert current.recording_revision == 1
    new = drain(build, current).snapshot.window
    assert new.verified_usage_microseconds == 45_000_000


def test_finished_generation_uses_complete_proof_prefix_and_does_not_promote_missing_steps(
    stable_recording,
    monkeypatch,
):
    writer, closed, (app, _, _) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    post_remaining(writer, closed)
    build = build_service(service_for(writer))
    plan = build.create(POST, build_request(build))
    before = counts(build)
    with pytest.raises(DomainError) as error:
        build.finish(POST, plan.request.id)
    assert error.value.code == "COVERAGE_BUILD_INCOMPLETE" and counts(build) == before
    with pytest.raises(DomainError, match="progress changed"):
        build.advance(POST, next_step(plan, 1))

    def unexpected_replay(*_):
        pytest.fail("Finalizing committed coverage must not replay the source")

    with monkeypatch.context() as patch:
        patch.setattr(build.coverage, "reverify_in_transaction", unexpected_replay)
        result = drain(build, plan).snapshot
    assert (
        result.window.source_prefix_complete
        and result.window.total_usage_microseconds == 135_000_000
    )
    assert build.windows.reverify(plan.window_id) == result
    for statement in (
        "DELETE FROM usage_coverage_build_plans",
        "UPDATE usage_coverage_build_steps SET fingerprint=fingerprint",
    ):
        with pytest.raises(DBAPIError), build.sessions.begin() as db:
            db.execute(text(statement))
    with pytest.raises(RuntimeError, match="coverage build history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0039")


@pytest.mark.parametrize("point", ["plan", "step", "window"])
def test_failed_plan_step_or_window_write_rolls_back_all_derived_history(
    stable_recording,
    monkeypatch,
    point,
):
    writer, closed, (app, _, _) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    build = build_service(service_for(writer))
    incoming = build_request(build)
    plan = build.create(POST, incoming) if point != "plan" else None
    if point == "window":
        for revision in range(plan.target_revision):
            build.advance(POST, next_step(plan, revision))
    owner, name = (
        (CoverageBuildRepository, "append" if point == "plan" else "append_step")
        if point != "window"
        else (build.windows, "_append")
    )
    original, before = getattr(owner, name), counts(build)

    def fail(*args):
        original(*args)
        raise RuntimeError("fixture coverage build write failure")

    with monkeypatch.context() as patch:
        patch.setattr(owner, name, fail)
        with pytest.raises(RuntimeError, match="build write failure"):
            if point == "plan":
                build.create(POST, incoming)
            elif point == "step":
                build.advance(POST, next_step(plan, 0))
            else:
                build.finish(POST, plan.request.id)
    assert counts(build) == before


@pytest.mark.parametrize("when", ["before", "after"])
def test_lost_commit_reply_replays_plan_step_and_window_without_duplicate_events(
    stable_recording,
    monkeypatch,
    when,
):
    import aris.modules.lyra.coverage_build_service as module

    writer, _, _ = stable_recording
    build = build_service(service_for(writer))
    request = build_request(build)
    original = module.commit_owned_transaction

    def lost(db):
        if when == "after":
            original(db)
        raise RuntimeError("fixture lost coverage build commit reply")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", lost)
        with pytest.raises(RuntimeError, match="lost coverage build"):
            build.create(POST, request)
    plan = build.create(POST, request)
    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", lost)
        with pytest.raises(RuntimeError, match="lost coverage build"):
            build.advance(POST, next_step(plan, 0))
    first = build.advance(POST, next_step(plan, 0))
    assert first.revision == 1
    for revision in range(1, plan.target_revision):
        build.advance(POST, next_step(plan, revision))
    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", lost)
        with pytest.raises(RuntimeError, match="lost coverage build"):
            build.finish(POST, request.id)
    result = build.finish(POST, request.id)
    assert result.duplicate is (when == "after")
    assert counts(build)[:4] == (1, plan.target_revision, plan.target_revision, 1)
    assert build.windows.reverify(plan.window_id) == result.snapshot


def test_new_admission_rejects_stale_head_future_window_and_disabled_configuration(
    stable_recording,
    monkeypatch,
):
    writer, closed, (app, _, _) = stable_recording
    build = build_service(service_for(writer))
    request = build_request(build)
    writer.accept_and_record(POST, open_reference(app, closed))
    with pytest.raises(DomainError, match="boundaries changed"):
        build.create(POST, request)
    current = build_request(build)
    future = current.model_copy(
        update={
            "query": current.query.model_copy(
                update={"end_at": build._now() + timedelta(seconds=1)}
            )
        }
    )
    with pytest.raises(DomainError, match="has not ended"):
        build.create(POST, future)
    assert counts(build)[:4] == (0, 0, 0, 0)
    build.coverage.configuration = None

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled build accessed DB")

    monkeypatch.setattr(build, "sessions", forbidden)
    with pytest.raises(DomainError, match="paused"):
        build.create(POST, current)


def test_postgresql_same_source_step_has_one_atomic_manifest_and_progress(
    stable_recording, database_url
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real coverage build transaction locks")
    writer, _, _ = stable_recording
    build = build_service(service_for(writer))
    command.check(Config("alembic.ini"))
    plan = build.create(POST, build_request(build))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: build.advance(POST, next_step(plan, 0)), range(2)))
    assert results[0] == results[1] and counts(build)[:4] == (1, 1, 1, 0)
    with build.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        assert CoverageBuildRepository(db).get(plan.request.id) == plan
    for table in ("usage_coverage_build_plans", "usage_coverage_build_steps"):
        with pytest.raises(DBAPIError), build.sessions.begin() as db:
            db.execute(text("TRUNCATE " + table + " CASCADE"))
