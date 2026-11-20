from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.access.password_models import LoginInvitation, LoginRateWindow, PasswordAccount
from aris.core.contracts import DomainError
from aris.core.time import utc


class PasswordRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def account(self, username: str, *, lock: bool = False) -> PasswordAccount | None:
        query = select(PasswordAccount).where(PasswordAccount.username == username)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(query)

    def user_account(self, user: str) -> PasswordAccount | None:
        return self.session.get(PasswordAccount, user, populate_existing=True)

    def accounts_for(self, users: tuple[str, ...]) -> dict[str, PasswordAccount]:
        return {
            a.user_id: a
            for a in self.session.scalars(
                select(PasswordAccount).where(PasswordAccount.user_id.in_(users))
            )
        }

    def reserve(self, user: str, username: str) -> bool:
        other = self.account(username)
        if other is not None and other.user_id != user:
            raise DomainError("CONFLICT", "This login username is already assigned", 409)
        account = self.user_account(user)
        if account is None:
            self.session.add(PasswordAccount(user_id=user, username=username, challenge_version=0))
            self.session.flush()
            return True
        if account.username == username:
            return False
        account.username = username
        account.challenge_version += 1
        self.session.flush()
        return True

    def invitation(self, identifier: str, *, lock: bool = False) -> LoginInvitation | None:
        query = select(LoginInvitation).where(LoginInvitation.id == identifier)
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(query)

    def latest_invitation(self, user: str) -> LoginInvitation | None:
        return self.session.scalar(
            select(LoginInvitation)
            .where(LoginInvitation.user_id == user)
            .order_by(LoginInvitation.created_at.desc(), LoginInvitation.id)
            .limit(1)
        )

    def take_rate_window(self, key: str, now: datetime, *, seconds: int, limit: int) -> None:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        self.session.execute(
            insert(LoginRateWindow)
            .values(key=key, started_at=now, attempts=0)
            .on_conflict_do_nothing()
        )
        window = self.session.scalars(
            select(LoginRateWindow)
            .where(LoginRateWindow.key == key)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if now >= utc(window.started_at) + timedelta(seconds=seconds):
            window.started_at, window.attempts = now, 0
        if window.attempts >= limit:
            raise DomainError("RATE_LIMITED", "Too many attempts. Please try again later.", 429)
        window.attempts += 1
