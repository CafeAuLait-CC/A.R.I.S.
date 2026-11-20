"""Explicit opaque browser sessions; identity and grants remain current database facts."""

import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import AwareDatetime, Field, SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract, ExternalSubject, UserPrincipal
from aris.core.access.service import AccessService
from aris.core.access.session_models import LoginSession
from aris.core.access.session_repo import LoginRepository
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.time import utc


def secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(session_token: str) -> str:
    return hmac.new(session_token.encode(), b"aris-member-csrf-v1", hashlib.sha256).hexdigest()


class SessionGrant(AccessContract):
    token: SecretStr = Field(repr=False)
    csrf: SecretStr = Field(repr=False)
    principal: UserPrincipal
    subject: ExternalSubject
    expires_at: AwareDatetime
    max_age: int | None


class CurrentSession(AccessContract):
    principal: UserPrincipal
    subject: ExternalSubject
    expires_at: AwareDatetime
    csrf: SecretStr = Field(repr=False)


def authentication_required() -> DomainError:
    return DomainError("PERMISSION_DENIED", "Sign in with your personal credential", 401)


class LoginSessionService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        namespace: IdentityNamespace,
        legacy_credentials: Mapping[str, ExternalSubject],
        *,
        reserved_digests: Sequence[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.namespace = sessions, namespace
        self.access = AccessService(sessions)
        self.legacy = dict(legacy_credentials)
        self.reserved = frozenset(reserved_digests) | frozenset(self.legacy)
        self.clock = clock or (lambda: datetime.now(UTC))
        if any(
            len(digest) != 64
            or (subject.provider, subject.namespace) != (namespace.provider, namespace.namespace)
            for digest, subject in self.legacy.items()
        ):
            raise ValueError("legacy login bindings require one explicit namespace")

    def now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("login clock requires a timezone")
        return utc(value)

    @staticmethod
    def _raw(value: str | None) -> str:
        if (
            value is None
            or not 32 <= len(value) <= 256
            or not value.isascii()
            or not value.isprintable()
            or any(character.isspace() for character in value)
        ):
            raise authentication_required()
        return value

    def login(
        self, credential: SecretStr, *, remember: bool, request_id: str | None = None
    ) -> SessionGrant:
        candidate = secret_digest(self._raw(credential.get_secret_value()))
        try:
            with self.sessions.begin() as session:
                repo = LoginRepository(session)
                stored = repo.credential(candidate)
                generation = None
                if stored is not None:
                    if stored.disabled_at is not None or not hmac.compare_digest(
                        stored.token_digest, candidate
                    ):
                        raise authentication_required()
                    identity = repo.login_identity(stored.user_id, self.namespace)
                    generation = stored.generation
                    subject = (
                        ExternalSubject(
                            provider=identity.provider,
                            namespace=identity.namespace,
                            external_id=identity.external_id,
                        )
                        if identity is not None
                        else None
                    )
                else:
                    subject = None
                    for expected, bound_subject in self.legacy.items():
                        if hmac.compare_digest(expected, candidate):
                            subject = bound_subject
                    identity = repo.identity(subject) if subject is not None else None
                if identity is None or subject is None:
                    raise authentication_required()
                principal = self.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise authentication_required()
                now = self.now()
                token = secrets.token_urlsafe(32)
                csrf = csrf_token(token)
                seconds = 7 * 86400 if remember else 8 * 3600
                expires = now + timedelta(seconds=seconds)
                session.add(
                    LoginSession(
                        token_digest=secret_digest(token),
                        user_id=identity.user_id,
                        identity_id=identity.id,
                        credential_generation=generation,
                        credential_digest=candidate,
                        csrf_digest=secret_digest(csrf),
                        issued_at=now,
                        expires_at=expires,
                    )
                )
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=str(principal.user_id),
                        action="access.session.started",
                        target="member-login",
                        request_id=request_id or str(uuid4()),
                        recorded_at=now,
                        changes={"remember": remember, "expires_at": expires.isoformat()},
                    )
                )
                session.flush()
                return SessionGrant(
                    token=token,
                    csrf=csrf,
                    principal=principal,
                    subject=subject,
                    expires_at=expires,
                    max_age=seconds if remember else None,
                )
        except DBAPIError:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Login is unavailable", 503) from None

    def current(self, token: str | None, csrf: str | None = None) -> CurrentSession:
        raw = self._raw(token)
        try:
            with self.sessions() as session, session.no_autoflush:
                repo = LoginRepository(session)
                stored = repo.current(secret_digest(raw), self.now())
                if stored is None:
                    raise authentication_required()
                identity = repo.login_identity(stored.user_id, self.namespace)
                if identity is None or identity.id != stored.identity_id:
                    raise authentication_required()
                subject = ExternalSubject(
                    provider=identity.provider,
                    namespace=identity.namespace,
                    external_id=identity.external_id,
                )
                if stored.credential_generation is None:
                    if self.legacy.get(stored.credential_digest) != subject:
                        raise authentication_required()
                else:
                    credential = repo.user_credential(stored.user_id)
                    if (
                        credential is None
                        or credential.disabled_at is not None
                        or credential.generation != stored.credential_generation
                        or not hmac.compare_digest(
                            credential.token_digest, stored.credential_digest
                        )
                    ):
                        raise authentication_required()
                expected_csrf = csrf_token(raw)
                if not hmac.compare_digest(stored.csrf_digest, secret_digest(expected_csrf)):
                    raise authentication_required()
                if csrf is not None and (
                    len(csrf) != 64
                    or not hmac.compare_digest(stored.csrf_digest, secret_digest(csrf))
                ):
                    raise DomainError("PERMISSION_DENIED", "The page token is invalid", 403)
                principal = self.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    raise authentication_required()
                return CurrentSession(
                    principal=principal,
                    subject=subject,
                    expires_at=utc(stored.expires_at),
                    csrf=expected_csrf,
                )
        except DBAPIError:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Login is unavailable", 503) from None

    def logout(self, token: str | None, *, request_id: str | None = None) -> None:
        raw = self._raw(token)
        with self.sessions.begin() as session:
            record = LoginRepository(session).current(secret_digest(raw), self.now())
            if record is not None:
                record.revoked_at = self.now()
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=record.user_id,
                        action="access.session.ended",
                        target="member-login",
                        request_id=request_id or str(uuid4()),
                        recorded_at=self.now(),
                        changes={},
                    )
                )


class SessionBearerAuthenticator:
    development_only = True
    browser_sessions = True

    def __init__(self, service: LoginSessionService) -> None:
        self.service = service

    def ensure_distinct_credentials(self, reserved_tokens: Sequence[SecretStr]) -> None:
        if any(
            secret_digest(value.get_secret_value()) in self.service.legacy
            for value in reserved_tokens
        ):
            raise ValueError("login credentials must be distinct from reserved credentials")

    def authenticate(self, authorization: str | None) -> ExternalSubject:
        parts = authorization.split(" ") if authorization else []
        if len(parts) != 2 or parts[0] != "Bearer":
            raise authentication_required()
        return self.service.current(parts[1]).subject
