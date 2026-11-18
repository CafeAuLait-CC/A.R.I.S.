import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue, SecretStr, TypeAdapter
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_repo import MANAGE_IDENTITIES, AccessAdministrationRepository
from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    AdministrationResult,
    AssignNodeUser,
    BindRole,
    BootstrapRequest,
    BootstrapResult,
    BootstrapStatus,
    ChangeOutcome,
    DefineRole,
    DirectoryChange,
    IdentityNamespace,
    RegisterIdentity,
    RegistrationMapping,
    RemoveNodeUser,
    UpdateUser,
)
from aris.core.access.history_repo import MappingHistoryRepository
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.access.models import AccessDirectoryState
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject, PermissionName
from aris.core.access.service import AccessService
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.db import transaction_savepoint
from aris.core.events import DomainEvent, OutboxEntry
from aris.core.time import utc


class RegisteredScopes(Protocol):
    def known_in_transaction(self, session: Session, scope: AccessScope) -> bool:
        """Read a public scope catalog; no external I/O or private domain imports."""
        ...


def subject_fingerprint(subject: ExternalSubject) -> str:
    return hashlib.sha256(
        json.dumps(subject.model_dump(), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AccessAdministrationService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        namespace: IdentityNamespace,
        *,
        operator_key: SecretStr | None = None,
        permission_catalog: Sequence[str] = (),
        scopes: RegisteredScopes | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if operator_key is not None and not 32 <= len(operator_key.get_secret_value()) <= 256:
            raise ValueError("operator key must be 32–256 characters")
        self.sessions, self.namespace, self.operator_key = sessions, namespace, operator_key
        self.scopes, self.clock = scopes, clock or (lambda: datetime.now(UTC))
        self.access = AccessService(sessions)
        validate = TypeAdapter(PermissionName)
        self.catalog = frozenset(validate.validate_python(name) for name in permission_catalog) | {
            MANAGE_IDENTITIES
        }

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("administration clock requires a timezone")
        return utc(value)

    def _subject(self, subject: ExternalSubject) -> None:
        if (subject.provider, subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            raise DomainError("PERMISSION_DENIED", "Identity namespace differs", 403)

    def _directory_namespace(self, state: AccessDirectoryState) -> None:
        if (state.provider, state.namespace) != (self.namespace.provider, self.namespace.namespace):
            raise DomainError("CONFLICT", "Directory namespace differs", 409)

    def _scope(self, session: Session, scope: AccessScope) -> None:
        if scope.type == "global":
            return
        if self.scopes is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Scope catalog is not configured", 503)
        if not self.scopes.known_in_transaction(session, scope):
            raise DomainError("VALIDATION_ERROR", "Scope is not registered", 422)

    def _emit(
        self,
        session: Session,
        *,
        actor: str,
        request_id: str,
        operation_id: str,
        revision: int,
        outcomes: tuple[ChangeOutcome, ...],
        reason: str,
        changed: bool,
        bootstrap: bool = False,
    ) -> tuple[str, datetime] | None:
        now = self._now()
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=actor,
                action="access.bootstrap.completed"
                if bootstrap
                else ("access.directory.changed" if changed else "access.directory.noop"),
                target="primary",
                request_id=request_id,
                recorded_at=now,
                changes={
                    "operation_id": operation_id,
                    "revision": revision,
                    "reason": reason,
                    "outcomes": [item.model_dump() for item in outcomes],
                },
            )
        )
        if changed:
            event_id = str(uuid4())
            session.add(
                DomainEvent(
                    event_id=event_id,
                    event_type="core.identity.changed",
                    event_version=1,
                    aggregate_type="identity_directory",
                    aggregate_id="primary",
                    aggregate_version=revision,
                    occurred_at=now,
                    recorded_at=now,
                    actor=actor,
                    correlation_id=request_id,
                    causation_id=operation_id,
                    payload={"revision": revision, "operations": [item.type for item in outcomes]},
                )
            )
            session.flush()
            session.add(OutboxEntry(event_id=event_id, created_at=now))
            return event_id, now
        return None

    def _operator(self, proof: SecretStr) -> None:
        if self.operator_key is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Identity bootstrap is not configured", 503)
        if not hmac.compare_digest(
            self.operator_key.get_secret_value().encode(),
            proof.get_secret_value().encode(),
        ):
            raise DomainError("PERMISSION_DENIED", "Operator authorization is invalid", 403)

    def bootstrap_status(self, operator_proof: SecretStr) -> BootstrapStatus:
        self._operator(operator_proof)
        with self.sessions() as session, session.no_autoflush:
            repo = AccessAdministrationRepository(session)
            state = repo.directory_snapshot()
            if state is not None and state[3]:
                if state[0] is None or state[1] is None:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Directory namespace is invalid", 503
                    )
                return BootstrapStatus(
                    state="initialized",
                    revision=state[2],
                    namespace=IdentityNamespace(provider=state[0], namespace=state[1]),
                )
            return BootstrapStatus(
                state="adoption_required" if repo.has_existing_directory() else "empty", revision=0
            )

    def bootstrap(self, request: BootstrapRequest) -> BootstrapResult:
        self._operator(request.operator_proof)
        self._subject(request.subject)
        with self.sessions.begin() as session:
            repo = AccessAdministrationRepository(session)
            state = repo.lock_directory()
            fingerprint = subject_fingerprint(request.subject)
            if state.completed_at is not None:
                self._directory_namespace(state)
                if state.subject_fingerprint != fingerprint or state.initial_user_id is None:
                    raise DomainError("CONFLICT", "Directory is already initialized", 409)
                return BootstrapResult(
                    state="already_initialized",
                    user_id=state.initial_user_id,
                    revision=state.revision,
                )
            if repo.has_existing_directory():
                raise DomainError("CONFLICT", "Existing identities require explicit adoption", 409)
            user = repo.create_user(request.subject, request.profile)
            role, _, _ = repo.define_role("identity_admin", (MANAGE_IDENTITIES,))
            repo.bind_role(user.id, role.id, AccessScope(type="global", id="*"), True)
            state.initial_user_id, state.subject_fingerprint = user.id, fingerprint
            state.provider, state.namespace = self.namespace.provider, self.namespace.namespace
            state.completed_at, state.revision = self._now(), 1
            session.flush()
            self._emit(
                session,
                actor="operator:bootstrap",
                request_id=request.request_id,
                operation_id=str(request.operation_id),
                revision=1,
                outcomes=(ChangeOutcome(type="bootstrap", entity_id=user.id, changed=True),),
                reason="explicit initial identity administrator",
                changed=True,
                bootstrap=True,
            )
            return BootstrapResult(state="created", user_id=user.id, revision=1)

    def apply(
        self, context: AdministrationContext, batch: AdministrationBatch
    ) -> AdministrationResult:
        with self.sessions.begin() as session:
            return self.apply_in_transaction(session, context, batch)

    def apply_in_transaction(
        self, session: Session, context: AdministrationContext, batch: AdministrationBatch
    ) -> AdministrationResult:
        with transaction_savepoint(session):
            repo = AccessAdministrationRepository(session)
            state = repo.lock_directory()
            if state.completed_at is None:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Identity directory is not initialized", 503
                )
            self._directory_namespace(state)
            self.access.require_in_transaction(
                session,
                AccessRequest(
                    user_id=context.actor_id,
                    permission=MANAGE_IDENTITIES,
                    scope=AccessScope(type="global", id="*"),
                ),
            )
            if not repo.has_login_identity(str(context.actor_id), self.namespace):
                raise DomainError("PERMISSION_DENIED", "Identity namespace is not linked", 403)
            if batch.expected_revision is not None and batch.expected_revision != state.revision:
                raise DomainError("CONFLICT", "Identity directory has changed", 409)
            targets: set[tuple[str, str]] = set()
            for item in batch.changes:
                if isinstance(item, RegisterIdentity):
                    targets.update(
                        (mapping.node_id, mapping.os_username) for mapping in item.mappings
                    )
                elif isinstance(item, (AssignNodeUser, RemoveNodeUser)):
                    targets.add((item.node_id, item.os_username))
            history_repo = MappingHistoryRepository(session)
            before = {key: history_repo.current(*key) for key in targets}
            outcomes = tuple(self._change(session, repo, item) for item in batch.changes)
            session.flush()
            if not repo.active_administrators(self.namespace):
                raise DomainError("CONFLICT", "Cannot remove the last active administrator", 409)
            changed = any(item.changed for item in outcomes)
            if changed:
                state.revision += 1
            emitted = self._emit(
                session,
                actor=str(context.actor_id),
                request_id=context.request_id,
                operation_id=str(context.operation_id),
                revision=state.revision,
                outcomes=outcomes,
                reason=context.reason,
                changed=changed,
            )
            if emitted is not None:
                event_id, effective_at = emitted
                NodeUserHistoryService(
                    self.sessions, clock=self.clock
                )._record_changes_in_transaction(
                    session,
                    context,
                    event_id=event_id,
                    revision=state.revision,
                    effective_at=effective_at,
                    before=before,
                )
            session.flush()
            return AdministrationResult(revision=state.revision, changed=changed, outcomes=outcomes)

    def _assign(
        self,
        session: Session,
        repo: AccessAdministrationRepository,
        item: RegistrationMapping,
        user_id: str,
    ) -> tuple[bool, dict[str, JsonValue]]:
        self._scope(session, AccessScope(type="node", id=item.node_id))
        stored = repo.mapping(item.node_id, item.os_username)
        details: dict[str, JsonValue] = {
            "mapping_key": hashlib.sha256(
                (item.node_id + "\x00" + item.os_username).encode()
            ).hexdigest(),
            "node_id": item.node_id,
            "previous_user_id": stored.user_id if stored else None,
            "user_id": user_id,
            "previous_confidence": stored.confidence if stored else None,
            "previous_source": stored.source if stored else None,
            "confidence": "verified",
            "source": "manual",
        }
        if stored is None:
            if item.expected_user_id is not None:
                raise DomainError("CONFLICT", "Node user mapping has changed", 409)
            repo.add_mapping(item.node_id, item.os_username, user_id)
            return True, details
        if stored.user_id != user_id and str(item.expected_user_id) != stored.user_id:
            raise DomainError("CONFLICT", "Node user is already linked", 409)
        if item.expected_user_id is not None and str(item.expected_user_id) != stored.user_id:
            raise DomainError("CONFLICT", "Node user mapping has changed", 409)
        changed = (
            stored.user_id != user_id
            or stored.source != "manual"
            or stored.confidence != "verified"
        )
        stored.user_id, stored.source, stored.confidence = user_id, "manual", "verified"
        return changed, details

    def _change(
        self, session: Session, repo: AccessAdministrationRepository, item: DirectoryChange
    ) -> ChangeOutcome:
        if isinstance(item, RegisterIdentity):
            self._subject(item.subject)
            identity = repo.identity(item.subject)
            created = identity is None
            user = (
                repo.create_user(item.subject, item.profile)
                if identity is None
                else repo.user(identity.user_id)
            )
            if user is None:
                raise DomainError("DEPENDENCY_UNAVAILABLE", "Identity user is missing", 503)
            changed = created
            mapping_changes: list[JsonValue] = []
            for mapping in item.mappings:
                mapping_changed, detail = self._assign(session, repo, mapping, user.id)
                changed = mapping_changed or changed
                mapping_changes.append(detail)
            return ChangeOutcome(
                type=item.type,
                entity_id=user.id,
                changed=changed,
                details={
                    "created": created,
                    "subject_fingerprint": subject_fingerprint(item.subject),
                    "mappings": mapping_changes,
                },
            )
        if isinstance(item, DefineRole):
            if not set(item.permissions) <= self.catalog:
                raise DomainError("VALIDATION_ERROR", "Role permission is not registered", 422)
            role, changed, previous = repo.define_role(item.name, item.permissions)
            return ChangeOutcome(
                type=item.type,
                entity_id=role.id,
                changed=changed,
                details={
                    "role_name": item.name,
                    "previous_permissions": [*previous],
                    "permissions": [*item.permissions],
                },
            )
        if isinstance(item, RemoveNodeUser):
            self._scope(session, AccessScope(type="node", id=item.node_id))
            stored = repo.mapping(item.node_id, item.os_username)
            if stored is not None and stored.user_id != str(item.expected_user_id):
                raise DomainError("CONFLICT", "Node user mapping has changed", 409)
            if stored is not None:
                repo.remove_mapping(stored)
            key = hashlib.sha256((item.node_id + "\x00" + item.os_username).encode()).hexdigest()
            return ChangeOutcome(
                type=item.type,
                entity_id=key,
                changed=stored is not None,
                details={
                    "node_id": item.node_id,
                    "mapping_key": key,
                    "previous_user_id": stored.user_id if stored else None,
                    "user_id": None,
                },
            )
        user = repo.user(str(item.user_id))
        if user is None:
            raise DomainError("VALIDATION_ERROR", "Identity user is not registered", 422)
        if isinstance(item, AssignNodeUser):
            changed, details = self._assign(session, repo, item, user.id)
            key = hashlib.sha256((item.node_id + "\x00" + item.os_username).encode()).hexdigest()
            return ChangeOutcome(type=item.type, entity_id=key, changed=changed, details=details)
        if isinstance(item, BindRole):
            self._scope(session, item.scope)
            bound_role = repo.role(item.role_name)
            if bound_role is None:
                raise DomainError("VALIDATION_ERROR", "Role is not registered", 422)
            changed = repo.bind_role(user.id, bound_role.id, item.scope, item.present)
            return ChangeOutcome(
                type=item.type,
                entity_id=user.id,
                changed=changed,
                details={
                    "role_id": bound_role.id,
                    "scope": item.scope.model_dump(),
                    "present": item.present,
                },
            )
        assert isinstance(item, UpdateUser)
        changed = (user.display_name, user.timezone, user.status) != (
            item.profile.display_name,
            item.profile.timezone,
            item.status,
        )
        details = {
            "previous_status": user.status,
            "status": item.status,
            "previous_timezone": user.timezone,
            "timezone": item.profile.timezone,
            "previous_name_sha256": hashlib.sha256(user.display_name.encode()).hexdigest(),
            "name_sha256": hashlib.sha256(item.profile.display_name.encode()).hexdigest(),
        }
        user.display_name, user.timezone, user.status = (
            item.profile.display_name,
            item.profile.timezone,
            item.status,
        )
        return ChangeOutcome(type=item.type, entity_id=user.id, changed=changed, details=details)
