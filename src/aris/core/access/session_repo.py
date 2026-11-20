from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.models import ExternalIdentity, User
from aris.core.access.schemas import ExternalSubject
from aris.core.access.session_models import LoginCredential, LoginSession


class LoginRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def credential(self, digest: str) -> LoginCredential | None:
        return self.session.scalar(
            select(LoginCredential).where(LoginCredential.token_digest == digest)
        )

    def user_credential(self, user_id: str) -> LoginCredential | None:
        return self.session.get(LoginCredential, user_id, populate_existing=True)

    def identity(self, subject: ExternalSubject) -> ExternalIdentity | None:
        return self.session.scalar(
            select(ExternalIdentity)
            .join(User, User.id == ExternalIdentity.user_id)
            .where(
                ExternalIdentity.provider == subject.provider,
                ExternalIdentity.namespace == subject.namespace,
                ExternalIdentity.external_id == subject.external_id,
                User.status == "active",
            )
        )

    def login_identity(self, user_id: str, namespace: IdentityNamespace) -> ExternalIdentity | None:
        rows = self.session.scalars(
            select(ExternalIdentity)
            .join(User, User.id == ExternalIdentity.user_id)
            .where(
                ExternalIdentity.user_id == user_id,
                ExternalIdentity.provider == namespace.provider,
                ExternalIdentity.namespace == namespace.namespace,
                User.status == "active",
            )
            .limit(2)
        ).all()
        return rows[0] if len(rows) == 1 else None

    def current(self, digest: str, now: datetime) -> LoginSession | None:
        return self.session.scalar(
            select(LoginSession)
            .join(User, User.id == LoginSession.user_id)
            .join(ExternalIdentity, ExternalIdentity.id == LoginSession.identity_id)
            .where(
                LoginSession.token_digest == digest,
                LoginSession.revoked_at.is_(None),
                LoginSession.expires_at > now,
                User.status == "active",
                ExternalIdentity.user_id == LoginSession.user_id,
            )
        )

    def revoke_user(self, user_id: str, now: datetime, *, credential: bool = False) -> None:
        self.session.execute(
            update(LoginSession)
            .where(LoginSession.user_id == user_id, LoginSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        if credential:
            self.session.execute(
                update(LoginCredential)
                .where(LoginCredential.user_id == user_id)
                .values(disabled_at=now)
            )
