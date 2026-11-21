from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import SourcePrefixCheckpointRow
from aris.modules.lyra.source_prefix_repo import SourcePrefixRepository
from aris.modules.lyra.source_prefix_schemas import AdvanceSourcePrefix
from aris.modules.lyra.source_prefix_service import SourcePrefixService
from aris.modules.lyra.trace_attestation_service import TraceAttestationService
from aris.modules.vega.models import AgentMessage
from tests.test_administration import NOW
from tests.test_coverage_builds import build_request, build_service, drain, next_step
from tests.test_processing_coverage import attest
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
from tests.test_trace_consumer import CONTEXT, consumer, request, seed
from tests.test_usage_ledger import POST

__all__ = [
    "acceptance",
    "admin_state",
    "derivations",
    "history_state",
    "ledger",
    "partition_recording",
    "stable_recording",
    "consumer",
]


def prefix_service(writer):
    source = writer.acceptances.evidence.originals
    return SourcePrefixService(
        writer.sessions,
        source.attestations,
        configuration=source.records.configuration,
        clock=source.clock,
    )


def incoming(service, previous=None, limit=2):
    return AdvanceSourcePrefix(
        id=uuid4(),
        expected_configuration_fingerprint=service.configuration.fingerprint,
        previous=previous.reference() if previous else None,
        limit=limit,
    )


def sizes(service):
    with service.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (SourcePrefixCheckpointRow, DomainEvent, OutboxEntry, AuditEntry)
        )


def test_verified_old_prefix_excludes_only_earlier_times_and_build_reads_one_remaining_proof(
    stable_recording,
):
    writer, closed, (app, _, _) = stable_recording
    writer.accept_and_record(POST, open_reference(app, closed))
    post_remaining(writer, closed)
    prefixes = prefix_service(writer)
    prefix = prefixes.advance(POST, incoming(prefixes))
    assert prefix.reference().through_revision == 2
    assert prefix.max_observed_at == NOW + timedelta(seconds=115)
    build = build_service(service_for(writer))
    original = build.create(POST, build_request(build, start=120))
    old = drain(build, original).snapshot
    assert "prefix_checkpoint" not in old.window.model_dump()
    assert "prefix_checkpoint" not in old.request.model_dump()
    request = build_request(build, start=120).model_copy(
        update={"prefix_checkpoint": prefix.reference()}
    )
    plan = build.create(POST, request)
    assert plan.start_revision == 2 and build.progress(plan.request.id).processed_revision == 2
    step = build.advance(POST, next_step(plan, 2))
    assert step.revision == 3 and step.previous_fingerprint is None
    result = build.finish(POST, plan.request.id).snapshot
    assert (
        result.window.source_prefix_complete
        and result.window.total_usage_microseconds == 25_000_000
    )
    assert result.window.prefix_checkpoint == prefix.reference()
    assert build.windows.reverify(result.request.id) == result
    for forged in (
        prefix.reference().model_copy(update={"max_observed_at": NOW}),
        prefix.reference().model_copy(update={"fingerprint": "f" * 64}),
    ):
        with pytest.raises(DomainError, match="binding differs"):
            build.create(
                POST,
                build_request(build, start=120).model_copy(update={"prefix_checkpoint": forged}),
            )
    # Saved exclusion remains valid for later queries; it is not permission to delete raw data.
    # Here a disposable fixture models old-source absence. Current in-window raw checks remain.
    with writer.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
    assert build.windows.reverify(result.request.id) == result
    with pytest.raises(DomainError):
        build.windows.reverify(old.request.id)
    assert build.windows.exact(old.request.id) == old
    for start in (10, 115):
        with pytest.raises(DomainError, match="may affect"):
            build.create(
                POST,
                build_request(build, start=start).model_copy(
                    update={"prefix_checkpoint": prefix.reference()}
                ),
            )


def test_prefix_maximum_survives_clock_rollback_and_cannot_hide_a_future_hazard(consumer):
    state = consumer
    seed(state, times=(10, 120, 40, 50))
    proofs = TraceAttestationService(state.sessions, state.service, clock=state.now)
    for _ in range(2):
        receipt = state.service.consume(CONTEXT, request(state, through_seq=8, limit=4))
        attest(proofs, state.service, state.service.exact(receipt.id))
    service = SourcePrefixService(
        state.sessions, proofs, configuration=state.config, clock=state.now
    )
    first = service.advance(CONTEXT, incoming(service, limit=1))
    second = service.advance(CONTEXT, incoming(service, first, limit=1))
    assert second.max_observed_at == first.max_observed_at == state.base + timedelta(seconds=120)
    with state.sessions() as db:
        repo = SourcePrefixRepository(db)
        with pytest.raises(DomainError, match="may affect"):
            repo.checked(second.reference(), state.config, state.base + timedelta(seconds=100))
        assert repo.eligible(state.config, state.base + timedelta(seconds=121), 2) == second
    with pytest.raises(DomainError) as error:
        service.advance(CONTEXT, incoming(service, second))
    assert error.value.code == "SOURCE_PREFIX_CAUGHT_UP"


@pytest.mark.parametrize("when", ["before", "after"])
def test_prefix_commit_reply_loss_reuses_original_maximum_and_records_once(
    stable_recording,
    monkeypatch,
    when,
):
    import aris.modules.lyra.source_prefix_service as module

    service = prefix_service(stable_recording[0])
    request = incoming(service)
    before = sizes(service)
    original = module.commit_owned_transaction

    def lost(db):
        if when == "after":
            original(db)
        raise RuntimeError("fixture lost prefix commit reply")

    with monkeypatch.context() as patch:
        patch.setattr(module, "commit_owned_transaction", lost)
        with pytest.raises(RuntimeError, match="prefix commit reply"):
            service.advance(POST, request)
    saved = service.advance(POST, request)
    assert service.advance(POST, request) == saved
    assert sizes(service) == tuple(value + 1 for value in before)
    service.configuration = None
    assert service.exact(request.id) == saved


def test_missing_raw_or_final_write_failure_cannot_commit_a_prefix_and_history_is_immutable(
    stable_recording,
    monkeypatch,
):
    service = prefix_service(stable_recording[0])
    before = sizes(service)
    append = SourcePrefixRepository.append

    def failed(repo, value):
        append(repo, value)
        repo.session.flush()
        raise RuntimeError("fixture prefix append failure")

    with monkeypatch.context() as patch:
        patch.setattr(SourcePrefixRepository, "append", failed)
        with pytest.raises(RuntimeError, match="prefix append"):
            service.advance(POST, incoming(service))
    assert sizes(service) == before
    saved = service.advance(POST, incoming(service))
    with service.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 19))
    with pytest.raises(DomainError):
        service.advance(POST, incoming(service, saved))
    assert sizes(service) == tuple(value + 1 for value in before)
    for sql in (
        "UPDATE usage_source_prefix_checkpoints SET fingerprint=fingerprint",
        "DELETE FROM usage_source_prefix_checkpoints",
    ):
        with pytest.raises(DBAPIError), service.sessions.begin() as db:
            db.execute(text(sql))
    with pytest.raises(RuntimeError, match="source prefix history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0040")
    assert service.exact(saved.request.id) == saved


def test_relative_build_budget_preserves_large_absolute_source_revisions(stable_recording):
    # Structural counter check, not a claim that these synthetic prior proofs were verified.
    from aris.modules.lyra.coverage_build_schemas import (
        StoredCoverageBuild,
        coverage_build_fingerprint,
    )
    from aris.modules.lyra.source_prefix_schemas import SourcePrefixReference
    from aris.modules.lyra.trace_attestation_schemas import attestation_fingerprint

    writer, _, _ = stable_recording
    build = build_service(service_for(writer))
    plan = build.create(POST, build_request(build, start=120))
    tail = plan.tail.model_copy(update={"revision": 5001, "first_seq": 50001, "last_seq": 50006})
    tail = tail.model_copy(update={"fingerprint": attestation_fingerprint(tail)})
    source = plan.source.model_copy(update={"highest_committed_seq": 50006})
    from aris.modules.vega.usage_archive_schemas import archive_head_fingerprint

    source = source.model_copy(update={"fingerprint": archive_head_fingerprint(source)})
    ref = SourcePrefixReference(
        id=uuid4(),
        fingerprint="a" * 64,
        configuration_fingerprint=plan.configuration.trace.fingerprint,
        through_revision=5000,
        last_seq=50000,
        attestation_id=tail.previous_id,
        attestation_fingerprint=tail.previous_fingerprint,
        max_observed_at=NOW + timedelta(seconds=115),
        created_at=plan.read_at,
    )
    request = plan.request.model_copy(
        update={"prefix_checkpoint": ref, "expected_source_fingerprint": source.fingerprint}
    )
    draft = plan.model_copy(update={"request": request, "source": source, "tail": tail})
    draft = draft.model_copy(update={"fingerprint": coverage_build_fingerprint(draft)})
    assert StoredCoverageBuild.model_validate(draft.model_dump()).start_revision == 5000
    with pytest.raises(ValidationError):
        StoredCoverageBuild.model_validate(plan.model_copy(update={"tail": tail}).model_dump())


def test_postgresql_concurrent_prefix_same_operation_and_read_only_retrieval(
    stable_recording,
    database_url,
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real prefix transaction locks")
    service = prefix_service(stable_recording[0])
    request = incoming(service)
    before = sizes(service)

    def attempt(_):
        try:
            return service.advance(POST, request)
        except DomainError as error:
            assert error.code == "RETRYABLE_DATABASE_ERROR"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    saved = service.advance(POST, request)
    assert any(value == saved for value in results) and sizes(service) == tuple(
        value + 1 for value in before
    )
    with service.sessions() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        assert SourcePrefixRepository(db).get(request.id) == saved
    with pytest.raises(DBAPIError), service.sessions.begin() as db:
        db.execute(text("TRUNCATE usage_source_prefix_checkpoints CASCADE"))
    command.check(Config("alembic.ini"))
