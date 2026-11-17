from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from aris.core.access.admin_repo import AccessAdministrationRepository
from aris.core.access.admin_schemas import RegisterIdentity, RegistrationMapping
from aris.core.access.directory import AccessDirectoryQueries
from aris.core.access.directory_schemas import DirectoryQuery
from aris.core.access.mapping_command import IdentityMappingCommand, IdentityMappingRequest
from aris.core.access.models import (
    AccessDirectoryState,
    ExternalIdentity,
    NodeUserMapping,
    RoleBinding,
    User,
)
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import (
    ExternalCommandContext,
    ExternalCommandSubmit,
    UserCommandInput,
)
from aris.core.commands.intake import CommandIntakeService
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.commands.worker import CommandWorker
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from tests.test_administration import (
    KEY,
    NAMESPACE,
    NOW,
    PROFILE,
    initialized,
    register,
    subject,
)
from tests.test_administration import admin_state as admin_state
from tests.test_command_intake import LIMITS, WORKER_LIMITS
from tests.test_user_commands import LIMITS as GATEWAY_LIMITS


def counts(sessions):
    with sessions() as session:
        return tuple(
            session.scalar(select(func.count()).select_from(model))
            for model in (User, NodeUserMapping, DomainEvent, CommandRecord)
        )


def test_directory_page_has_stable_external_cursor_and_only_requested_node_mappings(admin_state):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    register(
        service,
        admin,
        mappings=(
            RegistrationMapping(node_id="node-a", os_username="a-person"),
            RegistrationMapping(node_id="node-b", os_username="b-person"),
        ),
    )
    with sessions.begin() as session:
        session.add(
            ExternalIdentity(
                id=str(uuid4()),
                user_id=str(admin),
                provider="slack",
                namespace="OTHER",
                external_id="UOTHER",
            )
        )
    before = counts(sessions)
    query = AccessDirectoryQueries(service)
    page = query.page(admin, DirectoryQuery(node_ids=("node-a",), limit=1))
    assert len(page.members) == 1 and page.members[0].external_id == "UADMIN"
    assert page.next_cursor == "UADMIN" and page.revision == 2
    next_page = query.page(
        admin, DirectoryQuery(node_ids=("node-a",), limit=1, after=page.next_cursor)
    )
    assert next_page.next_cursor is None and next_page.members[0].external_id == "UMEMBER"
    assert [(link.node_id, link.os_username) for link in next_page.members[0].mappings] == [
        ("node-a", "a-person")
    ]
    assert next_page.members[0].mappings[0].confidence == "verified"
    assert counts(sessions) == before


@pytest.mark.parametrize("change", ["role", "scope", "suspended", "login"])
def test_directory_requires_current_global_permission_active_user_and_namespace_login(
    admin_state, change
):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    with sessions.begin() as session:
        if change == "role":
            session.execute(delete(RoleBinding))
        elif change == "scope":
            binding = session.scalar(select(RoleBinding))
            binding.scope_type, binding.scope_id = "node", "node-a"
        elif change == "suspended":
            session.get(User, str(admin)).status = "suspended"
        else:
            session.execute(delete(ExternalIdentity))
    with pytest.raises(DomainError) as refused:
        AccessDirectoryQueries(service).page(admin, DirectoryQuery(node_ids=("node-a",)))
    assert refused.value.code == "PERMISSION_DENIED"


def test_uninitialized_read_does_not_create_a_directory_and_unknown_nodes_are_not_silent(
    admin_state,
):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    query = AccessDirectoryQueries(service)
    with pytest.raises(DomainError) as invalid:
        query.page(admin, DirectoryQuery(node_ids=("missing-node",)))
    assert invalid.value.code == "VALIDATION_ERROR"
    with sessions.begin() as session:
        session.execute(delete(AccessDirectoryState))
    with pytest.raises(DomainError) as unavailable:
        query.page(admin, DirectoryQuery(node_ids=("node-a",)))
    assert unavailable.value.code == "DEPENDENCY_UNAVAILABLE"
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(AccessDirectoryState)) == 0


@pytest.mark.parametrize("change", ["revision", "permission"])
def test_directory_rechecks_revision_and_permission_after_read(admin_state, monkeypatch, change):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    original = AccessAdministrationRepository.directory_mappings

    def changed(repo, users, nodes):
        result = original(repo, users, nodes)
        if change == "revision":
            register(service, admin)
        else:
            with sessions.begin() as other:
                other.execute(delete(RoleBinding))
        return result

    monkeypatch.setattr(AccessAdministrationRepository, "directory_mappings", changed)
    with pytest.raises(DomainError) as refused:
        AccessDirectoryQueries(service).page(admin, DirectoryQuery(node_ids=("node-a",)))
    assert refused.value.code == ("CONFLICT" if change == "revision" else "PERMISSION_DENIED")


def test_directory_never_silently_truncates_a_members_existing_aliases(admin_state):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    with sessions.begin() as session:
        session.add_all(
            [
                NodeUserMapping(
                    node_id="node-a",
                    os_username=f"alias{index}",
                    user_id=str(admin),
                    confidence="verified",
                    source="manual",
                )
                for index in range(129)
            ]
        )
    with pytest.raises(DomainError) as unavailable:
        AccessDirectoryQueries(service).page(admin, DirectoryQuery(node_ids=("node-a",)))
    assert unavailable.value.code == "DEPENDENCY_UNAVAILABLE"


def test_directory_read_preserves_no_flush_contract_and_postgres_read_only(admin_state):
    service, sessions, engine = admin_state
    admin = initialized(admin_state)
    with sessions.begin() as session:
        if engine.dialect.name == "postgresql":
            from sqlalchemy import text

            session.execute(text("SET TRANSACTION READ ONLY"))
        user = session.get(User, str(admin))
        user.display_name = "Uncommitted private edit"
        page = AccessDirectoryQueries(service).page_in_transaction(
            session, admin, DirectoryQuery(node_ids=("node-a", "node-b"))
        )
        assert (
            page.members[0].profile.display_name == PROFILE.display_name and user in session.dirty
        )
        session.rollback()


@pytest.mark.parametrize(
    "query",
    [
        {"node_ids": ()},
        {"node_ids": ("node-a",) * 2},
        {"node_ids": ("node-a",), "limit": 51},
        {"node_ids": ("node-a",), "limit": True},
    ],
)
def test_directory_rejects_unbounded_or_ambiguous_query(query):
    with pytest.raises(ValidationError):
        DirectoryQuery(**query)


def test_directory_selection_reads_only_registered_requested_ids_and_never_enrolls_missing(
    admin_state,
):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    query = AccessDirectoryQueries(service)
    page = query.page(
        admin, DirectoryQuery(node_ids=("node-a",), external_ids=("UADMIN", "UMISSING"))
    )
    assert [member.external_id for member in page.members] == ["UADMIN"]
    assert page.next_cursor is None and page.revision == 1
    assert query.page(admin, DirectoryQuery(node_ids=("node-a",), external_ids=())).members == ()
    assert counts(sessions)[:3] == (1, 0, 1)


@pytest.fixture
def mapping_state(admin_state):
    service, sessions, _ = admin_state
    admin = initialized(admin_state)
    commands = CommandService(
        sessions, [IdentityMappingCommand(service)], confirmation_key=KEY, clock=lambda: NOW
    )
    intake = CommandIntakeService(
        commands, NAMESPACE, LIMITS, confirmation_key_id="fixture-directory-key"
    )
    gateway = UserCommandGateway(intake, ["identity.map_users"], GATEWAY_LIMITS)
    return gateway, CommandWorker(intake, WORKER_LIMITS), admin_state, admin


def mapping_request(*, revision=1, **changes):
    return UserCommandInput(
        command="identity.map_users",
        parameters={
            "expected_revision": revision,
            "reason": "Explicit two-node attribution",
            "changes": [
                RegisterIdentity(
                    subject=subject("UPERSON"),
                    profile=PROFILE,
                    mappings=(
                        RegistrationMapping(node_id="node-a", os_username="alpha"),
                        RegistrationMapping(node_id="node-b", os_username="beta"),
                    ),
                ).model_dump(mode="json")
            ],
            **changes,
        },
    )


def submit_mapping(state):
    return state[0].submit(
        ExternalCommandSubmit(
            subject=subject(),
            request_id="fixture-mapping",
            receipt_key="mapping-input",
            idempotency_key="mapping-intent",
        ),
        mapping_request(),
    )


def confirm_mapping(state, outcome):
    return state[0].confirm(
        ExternalCommandContext(
            subject=subject(), request_id="fixture-map-confirm", receipt_key="mapping-confirm"
        ),
        ConfirmCommand(command_id=outcome.command_id, proof=outcome.proof),
    )


def test_mapping_command_confirmed_worker_changes_two_node_links_once_without_booking_roles(
    mapping_state,
):
    gateway, worker, (_, sessions, _), admin = mapping_state
    pending = submit_mapping(mapping_state)
    assert pending.outcome.state == "awaiting_confirmation" and counts(sessions)[:3] == (1, 0, 1)
    confirm_mapping(mapping_state, pending.outcome)
    assert counts(sessions)[:3] == (1, 0, 1)
    assert worker.run_once().state == "succeeded"
    assert counts(sessions) == (2, 2, 2, 1)
    replay = submit_mapping(mapping_state)
    assert replay.duplicate and replay.outcome.state == "succeeded"
    assert worker.run_once() is None
    with sessions() as session:
        member = session.scalar(
            select(ExternalIdentity.user_id).where(ExternalIdentity.external_id == "UPERSON")
        )
        assert (
            session.scalar(
                select(func.count()).select_from(RoleBinding).where(RoleBinding.user_id == member)
            )
            == 0
        )
        assert session.get(AccessDirectoryState, "primary").revision == 2
    assert gateway.definition("identity.map_users").required_permission == "identity.manage"


@pytest.mark.parametrize("change", ["revision", "permission", "unknown-node", "failure"])
def test_mapping_worker_rechecks_and_rolls_back_without_partial_identity_updates(
    mapping_state, monkeypatch, change
):
    _, worker, (service, sessions, _), admin = mapping_state
    pending = submit_mapping(mapping_state)
    confirm_mapping(mapping_state, pending.outcome)
    if change == "revision":
        register(service, admin, "UINDEPENDENT")
    elif change == "permission":
        with sessions.begin() as session:
            session.execute(delete(RoleBinding))
    elif change == "unknown-node":
        monkeypatch.setattr(service.scopes, "known_in_transaction", lambda *args: False)
    else:
        original = service._emit

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("fixture interrupted directory mutation")

        monkeypatch.setattr(service, "_emit", fail)
    before = counts(sessions)
    assert worker.run_once().state == "failed"
    assert counts(sessions) == before
    with sessions() as session:
        assert (
            session.scalar(
                select(ExternalIdentity).where(ExternalIdentity.external_id == "UPERSON")
            )
            is None
        )


@pytest.mark.parametrize("operation", ["define_role", "bind_role", "update_user"])
def test_mapping_command_does_not_expose_role_or_user_status_mutations(operation):
    with pytest.raises(ValidationError):
        IdentityMappingRequest.model_validate(
            {
                "expected_revision": 1,
                "reason": "Cannot grant roles here",
                "changes": [{"type": operation, "user_id": str(uuid4())}],
            }
        )
