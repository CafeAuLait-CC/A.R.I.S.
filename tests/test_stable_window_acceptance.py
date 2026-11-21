from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.lyra.models import (
    StableWindowAcceptanceRow,
    UsageDerivation,
    UsageLedgerEntryRow,
    UsageTraceAttestationRow,
    UsageTraceRecordRow,
)
from aris.modules.lyra.residency_evidence_schemas import ResidencyRunReference
from aris.modules.lyra.residency_layout import StableResidencyLayoutMethod
from aris.modules.lyra.residency_layout_verification_schemas import StableResidencyWindowReference
from aris.modules.lyra.residency_layout_verification_service import StableResidencyWindowService
from aris.modules.lyra.stable_window_acceptance_schemas import AcceptStableWindow
from aris.modules.lyra.stable_window_acceptance_service import StableWindowAcceptanceService
from aris.modules.vega.models import AgentMessage
from tests.test_mapping_history import assign
from tests.test_partition_recording import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


@pytest.fixture
def acceptance(partition_recording):
    writer, source, _, _, _ = partition_recording
    method = StableResidencyLayoutMethod(max_intervals=2)
    evidence = StableResidencyWindowService(
        writer.sessions, writer.evidence, method=method, clock=writer.clock
    )
    app = StableWindowAcceptanceService(
        writer.sessions,
        evidence,
        writer.identities,
        configuration=writer.ledger.configuration,
        clock=writer.clock,
    )
    query = AcceptStableWindow(
        window=StableResidencyWindowReference(
            reference=source.partition.reference,
            attestation_id=source.partition.closing_attestation_id,
            expected_attestation_fingerprint=source.partition.expected_closing_attestation_fingerprint,
            expected_trace_configuration_fingerprint=source.partition.expected_trace_configuration_fingerprint,
            expected_method_fingerprint=method.fingerprint,
            window_index=0,
        ),
        expected_recording_configuration_fingerprint=source.expected_recording_configuration_fingerprint,
        identity_revision=source.identity_revision,
    )
    return app, query, partition_recording


def counts(app):
    with app.sessions() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                StableWindowAcceptanceRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
                UsageDerivation,
                UsageLedgerEntryRow,
            )
        )


def open_reference(app, request):
    proof = app.evidence.verify(request.window)
    with app.sessions() as db:
        row = db.scalars(
            select(UsageTraceAttestationRow)
            .where(UsageTraceAttestationRow.consumer_id == proof.original.consumer_id)
            .order_by(UsageTraceAttestationRow.revision)
            .offset(1)
            .limit(1)
        ).one()
        fingerprint = db.scalar(
            select(UsageTraceRecordRow.fingerprint).where(UsageTraceRecordRow.id == row.record_id)
        )
        window = StableResidencyWindowReference.model_validate(
            {
                **request.window.model_dump(),
                "reference": ResidencyRunReference(
                    record_id=row.record_id,
                    expected_record_fingerprint=fingerprint,
                    run_id=request.window.reference.run_id,
                    source_cutoff=request.window.reference.source_cutoff,
                ),
                "attestation_id": row.id,
                "expected_attestation_fingerprint": row.fingerprint,
            }
        )
    return request.model_copy(update={"window": window})


def test_growth_closure_and_new_identity_revision_keep_the_first_acceptance(acceptance):
    app, closed, fixture = acceptance
    opened = open_reference(app, closed)
    before = counts(app)
    first = app.accept(POST, opened)
    stored = app.exact(first.key)
    assert stored.proof.original.state_at_record == "open"
    assert stored.proof.original.parent_run.intervals == 7
    assert counts(app) == tuple(
        value + 1 if index < 4 else value for index, value in enumerate(before)
    )
    later = app.accept(POST, closed)
    assert later.duplicate and later.fingerprint == first.fingerprint
    assert later.first_request == opened and app.exact(first.key) == stored
    assert not later.formal_usage_available and not later.quota_usage_available
    assert later.time_window_coverage == "not_assessed"

    # New historical knowledge must not rewrite the accepted identity, but blocks new windows.
    _, _, ledger_fixture, _, clock = fixture
    history, present = ledger_fixture[2][2], clock["now"]
    changed = assign(history, 80, 1, expected=history[4][0])
    clock["now"] = present
    newer = closed.model_copy(update={"identity_revision": changed.revision})
    after_change = counts(app)
    assert app.accept(POST, newer).first_request.identity_revision == opened.identity_revision
    with pytest.raises(DomainError) as error:
        app.accept(
            POST,
            newer.model_copy(
                update={"window": newer.window.model_copy(update={"window_index": 1})}
            ),
        )
    assert error.value.code == "USAGE_NEEDS_REVIEW"
    assert app.exact(first.key) == stored and counts(app) == after_change
    app.configuration = None
    assert app.accept(
        POST, closed
    ).duplicate  # Replay retains the old acceptance, not new authority.


def test_independent_formation_parent_start_and_configuration_are_required(acceptance):
    app, request, _ = acceptance
    before = counts(app)
    for changed in (
        request.model_copy(
            update={"window": request.window.model_copy(update={"window_index": 3})}
        ),
        request.model_copy(update={"expected_recording_configuration_fingerprint": "f" * 64}),
    ):
        with pytest.raises(DomainError):
            app.accept(POST, changed)
        assert counts(app) == before
    parent = app.evidence.verify(request.window).original.parent_run
    app.configuration = app.configuration.model_copy(
        update={"recording_start_at": parent.first.compute_observed_at + timedelta(seconds=30)}
    )
    late = request.model_copy(
        update={
            "expected_recording_configuration_fingerprint": app.configuration.fingerprint,
            "window": request.window.model_copy(update={"window_index": 2}),
        }
    )
    with pytest.raises(DomainError) as error:
        app.accept(POST, late)
    assert error.value.code == "USAGE_NEEDS_REVIEW" and counts(app) == before


@pytest.mark.parametrize("failure", ["source", "configuration"])
def test_final_revalidation_rolls_back_evidence_events_and_audit(acceptance, monkeypatch, failure):
    app, request, _ = acceptance
    before = counts(app)
    if failure == "source":
        original, calls = app._proof, []

        def changed(*args):
            calls.append(True)
            if len(calls) == 2:
                raise DomainError("CONFLICT", "final fixture source changed", 409)
            return original(*args)

        monkeypatch.setattr(app, "_proof", changed)
    else:
        original = app._emit

        def changed(*args):
            original(*args)
            app.configuration = None

        monkeypatch.setattr(app, "_emit", changed)
    with pytest.raises(DomainError):
        app.accept(POST, request)
    assert counts(app) == before


def test_first_evidence_is_immutable_retained_and_not_a_current_source_guarantee(
    acceptance, monkeypatch
):
    app, request, _ = acceptance
    engine = app.sessions.kw["bind"]
    with monkeypatch.context() as environment:
        environment.setenv("ARIS_DATABASE_URL", engine.url.render_as_string(hide_password=False))
        command.downgrade(Config("alembic.ini"), "0034")
        command.upgrade(Config("alembic.ini"), "head")
        command.check(Config("alembic.ini"))
    receipt = app.accept(POST, request)
    stored, before = app.exact(receipt.key), counts(app)
    for statement in (
        update(StableWindowAcceptanceRow).values(identity_revision=999),
        delete(StableWindowAcceptanceRow),
    ):
        with pytest.raises(DBAPIError), app.sessions.begin() as db:
            db.execute(statement)
    if engine.dialect.name == "postgresql":
        with pytest.raises(DBAPIError), app.sessions.begin() as db:
            db.execute(text("TRUNCATE usage_stable_window_acceptances"))
    with monkeypatch.context() as environment:
        environment.setenv("ARIS_DATABASE_URL", engine.url.render_as_string(hide_password=False))
        with pytest.raises(RuntimeError, match="history prevents downgrade"):
            command.downgrade(Config("alembic.ini"), "0034")
    assert app.exact(receipt.key) == stored and counts(app) == before
    with app.sessions.begin() as db:
        db.execute(delete(AgentMessage).where(AgentMessage.seq == 3))
    assert app.exact(receipt.key) == stored
    with pytest.raises(DomainError):
        app.accept(POST, request)


def test_postgresql_concurrent_growth_keeps_one_first_proof(acceptance, database_url):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real advisory locking")
    app, closed, _ = acceptance
    opened = open_reference(app, closed)
    before = counts(app)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda request: app.accept(POST, request), (opened, closed)))
    assert sorted(receipt.duplicate for receipt in receipts) == [False, True]
    assert receipts[0].fingerprint == receipts[1].fingerprint
    assert receipts[0].first_request == receipts[1].first_request
    assert counts(app) == tuple(
        value + 1 if index < 4 else value for index, value in enumerate(before)
    )
