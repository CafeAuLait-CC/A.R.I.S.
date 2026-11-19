from uuid import UUID

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import delete, event, func, inspect, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import RoleBinding
from aris.core.access.schemas import AccessRequest
from aris.core.audit import AuditEntry
from aris.core.commands.schemas import CommandExecution, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.db import Base, build_engine
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.models import PolicyReviewMaterialRow, PolicySourceVersion
from aris.modules.nova.policy_catalog_service import DRAFT_PERMISSION, GLOBAL_SCOPE
from aris.modules.nova.policy_material_commands import RecordPolicyReviewMaterialCommand
from aris.modules.nova.policy_material_repo import PolicyMaterialRepository
from aris.modules.nova.policy_material_schemas import (
    PolicyMaterialListQuery,
    RecordPolicyReviewMaterial,
)
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from tests.test_commands import command_state, context
from tests.test_policy_catalog import KEY, actor, catalog_state, confirmed, reference
from tests.test_policy_simulation import scenario

__all__ = ["command_state", "catalog_state"]


@pytest.fixture
def materials(catalog_state):
    assert confirmed(catalog_state).state == "succeeded"
    _, catalog, sessions, ids, clock = catalog_state
    service = PolicyReviewMaterialService(catalog, implementation_ref="fixture-constraints-a")
    bus = CommandService(
        sessions,
        [RecordPolicyReviewMaterialCommand(service)],
        confirmation_key=KEY,
        clock=catalog.clock,
    )
    return bus, service, catalog_state


def request(state, **changes):
    _, service, original = state
    source = service.catalog.exact(actor(original[3]), reference())
    values = dict(
        source=reference(),
        expected_source_fingerprint=source.fingerprint,
        expected_implementation_ref="fixture-constraints-a",
        scenarios=(
            scenario(include_usage=False, name="unknown-example"),
            scenario(used_microseconds=60_000_000, name="provided-assumption"),
        ),
        reason="Explicit supplied-case material; not approval",
    )
    values.update(changes)
    return CommandRequest(
        command="policy.record_review_material",
        scope=GLOBAL_SCOPE,
        parameters=RecordPolicyReviewMaterial(**values).model_dump(mode="json"),
    )


def pending(state, *, key="material-fixture", parameters=None):
    return state[0].execute(context(state[2][3], key=key), parameters or request(state))


def finish(state, item):
    return state[0].confirm(
        actor(state[2][3]), ConfirmCommand(command_id=item.command_id, proof=item.proof)
    )


def counts(state):
    with state[2][2]() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (
                PolicyReviewMaterialRow,
                DomainEvent,
                OutboxEntry,
                AuditEntry,
                PolicySourceVersion,
            )
        )


def test_confirmed_material_binds_source_server_results_and_original_replays(materials):
    state = materials
    before = counts(state)
    proposal = pending(state)
    assert proposal.state == "awaiting_confirmation" and counts(state)[0] == 0
    saved = finish(state, proposal)
    assert saved.result["created"] and saved.result["review_material_only"]
    assert not saved.result["approval_performed"] and not saved.result["publication_performed"]
    id = UUID(saved.result["id"])
    original = state[1].read(actor(state[2][3]), id, reverify=True)
    assert (
        original.basis == "caller_supplied_examples" and not original.current_dependencies_assessed
    )
    assert [case.decision for case in original.results] == ["NOT_EVALUATED", "WOULD_ALLOW"]
    assert all(case.simulation_only for case in original.results)
    after = counts(state)
    assert after[0] == before[0] + 1 and after[1] == before[1] + 1 and after[2] == before[2] + 1
    assert after[4] == before[4]
    # A later configured implementation never reinterprets the old committed command.
    state[1].implementation_ref = "fixture-constraints-b"
    replay = pending(state)
    assert replay.duplicate and replay.result == saved.result and counts(state) == after
    assert state[1].read(actor(state[2][3]), id) == original
    with pytest.raises(DomainError) as error:
        state[1].read(actor(state[2][3]), id, reverify=True)
    assert error.value.code == "CONFLICT"
    with state[2][2]() as session:
        public = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == "policy.review_material_recorded")
        ).one()
        assert (
            "scenarios" not in public.payload
            and "usage" not in public.payload
            and "reason" not in public.payload
        )


def test_material_and_event_outbox_audit_rollback_with_outer_transaction_or_failure(
    materials, monkeypatch
):
    state = materials
    proposal = pending(state)
    baseline = counts(state)
    principal = state[1].catalog.access.require_permission(
        AccessRequest(user_id=state[2][3]["alice"], permission=DRAFT_PERMISSION, scope=GLOBAL_SCOPE)
    )
    execution = CommandExecution(
        actor_id=state[2][3]["alice"],
        command_id=proposal.command_id,
        request_id="fixture-outer-material",
        scope=GLOBAL_SCOPE,
    )
    parameters = RecordPolicyReviewMaterial.model_validate(request(state).parameters)
    with pytest.raises(RuntimeError), state[2][2].begin() as session:
        state[1].record_in_transaction(session, execution, principal, parameters)
        raise RuntimeError("fixture caller rollback")
    assert counts(state) == baseline
    append = state[1]._append_event

    def fail(session, execution, value):
        append(session, execution, value)
        raise DomainError("INTERNAL_ERROR", "fixture after event", 500)

    monkeypatch.setattr(state[1], "_append_event", fail)
    with pytest.raises(DomainError):
        finish(state, proposal)
    assert counts(state) == baseline
    monkeypatch.setattr(state[1], "_append_event", append)
    assert finish(state, proposal).state == "succeeded"
    assert counts(state)[0:3] == tuple(value + 1 for value in baseline[0:3])


def test_wrong_source_runtime_scope_and_revoked_permissions_never_record_material(materials):
    state = materials
    wrong = pending(
        state, key="wrong-source", parameters=request(state, expected_source_fingerprint="f" * 64)
    )
    with pytest.raises(DomainError) as error:
        finish(state, wrong)
    assert error.value.code == "CONFLICT"
    old = pending(state, key="changed-implementation")
    state[1].implementation_ref = "fixture-constraints-b"
    with pytest.raises(DomainError) as error:
        finish(state, old)
    assert error.value.code == "CONFLICT"
    state[1].implementation_ref = "fixture-constraints-a"
    forged = request(state).model_copy(
        update={"scope": GLOBAL_SCOPE.model_copy(update={"type": "node", "id": "fixture-node"})}
    )
    with pytest.raises(DomainError) as error:
        state[0].execute(context(state[2][3], key="wrong-scope"), forged)
    assert error.value.code == "PERMISSION_DENIED"
    valid = pending(state, key="revoked-material")
    with pytest.raises(DomainError):
        state[0].confirm(
            actor({"alice": state[2][3]["bob"]}),
            ConfirmCommand(command_id=valid.command_id, proof=valid.proof),
        )
    with state[2][2].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == state[2][3]["alice"]))
    with pytest.raises(DomainError) as error:
        finish(state, valid)
    assert error.value.code == "PERMISSION_DENIED" and counts(state)[0] == 0


def test_strict_bounded_inputs_and_disabled_writer_cannot_touch_material_storage(materials):
    state = materials
    parameters = request(state).parameters
    invalid = [
        {**parameters, "approval_performed": True},
        {**parameters, "reason": "bad\nreason"},
        {**parameters, "scenarios": [parameters["scenarios"][0]] * 2},
    ]
    before = counts(state)
    for index, value in enumerate(invalid):
        with pytest.raises(DomainError) as error:
            state[0].execute(
                context(state[2][3], key="invalid-material-" + str(index)),
                CommandRequest(
                    command="policy.record_review_material", scope=GLOBAL_SCOPE, parameters=value
                ),
            )
        assert error.value.code == "VALIDATION_ERROR"
    large = request(
        state,
        scenarios=tuple(
            scenario(
                name="large-" + str(index),
                assumed_roles=tuple("r" * 60 + str(role) for role in range(32)),
            )
            for index in range(8)
        ),
    )
    with pytest.raises(DomainError) as error:
        pending(state, key="oversized-material", parameters=large)
    assert error.value.code == "VALIDATION_ERROR" and counts(state) == before
    disabled = PolicyReviewMaterialService(state[1].catalog)
    engine = state[2][2].kw["bind"]

    def forbidden(*args, **kwargs):
        raise AssertionError("disabled registration queried database")

    event.listen(engine, "before_cursor_execute", forbidden)
    try:
        with pytest.raises(DomainError) as error:
            RecordPolicyReviewMaterialCommand(disabled)
        assert error.value.code == "POLICY_REVIEW_NOT_CONFIGURED"
    finally:
        event.remove(engine, "before_cursor_execute", forbidden)


def test_directory_is_keyset_metadata_and_current_access_protects_historical_body(
    materials, monkeypatch
):
    state = materials
    first = finish(state, pending(state))
    second = finish(
        state,
        pending(
            state,
            key="second-material",
            parameters=request(state, reason="Another fixed example suite"),
        ),
    )
    query = PolicyMaterialListQuery(
        source=reference(),
        expected_source_fingerprint=request(state).parameters["expected_source_fingerprint"],
        limit=1,
    )
    statements = []

    def observe(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    engine = state[2][2].kw["bind"]
    event.listen(engine, "before_cursor_execute", observe)
    try:
        one = state[1].list(actor(state[2][3]), query)
        two = state[1].list(
            actor(state[2][3]), query.model_copy(update={"before": one.next_cursor})
        )
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert {str(item.id) for item in one.items + two.items} == {
        first.result["id"],
        second.result["id"],
    }
    assert two.next_cursor is None and not any("canonical_material" in sql for sql in statements)
    assert all(item.scenarios == 2 for item in one.items + two.items)
    with pytest.raises(DomainError) as error:
        state[1].list(
            actor(state[2][3]), query.model_copy(update={"expected_source_fingerprint": "f" * 64})
        )
    assert error.value.code == "CONFLICT"
    with state[2][2].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == state[2][3]["alice"]))

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized body queried")

    monkeypatch.setattr(PolicyMaterialRepository, "get", forbidden)
    with pytest.raises(DomainError) as error:
        state[1].read(actor(state[2][3]), UUID(first.result["id"]))
    assert error.value.code == "PERMISSION_DENIED"


def test_immutable_material_and_nonempty_downgrade_preserve_original_history(materials):
    state = materials
    saved = finish(state, pending(state))
    id = UUID(saved.result["id"])
    original = state[1].read(actor(state[2][3]), id)
    operations = [
        "UPDATE policy_review_materials SET request_id='changed'",
        "DELETE FROM policy_review_materials",
    ]
    if state[2][2].kw["bind"].dialect.name == "postgresql":
        operations.append("TRUNCATE policy_review_materials")
    for sql in operations:
        with pytest.raises(DBAPIError), state[2][2].begin() as session:
            session.execute(text(sql))
        assert state[1].read(actor(state[2][3]), id) == original
    with pytest.raises(RuntimeError, match="history prevents downgrade"):
        command.downgrade(Config("alembic.ini"), "0028")
    assert state[1].read(actor(state[2][3]), id) == original


def test_empty_material_migration_roundtrip_matches_metadata(database_url, monkeypatch):
    monkeypatch.setenv("ARIS_DATABASE_URL", database_url)
    monkeypatch.setenv("ARIS_ENVIRONMENT", "test")
    command.upgrade(Config("alembic.ini"), "0028")
    command.upgrade(Config("alembic.ini"), "head")
    engine = build_engine(database_url)
    try:
        assert "policy_review_materials" in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
        command.downgrade(Config("alembic.ini"), "0028")
        assert "policy_review_materials" not in inspect(engine).get_table_names()
        command.upgrade(Config("alembic.ini"), "head")
        with engine.connect() as connection:
            assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
    finally:
        engine.dispose()


def test_postgresql_material_reads_are_read_only_bounded_and_recover_after_lock(
    materials, monkeypatch
):
    state = materials
    if state[2][2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only and real lock wait")
    saved = finish(state, pending(state))
    id = UUID(saved.result["id"])
    original_get = PolicyMaterialRepository.get

    def checked(repo, value):
        assert repo.session.scalar(text("SHOW transaction_read_only")) == "on"
        return original_get(repo, value)

    monkeypatch.setattr(PolicyMaterialRepository, "get", checked)
    assert state[1].read(actor(state[2][3]), id, expected_source=reference()).id == id
    with state[2][2]() as blocker:
        blocker.execute(text("LOCK TABLE policy_review_materials IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            state[1].read(actor(state[2][3]), id)
        assert error.value.code == "RETRYABLE_DATABASE_ERROR"
        blocker.rollback()
    assert state[1].read(actor(state[2][3]), id).id == id


def test_postgresql_revocation_after_insert_rolls_back_uncommitted_material(materials, monkeypatch):
    state = materials
    if state[2][2].kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL independent concurrent revocation")
    proposal = pending(state)
    before = counts(state)
    original = PolicyMaterialRepository.insert_if_absent

    def revoke(repo, value):
        result = original(repo, value)
        with state[2][2].begin() as other:
            other.execute(delete(RoleBinding).where(RoleBinding.user_id == state[2][3]["alice"]))
        return result

    monkeypatch.setattr(PolicyMaterialRepository, "insert_if_absent", revoke)
    with pytest.raises(DomainError) as error:
        finish(state, proposal)
    assert error.value.code == "PERMISSION_DENIED"
    assert counts(state) == before
