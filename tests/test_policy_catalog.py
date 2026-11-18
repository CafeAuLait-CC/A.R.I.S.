import copy
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Event
from time import monotonic, sleep
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import Permission, RoleBinding
from aris.core.access.schemas import AccessScope, UserPrincipal
from aris.core.audit import AuditEntry
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import (
    ActorContext,
    CommandExecution,
    CommandRequest,
    ConfirmCommand,
)
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.db import SCHEMA_REVISION
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.models import PolicySourceVersion
from aris.modules.nova.policy_catalog_commands import RecordPolicyDraftCommand
from aris.modules.nova.policy_catalog_repo import PolicyCatalogRepository
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference, RecordPolicyDraft
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, PolicyCatalogService
from aris.modules.nova.policy_source import compile_policy, parse_policy_source
from tests.test_commands import command_state, context
from tests.test_policy_source import policy

__all__ = ["command_state"]
KEY = SecretStr("fixture-only-policy-confirmation-key-12345")
REFERENCE = PolicySourceReference(name="fixture-policy", version=1)


@pytest.fixture
def catalog_state(command_state):
    _, sessions, ids, _ = command_state
    with sessions.begin() as session:
        session.get(Permission, ids["permission"]).name = DRAFT_PERMISSION
    clock = {"now": datetime(2026, 11, 17, tzinfo=UTC)}
    catalog = PolicyCatalogService(sessions, clock=lambda: clock["now"])
    action = RecordPolicyDraftCommand(catalog)
    bus = CommandService(sessions, [action], confirmation_key=KEY, clock=lambda: clock["now"])
    return bus, catalog, sessions, ids, clock


def request(source=None, reason="Review fixture draft", **changes):
    return CommandRequest(
        command="policy.record_draft",
        scope=AccessScope(type="global", id="*"),
        parameters={"source": source or policy(), "reason": reason, **changes},
    )


def actor(ids):
    return ActorContext(actor_id=ids["alice"], request_id="fixture-policy-review")


def confirmed(state, *, key="policy-fixture-key", source=None, reason="Review fixture draft"):
    bus, _, _, ids, _ = state
    pending = bus.execute(context(ids, key=key), request(source, reason))
    if pending.state == "succeeded":
        return pending
    return bus.confirm(
        actor(ids), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )


def source_counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (PolicySourceVersion, DomainEvent, OutboxEntry)
        )


def reference(source=None):
    raw = source or policy()
    return PolicySourceReference(name=raw["metadata"]["name"], version=raw["metadata"]["version"])


def test_record_requires_confirmation_then_retains_exact_source_and_atomic_evidence(catalog_state):
    bus, catalog, sessions, ids, clock = catalog_state
    pending = bus.execute(context(ids), request())
    assert pending.state == "awaiting_confirmation" and source_counts(sessions) == (0, 0, 0)
    result = bus.confirm(
        actor(ids), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    )
    assert result.result["created"] and result.result["catalog_entry_only"]
    assert not result.result["publication_performed"] and source_counts(sessions) == (1, 1, 1)
    view = catalog.exact(actor(ids), reference())
    assert (
        view.fingerprint == compile_policy(view.source).fingerprint == result.result["fingerprint"]
    )
    assert view.command_id == result.command_id and view.created_by == UUID(ids["alice"])
    assert view.created_at == clock["now"] and view.reason == "Review fixture draft"
    with pytest.raises(ValidationError):
        view.source.metadata.version = 9
    with sessions() as session:
        event = session.scalars(select(DomainEvent)).one()
        assert event.event_type == "policy.draft_recorded" and event.aggregate_id == str(view.id)
        assert "source" not in event.payload and "reason" not in event.payload
        audits = session.scalars(
            select(AuditEntry).where(AuditEntry.action == "policy.draft_recorded")
        ).all()
        assert len(audits) == 1 and audits[0].changes["fingerprint"] == view.fingerprint


def test_canonical_replay_and_independent_equivalent_record_do_not_replace_original_evidence(
    catalog_state,
):
    bus, catalog, sessions, ids, clock = catalog_state
    first = confirmed(catalog_state)
    original = catalog.exact(actor(ids), reference())
    raw = copy.deepcopy(policy())
    raw["spec"]["gpu_time_weights"]["default_weight"] = {"numerator": 3, "denominator": 3}
    replay = confirmed(catalog_state, source=raw)
    assert replay.duplicate and replay.command_id == first.command_id
    clock["now"] += timedelta(minutes=1)
    equivalent = confirmed(catalog_state, key="another-key", source=raw, reason="Another review")
    assert not equivalent.duplicate and not equivalent.result["created"]
    assert equivalent.result["id"] == first.result["id"]
    assert catalog.exact(actor(ids), reference()) == original and source_counts(sessions) == (
        1,
        1,
        1,
    )


def test_same_version_different_content_is_a_conflict_and_a_new_version_preserves_the_old(
    catalog_state,
):
    _, catalog, sessions, ids, _ = catalog_state
    confirmed(catalog_state)
    old = catalog.exact(actor(ids), reference())
    raw = policy()
    raw["spec"]["reservation"]["max_concurrent_gpus"] = 7
    with pytest.raises(DomainError) as conflict:
        confirmed(catalog_state, key="conflict-key", source=raw)
    assert conflict.value.code == "CONFLICT" and source_counts(sessions) == (1, 1, 1)
    raw["metadata"]["version"] = 2
    result = confirmed(catalog_state, key="new-version-key", source=raw)
    assert result.result["created"] and source_counts(sessions) == (2, 2, 2)
    assert catalog.exact(actor(ids), reference()) == old
    assert catalog.exact(actor(ids), reference(raw)).fingerprint != old.fingerprint


@pytest.mark.parametrize("case", ["permission", "node_scope", "identity_admin", "revoked"])
def test_draft_management_is_an_explicit_current_global_permission(catalog_state, case):
    bus, catalog, sessions, ids, _ = catalog_state
    pending = bus.execute(context(ids), request()) if case == "revoked" else None
    with sessions.begin() as session:
        if case in {"permission", "revoked"}:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        elif case == "node_scope":
            binding = session.scalars(
                select(RoleBinding).where(RoleBinding.user_id == ids["alice"])
            ).one()
            binding.scope_type, binding.scope_id = "node", "fixture-node"
        else:
            session.get(Permission, ids["permission"]).name = "identity.manage"
    with pytest.raises(DomainError) as denied:
        if pending:
            bus.confirm(
                actor(ids), ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
            )
        else:
            bus.execute(context(ids), request())
    assert denied.value.code == "PERMISSION_DENIED" and source_counts(sessions) == (0, 0, 0)
    with pytest.raises(DomainError) as denied:
        catalog.exact(actor(ids), reference())
    assert denied.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("case", ["blank_reason", "newlines", "approval", "published", "script"])
def test_invalid_draft_inputs_cannot_bypass_the_fixed_source_schema(catalog_state, case):
    bus, _, sessions, ids, _ = catalog_state
    raw = request().model_dump(mode="json")
    if case == "blank_reason":
        raw["parameters"]["reason"] = "  "
    elif case == "newlines":
        raw["parameters"]["reason"] = "fixture\nreason"
    elif case == "script":
        raw["parameters"]["source"]["spec"]["script"] = "do not execute"
    else:
        raw["parameters"][case] = True
    with pytest.raises(DomainError) as invalid:
        bus.execute(context(ids), CommandRequest.model_validate(raw))
    assert invalid.value.code == "VALIDATION_ERROR" and source_counts(sessions) == (0, 0, 0)


def test_event_failure_rolls_back_source_and_original_confirmation_can_be_retried(
    catalog_state, monkeypatch
):
    bus, _, sessions, ids, _ = catalog_state
    pending = bus.execute(context(ids), request())
    proof = ConfirmCommand(command_id=pending.command_id, proof=pending.proof)
    from sqlalchemy import event

    engine = sessions.kw["bind"]

    def fail(connection, cursor, statement, parameters, context, executemany):
        if "INSERT INTO domain_events" in statement:
            raise RuntimeError("fixture event failure")

    event.listen(engine, "before_cursor_execute", fail)
    try:
        with pytest.raises(RuntimeError):
            bus.confirm(actor(ids), proof)
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    assert source_counts(sessions) == (0, 0, 0)
    with sessions() as session:
        assert session.get(CommandRecord, str(pending.command_id)).state == "awaiting_confirmation"
    assert bus.confirm(actor(ids), proof).result["created"]


@pytest.mark.parametrize(
    "operation", ["UPDATE policy_versions SET reason = 'changed'", "DELETE FROM policy_versions"]
)
def test_database_rejects_update_and_delete_of_recorded_history(catalog_state, operation):
    _, catalog, sessions, ids, _ = catalog_state
    confirmed(catalog_state)
    before = catalog.exact(actor(ids), reference())
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(text(operation))
    assert catalog.exact(actor(ids), reference()) == before


def test_empty_migration_roundtrip_and_drift_check_preserve_prior_schema(catalog_state):
    _, _, sessions, _, _ = catalog_state
    config = Config("alembic.ini")
    command.downgrade(config, "0013")
    assert "policy_versions" not in inspect(sessions.kw["bind"]).get_table_names()
    command.upgrade(config, "head")
    command.check(config)
    assert source_counts(sessions) == (0, 0, 0)


def test_history_prevents_downgrade_and_remains_readable(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    confirmed(catalog_state)
    before = catalog.exact(actor(ids), reference())
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0013")
    assert catalog.exact(actor(ids), reference()) == before
    command.upgrade(Config("alembic.ini"), "head")
    with sessions() as session:
        assert session.scalar(text("SELECT version_num FROM alembic_version")) == SCHEMA_REVISION


@pytest.mark.parametrize("case", ["fingerprint", "canonical_bytes", "name", "version", "creator"])
def test_inconsistent_stored_source_is_unavailable_instead_of_a_valid_draft(
    catalog_state, monkeypatch, case
):
    _, catalog, sessions, ids, _ = catalog_state
    confirmed(catalog_state)
    with sessions() as session:
        record = PolicyCatalogRepository(session).exact(reference())
    changes = {
        "fingerprint": {"fingerprint": "0" * 64},
        "canonical_bytes": {"canonical_json": record.canonical_json + " "},
        "name": {"name": "other-name"},
        "version": {"source_version": 9},
        "creator": {"created_by": "invalid-uuid"},
    }
    monkeypatch.setattr(
        PolicyCatalogRepository, "exact", lambda self, ref: replace(record, **changes[case])
    )
    with pytest.raises(DomainError) as invalid:
        catalog.exact(actor(ids), reference())
    assert invalid.value.code == "DEPENDENCY_UNAVAILABLE"


def test_read_uses_database_columns_without_flushing_or_trusting_dirty_tracked_source(
    catalog_state,
):
    _, catalog, sessions, ids, _ = catalog_state
    confirmed(catalog_state)
    original = catalog.exact(actor(ids), reference())
    with sessions() as session:
        stored = session.get(PolicySourceVersion, str(original.id))
        stored.reason = "dirty uncommitted value"
        result = catalog.exact_in_transaction(session, actor(ids), reference())
        assert result == original and stored.reason == "dirty uncommitted value"
        session.rollback()


def test_actor_scope_and_original_deadline_are_rechecked_before_source_commit(catalog_state):
    bus, catalog, sessions, ids, clock = catalog_state
    pending = bus.execute(context(ids), request())
    execution = CommandExecution(
        actor_id=ids["alice"],
        command_id=pending.command_id,
        request_id="fixture-source-write",
        scope=AccessScope(type="global", id="*"),
        execution_deadline=clock["now"],
    )
    principal = UserPrincipal(user_id=ids["alice"], display_name="Fixture", timezone="UTC")
    with sessions.begin() as session:
        with pytest.raises(DomainError) as expired:
            catalog.record_in_transaction(
                session, execution, principal, RecordPolicyDraft(source=policy(), reason="Fixture")
            )
        assert expired.value.code == "EXECUTION_EXPIRED"
    assert source_counts(sessions) == (0, 0, 0)
    for changed in (
        execution.model_copy(update={"scope": AccessScope(type="node", id="fixture-node")}),
        execution.model_copy(update={"actor_id": UUID(ids["bob"])}),
    ):
        with sessions.begin() as session:
            with pytest.raises(DomainError) as denied:
                catalog.record_in_transaction(
                    session,
                    changed,
                    principal,
                    RecordPolicyDraft(source=policy(), reason="Fixture"),
                )
            assert denied.value.code == "PERMISSION_DENIED"


@pytest.mark.parametrize("conflicting", [False, True])
def test_postgresql_concurrent_recording_has_one_source_and_one_domain_event(
    catalog_state, conflicting
):
    bus, catalog, sessions, ids, _ = catalog_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL immutable source uniqueness arbitration")
    raw = policy()
    if conflicting:
        raw["spec"]["reservation"]["max_concurrent_gpus"] = 7
    pending = [
        bus.execute(context(ids, key="race-a"), request()),
        bus.execute(context(ids, key="race-b"), request(raw)),
    ]
    barrier = Barrier(2)

    def finish(item):
        barrier.wait(timeout=10)
        try:
            return bus.confirm(
                actor(ids), ConfirmCommand(command_id=item.command_id, proof=item.proof)
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, pending))
    assert source_counts(sessions) == (1, 1, 1)
    if conflicting:
        assert len([item for item in results if item == "CONFLICT"]) == 1
    else:
        assert sorted(item.result["created"] for item in results) == [False, True]
    view = catalog.exact(actor(ids), reference())
    assert view.fingerprint in {
        compile_policy(parse_policy_source(json.dumps(item).encode())).fingerprint
        for item in (policy(), raw)
    }


def test_postgresql_exact_read_runs_in_a_read_only_transaction(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL source catalog READ ONLY contract")
    confirmed(catalog_state)
    before = source_counts(sessions)
    with sessions.begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert catalog.exact_in_transaction(session, actor(ids), reference()).catalog_entry_only
    assert source_counts(sessions) == before


def test_large_valid_sources_keep_the_platform_inline_command_size_limit(catalog_state):
    bus, _, sessions, ids, _ = catalog_state
    raw = policy()
    raw["spec"]["gpu_time_weights"]["model_weights"] = [
        {"model": str(index) + "X" * 250, "weight": {"numerator": 1, "denominator": 1}}
        for index in range(64)
    ]
    assert len(compile_policy(parse_policy_source(json.dumps(raw).encode())).canonical_json) < 65536
    with pytest.raises(DomainError) as error:
        bus.execute(context(ids), request(raw))
    assert error.value.code == "VALIDATION_ERROR" and source_counts(sessions) == (0, 0, 0)


def test_postgresql_truncate_cannot_bypass_source_history_protection(catalog_state):
    _, catalog, sessions, ids, _ = catalog_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL immutable source TRUNCATE guard")
    confirmed(catalog_state)
    before = catalog.exact(actor(ids), reference())
    with pytest.raises(DBAPIError):
        with sessions.begin() as session:
            session.execute(text("TRUNCATE policy_versions"))
    assert catalog.exact(actor(ids), reference()) == before


def test_postgresql_permission_revoked_while_unique_insert_waits_is_rechecked(
    catalog_state, monkeypatch
):
    bus, catalog, sessions, ids, _ = catalog_state
    if sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL actual unique-key wait and permission recheck")
    first = bus.execute(context(ids, user="bob", key="blocking-source"), request())
    second = bus.execute(context(ids, key="waiting-source"), request())
    inserted, waiting, release = Event(), Event(), Event()
    original = PolicyCatalogRepository.insert_if_absent
    waiter = {}

    def insert(repo, record):
        if record.created_by == ids["bob"]:
            created = original(repo, record)
            inserted.set()
            assert release.wait(10)
            return created
        waiter["pid"] = repo.session.scalar(text("SELECT pg_backend_pid()"))
        waiting.set()
        return original(repo, record)

    monkeypatch.setattr(PolicyCatalogRepository, "insert_if_absent", insert)

    def finish(item, user):
        try:
            return bus.confirm(
                ActorContext(actor_id=ids[user], request_id="fixture-lock-wait"),
                ConfirmCommand(command_id=item.command_id, proof=item.proof),
            )
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(finish, first, "bob")
        assert inserted.wait(5)
        two = pool.submit(finish, second, "alice")
        try:
            assert waiting.wait(5)
            deadline = monotonic() + 5
            observed_wait = False
            while monotonic() < deadline:
                with sessions() as session:
                    observed_wait = session.scalar(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid = :pid"
                        ),
                        {"pid": waiter["pid"]},
                    )
                if observed_wait:
                    break
                sleep(0.01)
            assert observed_wait, "fixture must witness a real PostgreSQL lock wait"
            with sessions.begin() as session:
                session.execute(delete(RoleBinding).where(RoleBinding.user_id == ids["alice"]))
        finally:
            release.set()
        assert one.result(timeout=10).result["created"]
        assert two.result(timeout=10) == "PERMISSION_DENIED"
    assert source_counts(sessions) == (1, 1, 1)
    assert catalog.exact(
        ActorContext(actor_id=ids["bob"], request_id="fixture-read"), reference()
    ).catalog_entry_only
