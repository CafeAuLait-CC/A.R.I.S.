"""Typed member policy over the existing directory and confirmed command transactions."""

import hashlib
import json
from collections.abc import Sequence
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.orm import Session

from aris.core.access.admin_repo import AccessAdministrationRepository
from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    AssignNodeUser,
    BindRole,
    ChangeOutcome,
    DefineRole,
    DirectoryChange,
    DirectoryProfile,
    RegisterIdentity,
    RemoveNodeUser,
    UpdateUser,
)
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.member_repo import MemberRepository
from aris.core.access.member_schemas import (
    EditMember,
    IssueLoginCredential,
    ManageMembersRequest,
    MemberAccessSummary,
    PortalIdentity,
    ProfileEditRequest,
)
from aris.core.access.password_repo import PasswordRepository
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject
from aris.core.access.session_models import LoginCredential
from aris.core.access.session_repo import LoginRepository
from aris.core.access.sessions import LoginSessionService
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint


class MemberAdministrationService:
    def __init__(
        self,
        administration: AccessAdministrationService,
        login: LoginSessionService,
        role_definitions: Sequence[DefineRole],
    ) -> None:
        self.administration, self.login = administration, login
        self.sessions, self.access = administration.sessions, administration.access
        self.roles = tuple(role_definitions)
        if (
            self.sessions is not login.sessions
            or administration.namespace != login.namespace
            or {role.name for role in self.roles} != {"admin", "member"}
            or len(self.roles) != 2
        ):
            raise ValueError("member roles require one namespace, database and two fixed templates")
        for role in self.roles:
            if ("identity.manage" in role.permissions) != (role.name == "admin"):
                raise ValueError("only the admin role may manage the directory")
        self.policy = {
            role.name: list(role.permissions)
            for role in sorted(self.roles, key=lambda role: role.name)
        }
        self.policy_fingerprint = hashlib.sha256(
            json.dumps(self.policy, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        templates = {role.name: frozenset(role.permissions) for role in self.roles}
        self.privileged_permissions = tuple(sorted(templates["admin"] - templates["member"]))

    def _admin(self, session: Session, user_id: UUID) -> None:
        self.access.require_in_transaction(
            session,
            AccessRequest(
                user_id=user_id,
                permission="identity.manage",
                scope=AccessScope(type="global", id="*"),
            ),
        )
        if not MemberRepository(session).in_namespace(user_id, self.administration.namespace):
            raise DomainError("PERMISSION_DENIED", "Identity namespace differs", 403)

    def identity(self, subject: ExternalSubject) -> PortalIdentity:
        with self.sessions() as session, session.no_autoflush:
            principal = self.access.resolve_external_in_transaction(session, subject)
            if principal is None or (subject.provider, subject.namespace) != (
                self.administration.namespace.provider,
                self.administration.namespace.namespace,
            ):
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            state = AccessAdministrationRepository(session).directory_snapshot()
            if state is None or not state[3]:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Directory is unavailable", 503)
            user = MemberRepository(session).user(principal.user_id)
            return PortalIdentity(
                **principal.model_dump(),
                email=user.email if user else None,
                role=self._role(session, principal.user_id),
                directory_revision=state[2],
                role_policy=self.policy,
            )

    def _role(self, session: Session, user_id: UUID) -> str:
        try:
            self._admin(session, user_id)
            return "admin"
        except DomainError as error:
            if error.code != "PERMISSION_DENIED":
                raise
            return "member"

    def summaries(self, actor_id: UUID, users: tuple[UUID, ...]) -> tuple[MemberAccessSummary, ...]:
        if len(users) > 50 or len(set(users)) != len(users):
            raise DomainError("VALIDATION_ERROR", "Select at most 50 distinct members", 422)
        with self.sessions() as session, session.no_autoflush:
            self._admin(session, actor_id)
            repo = MemberRepository(session)
            identifiers = tuple(str(user) for user in users)
            if repo.namespace_users(identifiers, self.administration.namespace) != set(identifiers):
                raise DomainError("PERMISSION_DENIED", "Member namespace differs", 403)
            all_bindings = repo.bindings_for(identifiers)
            administrators = repo.administrator_users(identifiers)
            credentials = LoginRepository(session).credentials_for(identifiers)
            accounts = PasswordRepository(session).accounts_for(identifiers)
            privileged = repo.privileged_roles(self.privileged_permissions)
            result = []
            for user in users:
                bindings = all_bindings.get(str(user), ())
                if len(bindings) > 32:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Access bindings exceed budget", 503
                    )
                credential = credentials.get(str(user))
                result.append(
                    MemberAccessSummary(
                        user_id=user,
                        login_username=accounts[str(user)].username
                        if str(user) in accounts
                        else None,
                        password_ready=bool(
                            str(user) in accounts
                            and accounts[str(user)].password_hash
                            and credential
                            and credential.disabled_at is None
                        ),
                        role="admin" if str(user) in administrators else "member",
                        bindings=bindings,
                        revoked_when_member=tuple(
                            binding
                            for binding in bindings
                            if (
                                binding.role_name in {"admin", "member"}
                                or binding.role_name in privileged
                            )
                            and not (
                                binding.role_name == "member"
                                and binding.scope == AccessScope(type="global", id="*")
                            )
                        ),
                        credential_generation=credential.generation if credential else None,
                        credential_available=credential is not None
                        and credential.disabled_at is None,
                    )
                )
            self._admin(session, actor_id)
            return tuple(result)

    def manage_in_transaction(
        self, session: Session, context: AdministrationContext, request: ManageMembersRequest
    ) -> dict[str, JsonValue]:
        if request.role_policy_fingerprint != self.policy_fingerprint:
            raise DomainError("CONFLICT", "Member role policy changed; review again", 409)
        with transaction_savepoint(session):
            base_repo = AccessAdministrationRepository(session)
            state = base_repo.lock_directory()
            self._admin(session, context.actor_id)
            if state.revision != request.expected_revision:
                raise DomainError("CONFLICT", "Identity directory has changed", 409)
            self.administration._directory_namespace(state)
            repo, login = MemberRepository(session), LoginRepository(session)
            changes: list[DirectoryChange] = list(self.roles)
            credentials = []
            username_changes: list[tuple[str, str]] = []
            disabled: set[str] = set()
            for item in request.changes:
                if isinstance(item, RegisterIdentity):
                    if base_repo.identity(item.subject) is not None:
                        raise DomainError("CONFLICT", "Member is already registered", 409)
                    changes.append(item)
                    continue
                if isinstance(item, (AssignNodeUser, RemoveNodeUser)):
                    target = (
                        item.user_id if isinstance(item, AssignNodeUser) else item.expected_user_id
                    )
                    if not repo.in_namespace(target, self.administration.namespace):
                        raise DomainError("PERMISSION_DENIED", "Member namespace differs", 403)
                    changes.append(item)
                    continue
                if not repo.in_namespace(item.user_id, self.administration.namespace):
                    raise DomainError("PERMISSION_DENIED", "Member namespace differs", 403)
                user = repo.user(item.user_id)
                if user is None:
                    raise DomainError("VALIDATION_ERROR", "Member is not registered", 422)
                if isinstance(item, IssueLoginCredential):
                    if item.user_id == context.actor_id:
                        raise DomainError(
                            "VALIDATION_ERROR",
                            "Use another administrator to rotate your own login credential",
                            422,
                        )
                    if user.status != "active" or item.token_digest in self.login.reserved:
                        raise DomainError(
                            "VALIDATION_ERROR", "Login credential is not allowed", 422
                        )
                    current = login.user_credential(user.id)
                    if (current.generation if current else None) != item.expected_generation:
                        raise DomainError("CONFLICT", "Login credential changed", 409)
                    other = login.credential(item.token_digest)
                    if other is not None and other.user_id != user.id:
                        raise DomainError("CONFLICT", "Login credential is already assigned", 409)
                    credentials.append(item)
                    continue
                assert isinstance(item, EditMember)
                if item.login_username is not None:
                    username_changes.append((str(item.user_id), item.login_username))
                if item.role is not None:
                    previous = repo.bindings(user.id)
                    if previous != tuple(
                        sorted(
                            item.previous_bindings,
                            key=lambda binding: (
                                binding.role_name,
                                binding.scope.type,
                                binding.scope.id,
                            ),
                        )
                    ):
                        raise DomainError("CONFLICT", "Member access changed; review again", 409)
                    privileged = repo.privileged_roles(self.privileged_permissions)
                    target_scope = AccessScope(type="global", id="*")
                    changes.extend(
                        BindRole(
                            user_id=item.user_id,
                            role_name=binding.role_name,
                            scope=binding.scope,
                            present=False,
                        )
                        for binding in previous
                        if (
                            binding.role_name in {"admin", "member"}
                            or (item.role == "member" and binding.role_name in privileged)
                        )
                        and not (binding.role_name == item.role and binding.scope == target_scope)
                    )
                    if not any(
                        binding.role_name == item.role and binding.scope == target_scope
                        for binding in previous
                    ):
                        changes.append(
                            BindRole(
                                user_id=item.user_id,
                                role_name=item.role,
                                scope=target_scope,
                                present=True,
                            )
                        )
                if item.profile is not None or item.active is not None:
                    changes.append(
                        UpdateUser(
                            user_id=item.user_id,
                            profile=item.profile
                            or DirectoryProfile(
                                display_name=user.display_name,
                                timezone=user.timezone,
                                email=user.email,
                            ),
                            status=("active" if item.active else "suspended")
                            if item.active is not None
                            else user.status,
                        )
                    )
                if item.active is False:
                    disabled.add(user.id)
            if len(changes) > 32:
                raise DomainError(
                    "VALIDATION_ERROR",
                    "This selection exceeds 32 access changes; split the batch",
                    422,
                )
            if disabled & {str(item.user_id) for item in credentials}:
                raise DomainError(
                    "VALIDATION_ERROR", "Cannot issue login access to a removed member", 422
                )
            if len({item.user_id for item in credentials}) != len(credentials):
                raise DomainError(
                    "VALIDATION_ERROR", "Select one credential change per member", 422
                )
            result = self.administration.apply_in_transaction(
                session,
                context,
                AdministrationBatch(
                    expected_revision=request.expected_revision, changes=tuple(changes)
                ),
            )
            for item in request.changes:
                if isinstance(item, RegisterIdentity) and item.login_username is not None:
                    identity = login.identity(item.subject)
                    if identity is None:
                        raise DomainError(
                            "CONFLICT", "Registered login identity is unavailable", 409
                        )
                    username_changes.append((identity.user_id, item.login_username))
            account_outcomes = []
            for user_id, username in username_changes:
                if user_id in disabled:
                    raise DomainError(
                        "VALIDATION_ERROR", "Cannot enable login for a removed member", 422
                    )
                if PasswordRepository(session).reserve(user_id, username):
                    account_outcomes.append(
                        ChangeOutcome(
                            type="reserve_login_username",
                            entity_id=user_id,
                            changed=True,
                            details={"username": username},
                        )
                    )
            for user_id in disabled:
                login.revoke_user(user_id, self.login.now(), credential=True)
            outcomes = []
            for item in credentials:
                current = login.user_credential(str(item.user_id))
                generation = (current.generation if current else 0) + 1
                login.revoke_user(str(item.user_id), self.login.now())
                if current is None:
                    current = LoginCredential(user_id=str(item.user_id))
                    session.add(current)
                current.token_digest, current.generation = item.token_digest, generation
                current.issued_at, current.disabled_at = self.login.now(), None
                outcomes.append(
                    ChangeOutcome(
                        type=item.type,
                        entity_id=str(item.user_id),
                        changed=True,
                        details={"generation": generation},
                    )
                )
            if outcomes or account_outcomes:
                state.revision += 1
                self.administration._emit(
                    session,
                    actor=str(context.actor_id),
                    request_id=context.request_id,
                    operation_id=str(context.operation_id),
                    revision=state.revision,
                    outcomes=tuple(outcomes + account_outcomes),
                    reason=context.reason,
                    changed=True,
                )
            session.flush()
            return {
                "revision": state.revision,
                "directory": result.model_dump(mode="json"),
                "credentials_changed": len(outcomes),
                "login_usernames_changed": len(account_outcomes),
            }

    def edit_profile_in_transaction(
        self, session: Session, context: AdministrationContext, request: ProfileEditRequest
    ) -> dict[str, JsonValue]:
        with transaction_savepoint(session):
            repo = AccessAdministrationRepository(session)
            state = repo.lock_directory()
            self.administration._directory_namespace(state)
            self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=context.actor_id,
                    permission="identity.edit_profile",
                    scope=AccessScope(type="global", id="*"),
                ),
            )
            if not repo.has_login_identity(str(context.actor_id), self.administration.namespace):
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            if state.revision != request.expected_revision:
                raise DomainError("CONFLICT", "Identity directory has changed", 409)
            user = repo.user(str(context.actor_id))
            if user is None:
                raise DomainError("PERMISSION_DENIED", "Current identity is unavailable", 403)
            outcome = self.administration._change(
                session,
                repo,
                UpdateUser(user_id=context.actor_id, profile=request.profile, status=user.status),
            )
            if outcome.changed:
                state.revision += 1
            self.administration._emit(
                session,
                actor=str(context.actor_id),
                request_id=context.request_id,
                operation_id=str(context.operation_id),
                revision=state.revision,
                outcomes=(outcome,),
                reason="Member updated their own profile",
                changed=outcome.changed,
            )
            session.flush()
            return {"revision": state.revision, "profile": request.profile.model_dump(mode="json")}
