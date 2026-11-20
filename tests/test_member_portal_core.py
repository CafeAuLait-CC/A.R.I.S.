from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    BindRole,
    BootstrapRequest,
    DefineRole,
    DirectoryProfile,
    IdentityNamespace,
    RegisterIdentity,
    RegistrationMapping,
    RegistrationRoleGrant,
)
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.directory import AccessDirectoryQueries
from aris.core.access.directory_schemas import DirectoryQuery
from aris.core.access.member_commands import ManageMembersCommand
from aris.core.access.member_repo import MemberRepository
from aris.core.access.member_schemas import EditMember, IssueLoginCredential, ManageMembersRequest
from aris.core.access.members import MemberAdministrationService
from aris.core.access.models import Role, RoleBinding, User
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject
from aris.core.access.session_models import LoginCredential, LoginSession
from aris.core.access.sessions import LoginSessionService, secret_digest
from aris.core.contracts import DomainError
from aris.core.db import build_engine, session_factory

ADMIN_TOKEN = "member-admin-personal-fixture-credential-123456789"
MEMBER_TOKEN = "member-personal-fixture-credential-123456789"
NOW = datetime(2026, 11, 19, 12, tzinfo=UTC)
GLOBAL = AccessScope(type="global", id="*")
ROLES = (
    DefineRole(
        name="admin",
        permissions=(
            "identity.manage",
            "identity.edit_profile",
            "usage.read_own",
            "usage.read_all",
            "usage.read_general",
        ),
    ),
    DefineRole(
        name="member", permissions=("identity.edit_profile", "usage.read_own", "usage.read_general")
    ),
)


class Scopes:
    def known_in_transaction(self, session, scope):
        return scope.type == "node" and scope.id in ("node-a", "node-b")


@pytest.fixture
def members(settings):
    engine = build_engine(settings.database_url)
    sessions = session_factory(engine)
    clock = [NOW]
    namespace = IdentityNamespace(provider="slack", namespace="TDEMO")
    subject = ExternalSubject(provider="slack", namespace="TDEMO", external_id="UADMIN")
    service = AccessAdministrationService(
        sessions,
        namespace,
        operator_key=SecretStr("fixture-operator-key-1234567890123456789"),
        scopes=Scopes(),
        permission_catalog=tuple({p for role in ROLES for p in role.permissions}),
        clock=lambda: clock[0],
        registration_roles=(RegistrationRoleGrant(role_name="member", scope=GLOBAL),),
    )
    boot = service.bootstrap(
        BootstrapRequest(
            subject=subject,
            profile=DirectoryProfile(display_name="Admin", timezone="UTC"),
            operator_proof=service.operator_key,
            request_id="fixture-bootstrap",
        )
    )
    service.apply(
        AdministrationContext(
            actor_id=boot.user_id,
            request_id="fixture-admin-profile",
            reason="Configure portal roles",
        ),
        AdministrationBatch(
            expected_revision=1,
            changes=(
                *ROLES,
                BindRole(user_id=boot.user_id, role_name="admin", scope=GLOBAL, present=True),
            ),
        ),
    )
    login = LoginSessionService(
        sessions, namespace, {secret_digest(ADMIN_TOKEN): subject}, clock=lambda: clock[0]
    )
    manager = MemberAdministrationService(service, login, ROLES)
    try:
        yield manager, boot.user_id, subject, clock
    finally:
        engine.dispose()


def revision(service, admin):
    return (
        AccessDirectoryQueries(service.administration)
        .page(admin, DirectoryQuery(node_ids=("node-a",)))
        .revision
    )


def apply(service, admin, changes):
    request = ManageMembersRequest(
        expected_revision=revision(service, admin),
        changes=tuple(changes),
        reason="Fixture member management",
    )
    command = ManageMembersCommand(service)
    normalized = ManageMembersRequest.model_validate(
        command.normalize(request.model_dump(mode="json"))
    )
    with service.sessions.begin() as session:
        return service.manage_in_transaction(
            session,
            AdministrationContext(
                actor_id=admin,
                operation_id=uuid4(),
                request_id="fixture-members",
                reason=request.reason,
            ),
            normalized,
        )


def register(service, admin, name="UMEMBER", username="member-linux"):
    apply(
        service,
        admin,
        [
            RegisterIdentity(
                subject=ExternalSubject(provider="slack", namespace="TDEMO", external_id=name),
                profile=DirectoryProfile(display_name=name, timezone="UTC"),
                mappings=(RegistrationMapping(node_id="node-a", os_username=username),),
            )
        ],
    )
    page = AccessDirectoryQueries(service.administration).page(
        admin, DirectoryQuery(node_ids=("node-a",), external_ids=(name,))
    )
    return page.members[0].user_id


def issue(service, admin, user, token=MEMBER_TOKEN, generation=None):
    apply(
        service,
        admin,
        [
            IssueLoginCredential(
                user_id=user, token_digest=secret_digest(token), expected_generation=generation
            )
        ],
    )


def test_new_member_role_has_own_and_general_access_without_other_user_detail(members):
    service, admin, _, _ = members
    user = register(service, admin)
    principal = service.access.require_permission(
        AccessRequest(
            user_id=user, permission="usage.read_own", scope=AccessScope(type="node", id="node-a")
        )
    )
    assert principal.user_id == user
    service.access.require_permission(
        AccessRequest(user_id=user, permission="usage.read_general", scope=GLOBAL)
    )
    for permission in ("usage.read_all", "identity.manage"):
        with pytest.raises(DomainError, match="Permission is not granted"):
            service.access.require_permission(
                AccessRequest(user_id=user, permission=permission, scope=GLOBAL)
            )
    summaries = service.summaries(admin, (user,))
    assert summaries[0].role == "member" and summaries[0].bindings[0].role_name == "member"


@pytest.mark.parametrize("role", ("admin", "member"))
def test_reapplying_same_global_role_preserves_bindings_permissions_and_revision(members, role):
    service, admin, _, _ = members
    user = admin if role == "admin" else register(service, admin)

    def binding_ids():
        with service.sessions() as session:
            return tuple(
                session.execute(
                    select(RoleBinding.id, Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
                    .join(Role, Role.id == RoleBinding.role_id)
                    .where(RoleBinding.user_id == str(user))
                    .order_by(Role.name, RoleBinding.scope_type, RoleBinding.scope_id)
                )
            )

    original_ids, original_revision = binding_ids(), revision(service, admin)
    expected_permissions = next(item.permissions for item in ROLES if item.name == role)
    for _ in range(2):
        with service.sessions() as session:
            bindings = MemberRepository(session).bindings(str(user))
        outcome = apply(
            service, admin, [EditMember(user_id=user, role=role, previous_bindings=bindings)]
        )
        assert not outcome["directory"]["changed"]
        assert outcome["revision"] == original_revision == revision(service, admin)
        assert binding_ids() == original_ids
        for permission in expected_permissions:
            service.access.require_permission(
                AccessRequest(user_id=user, permission=permission, scope=GLOBAL)
            )
        if role == "member":
            for permission in ("identity.manage", "usage.read_all"):
                with pytest.raises(DomainError):
                    service.access.require_permission(
                        AccessRequest(user_id=user, permission=permission, scope=GLOBAL)
                    )


def test_batch_conflict_rolls_back_all_registrations_and_cannot_remove_last_admin(members):
    service, admin, _, _ = members
    register(service, admin)
    with service.sessions() as session:
        bindings = MemberRepository(session).bindings(str(admin))
        count = session.scalar(select(func.count()).select_from(User))
    addition = RegisterIdentity(
        subject=ExternalSubject(provider="slack", namespace="TDEMO", external_id="UNEW"),
        profile=DirectoryProfile(display_name="New", timezone="UTC"),
    )
    with pytest.raises(DomainError, match="last active administrator"):
        apply(
            service,
            admin,
            [addition, EditMember(user_id=admin, role="member", previous_bindings=bindings)],
        )
    with service.sessions() as session:
        assert session.scalar(select(func.count()).select_from(User)) == count


def test_session_remembers_refresh_expires_and_logout_revokes_without_plaintext_storage(members):
    service, admin, _, clock = members
    user = register(service, admin)
    issue(service, admin, user)
    grant = service.login.login(SecretStr(MEMBER_TOKEN), remember=True)
    raw = grant.token.get_secret_value()
    assert grant.max_age == 7 * 86400 and grant.principal.user_id == user
    assert service.login.current(raw).csrf.get_secret_value() == grant.csrf.get_secret_value()
    with service.sessions() as session:
        stored = session.get(LoginSession, secret_digest(raw))
        assert stored is not None and stored.token_digest != raw
        assert session.get(LoginCredential, str(user)).token_digest == secret_digest(MEMBER_TOKEN)
    with pytest.raises(DomainError):
        service.login.login(SecretStr(secret_digest(MEMBER_TOKEN)), remember=True)
    clock[0] += timedelta(days=6)
    assert service.login.current(raw).principal.user_id == user
    service.login.logout(raw)
    with pytest.raises(DomainError):
        service.login.current(raw)
    next_grant = service.login.login(SecretStr(MEMBER_TOKEN), remember=False)
    assert next_grant.max_age is None
    clock[0] += timedelta(hours=8, seconds=1)
    with pytest.raises(DomainError):
        service.login.current(next_grant.token.get_secret_value())


def test_suspension_disables_old_login_and_sessions_even_after_reactivation(members):
    service, admin, _, clock = members
    user = register(service, admin)
    issue(service, admin, user)
    grant = service.login.login(SecretStr(MEMBER_TOKEN), remember=True)
    apply(service, admin, [EditMember(user_id=user, active=False)])
    with pytest.raises(DomainError):
        service.login.current(grant.token.get_secret_value())
    apply(service, admin, [EditMember(user_id=user, active=True)])
    with pytest.raises(DomainError):
        service.login.login(SecretStr(MEMBER_TOKEN), remember=True)
    with pytest.raises(DomainError):
        service.login.current(grant.token.get_secret_value())
    rotated = "fresh-rotated-member-fixture-credential-123456789"
    issue(service, admin, user, rotated, generation=1)
    assert service.login.login(SecretStr(rotated), remember=True).principal.user_id == user
    legacy_user = register(service, admin, "ULEGACY", "legacy-linux")
    legacy_token = "fixture-legacy-member-personal-credential-123456789"
    legacy_subject = ExternalSubject(provider="slack", namespace="TDEMO", external_id="ULEGACY")
    service.login = LoginSessionService(
        service.sessions,
        service.administration.namespace,
        {**service.login.legacy, secret_digest(legacy_token): legacy_subject},
        clock=lambda: clock[0],
    )
    old_session = service.login.login(SecretStr(legacy_token), remember=True)
    apply(service, admin, [EditMember(user_id=legacy_user, active=False)])
    apply(service, admin, [EditMember(user_id=legacy_user, active=True)])
    with pytest.raises(DomainError):
        service.login.login(SecretStr(legacy_token), remember=True)
    with pytest.raises(DomainError):
        service.login.current(old_session.token.get_secret_value())


def test_cookie_csrf_is_bound_to_one_session_and_legacy_removal_invalidates_session(members):
    service, _, _, _ = members
    first = service.login.login(SecretStr(ADMIN_TOKEN), remember=True)
    second = service.login.login(SecretStr(ADMIN_TOKEN), remember=True)
    with pytest.raises(DomainError, match="page token"):
        service.login.current(first.token.get_secret_value(), second.csrf.get_secret_value())
    service.login.legacy.clear()
    with pytest.raises(DomainError):
        service.login.current(first.token.get_secret_value())
