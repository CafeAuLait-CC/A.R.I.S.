"""Password authentication and Slack-independent one-time invitation contracts."""

import hashlib
import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_repo import AccessAdministrationRepository
from aris.core.access.password_models import LoginInvitation, PasswordAccount
from aris.core.access.password_repo import PasswordRepository
from aris.core.access.password_schemas import (
    ActivatePasswordInput,
    InvitationReceipt,
    PasswordAccountSummary,
    SetOwnPasswordInput,
)
from aris.core.access.schemas import AccessRequest, AccessScope, ExternalSubject
from aris.core.access.session_models import LoginCredential
from aris.core.access.session_repo import LoginRepository
from aris.core.access.sessions import LoginSessionService, SessionGrant, authentication_required
from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.core.time import utc

Purpose = Literal["activate", "reset"]


class InvitationSender(Protocol):
    def send(self, subject: ExternalSubject, code: SecretStr, purpose: Purpose) -> None: ...


class PasswordAccountService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        login: LoginSessionService,
        key: SecretStr,
        sender: InvitationSender | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions, self.login, self.sender = sessions, login, sender
        self.key = key
        self.clock = clock or (lambda: datetime.now(UTC))
        self.hasher = PasswordHasher(time_cost=3, memory_cost=19456, parallelism=1)
        # Verify the same slow hash for an unknown username.
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def now(self) -> datetime:
        return utc(self.clock())

    def _audit(self, session: Session, user: str, action: str, request_id: str) -> None:
        session.add(
            AuditEntry(
                id=str(uuid4()),
                actor=user,
                action=action,
                target="member-account",
                request_id=request_id,
                recorded_at=self.now(),
                changes={},
            )
        )

    def _throttle(self, action: str, username: str, peer: str) -> None:
        now = self.now()
        # No proxy headers: the trusted HTTP adapter supplies its direct peer.
        with self.sessions.begin() as session:
            repo = PasswordRepository(session)
            windows = [("peer:" + peer, 60, 30), ("account:" + username, 900, 10)]
            if action == "invitation-send":
                windows.append(("resend:" + username, 60, 1))
            for scope, seconds, limit in windows:
                digest = hashlib.sha256((action + ":" + scope).encode()).hexdigest()
                repo.take_rate_window(digest, now, seconds=seconds, limit=limit)

    def summary(self, user: UUID) -> PasswordAccountSummary:
        with self.sessions() as session:
            account = PasswordRepository(session).user_account(str(user))
            credential = LoginRepository(session).user_credential(str(user))
            return PasswordAccountSummary(
                username=account.username if account else None,
                password_ready=bool(
                    account
                    and account.password_hash
                    and credential
                    and credential.disabled_at is None
                ),
            )

    def _subject(self, session: Session, user: str) -> ExternalSubject | None:
        identity = LoginRepository(session).login_identity(user, self.login.namespace)
        if identity is None:
            return None
        return ExternalSubject(
            provider=identity.provider,
            namespace=identity.namespace,
            external_id=identity.external_id,
        )

    def login_password(
        self,
        username: str,
        password: SecretStr,
        *,
        remember: bool,
        peer: str,
        request_id: str,
    ) -> SessionGrant:
        self._throttle("password-login", username, peer)
        with self.sessions() as session:
            account = PasswordRepository(session).account(username)
            stored_hash = account.password_hash if account else None
        try:
            self.hasher.verify(stored_hash or self.dummy_hash, password.get_secret_value())
        except (VerificationError, InvalidHashError):
            raise DomainError("PERMISSION_DENIED", "Incorrect username or password", 401) from None
        if stored_hash is None:
            raise DomainError("PERMISSION_DENIED", "Incorrect username or password", 401)
        try:
            with self.sessions.begin() as session:
                AccessAdministrationRepository(session).lock_directory()
                account = PasswordRepository(session).account(username, lock=True)
                if account is None or account.password_hash != stored_hash:
                    raise authentication_required()
                subject = self._subject(session, account.user_id)
                credential = LoginRepository(session).user_credential(account.user_id)
                if subject is None or credential is None or credential.disabled_at is not None:
                    raise authentication_required()
                return self.login.grant_in_transaction(
                    session,
                    subject,
                    credential.token_digest,
                    credential.generation,
                    remember=remember,
                    request_id=request_id,
                )
        except DBAPIError:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Sign in is temporarily unavailable", 503
            ) from None

    def _code_digest(self, identifier: str, user: str, code: str) -> str:
        body = ("aris-login-invitation-v1:" + identifier + ":" + user + ":" + code).encode()
        return hmac.new(self.key.get_secret_value().encode(), body, hashlib.sha256).hexdigest()

    def request_invitation(
        self,
        username: str,
        purpose: Purpose,
        *,
        peer: str,
        request_id: str,
    ) -> InvitationReceipt:
        if self.sender is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Slack invitations are not configured", 503)
        self._throttle("invitation-send", username, peer)
        identifier, code, now = str(uuid4()), f"{secrets.randbelow(1000000):06d}", self.now()
        subject = None
        with self.sessions.begin() as session:
            AccessAdministrationRepository(session).lock_directory()
            repo = PasswordRepository(session)
            account = repo.account(username, lock=True)
            if account is not None:
                subject = self._subject(session, account.user_id)
                if (purpose == "activate") != (account.password_hash is None):
                    subject = None
            if account is not None and subject is not None:
                previous = repo.latest_invitation(account.user_id)
                if previous is not None and now < utc(previous.created_at) + timedelta(seconds=60):
                    raise DomainError(
                        "RATE_LIMITED", "Please wait a minute before requesting another code", 429
                    )
                identity = LoginRepository(session).login_identity(
                    account.user_id, self.login.namespace
                )
                assert identity is not None
                account.challenge_version += 1
                session.add(
                    LoginInvitation(
                        id=identifier,
                        user_id=account.user_id,
                        identity_id=identity.id,
                        username=username,
                        purpose=purpose,
                        version=account.challenge_version,
                        code_digest=self._code_digest(identifier, account.user_id, code),
                        created_at=now,
                        expires_at=now + timedelta(minutes=10),
                        attempts=0,
                    )
                )
                self._audit(session, account.user_id, "access.invitation.requested", request_id)
        if subject is not None:
            # No open database transaction or durable plaintext outbox during Slack I/O.
            try:
                self.sender.send(subject, SecretStr(code), purpose)
            except Exception:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE",
                    "Code delivery could not be confirmed. Wait a minute and request a new code.",
                    503,
                ) from None
            with self.sessions.begin() as session:
                invitation = PasswordRepository(session).invitation(identifier, lock=True)
                assert invitation is not None
                invitation.delivered_at = self.now()
        return InvitationReceipt(challenge_id=UUID(identifier))

    def _replace_password(
        self,
        session: Session,
        account: PasswordAccount,
        password_hash: str,
        *,
        request_id: str,
        remember: bool,
    ) -> SessionGrant:
        subject = self._subject(session, account.user_id)
        if subject is None:
            raise authentication_required()
        login = LoginRepository(session)
        login.revoke_user(account.user_id, self.now())
        credential = login.user_credential(account.user_id)
        if credential is None:
            credential = LoginCredential(user_id=account.user_id, generation=0)
            session.add(credential)
        credential.generation += 1
        credential.token_digest = secrets.token_hex(32)
        credential.issued_at, credential.disabled_at = self.now(), None
        account.password_hash = password_hash
        account.challenge_version += 1
        self._audit(session, account.user_id, "access.password.changed", request_id)
        session.flush()
        return self.login.grant_in_transaction(
            session,
            subject,
            credential.token_digest,
            credential.generation,
            remember=remember,
            request_id=request_id,
        )

    def activate(
        self,
        data: ActivatePasswordInput,
        purpose: Purpose,
        *,
        peer: str,
        request_id: str,
    ) -> SessionGrant:
        self._throttle("invitation-verify", data.username, peer)
        password_hash = self.hasher.hash(data.password.get_secret_value())
        failure, grant = False, None
        with self.sessions.begin() as session:
            AccessAdministrationRepository(session).lock_directory()
            repo = PasswordRepository(session)
            account = repo.account(data.username, lock=True)
            invitation = repo.invitation(str(data.challenge_id), lock=True)
            identity = (
                LoginRepository(session).login_identity(account.user_id, self.login.namespace)
                if account
                else None
            )
            if (
                account is None
                or invitation is None
                or identity is None
                or invitation.user_id != account.user_id
                or invitation.username != account.username
                or invitation.identity_id != identity.id
                or invitation.version != account.challenge_version
                or invitation.purpose != purpose
                or invitation.delivered_at is None
                or invitation.used_at is not None
                or invitation.attempts >= 5
                or self.now() >= utc(invitation.expires_at)
                or ((purpose == "activate") != (account.password_hash is None))
            ):
                failure = True
            else:
                invitation.attempts += 1
                if not hmac.compare_digest(
                    invitation.code_digest,
                    self._code_digest(invitation.id, account.user_id, data.code.get_secret_value()),
                ):
                    failure = True
                else:
                    invitation.used_at = self.now()
                    grant = self._replace_password(
                        session,
                        account,
                        password_hash,
                        request_id=request_id,
                        remember=data.remember,
                    )
        # Commit incorrect-attempt counters, then report rejection.
        if failure or grant is None:
            raise DomainError(
                "PERMISSION_DENIED",
                "The invitation code is invalid or expired. Request a new code if needed.",
                401,
            )
        return grant

    def set_own_password(
        self,
        session_token: str,
        data: SetOwnPasswordInput,
        *,
        request_id: str,
    ) -> SessionGrant:
        actor = self.login.current(session_token)
        self._throttle(
            "password-change", str(actor.principal.user_id), str(actor.principal.user_id)
        )
        with self.sessions() as session:
            existing = PasswordRepository(session).user_account(str(actor.principal.user_id))
            previous_hash = existing.password_hash if existing else None
        if previous_hash:
            try:
                self.hasher.verify(
                    previous_hash,
                    data.current_password.get_secret_value() if data.current_password else "",
                )
            except (VerificationError, InvalidHashError):
                raise DomainError(
                    "PERMISSION_DENIED", "The current password is incorrect", 401
                ) from None
        password_hash = self.hasher.hash(data.password.get_secret_value())
        with self.sessions.begin() as session:
            AccessAdministrationRepository(session).lock_directory()
            # Recheck the same current session after acquiring the credential-write lock.
            latest = self.login.current(session_token)
            if latest.principal.user_id != actor.principal.user_id:
                raise authentication_required()
            repo = PasswordRepository(session)
            account = repo.user_account(str(actor.principal.user_id))
            if (account.password_hash if account else None) != previous_hash:
                raise DomainError(
                    "CONFLICT", "Your login settings changed. Please sign in again.", 409
                )
            if account is not None and previous_hash and account.username != data.username:
                raise DomainError(
                    "VALIDATION_ERROR", "Ask an administrator to change your login username", 422
                )
            if account is None or account.username != data.username:
                self.login.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=actor.principal.user_id,
                        permission="identity.manage",
                        scope=AccessScope(type="global", id="*"),
                    ),
                )
            repo.reserve(str(actor.principal.user_id), data.username)
            session.flush()
            account = repo.user_account(str(actor.principal.user_id))
            assert account is not None
            return self._replace_password(
                session, account, password_hash, request_id=request_id, remember=data.remember
            )
