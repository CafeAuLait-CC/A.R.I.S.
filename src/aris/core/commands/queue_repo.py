import hashlib
from datetime import datetime
from typing import Literal
from uuid import uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.commands.models import (
    CommandAdmission,
    CommandIntakeReceipt,
    CommandJob,
    CommandRecord,
)
from aris.core.commands.queue_schemas import CommandIntakeContext


class CommandQueueRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def claim_receipt(
        self,
        context: CommandIntakeContext,
        kind: Literal["submit", "confirm", "withdraw"],
        fingerprint: str,
        subject_fingerprint: str,
        now: datetime,
    ) -> tuple[CommandIntakeReceipt, bool]:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        key = hashlib.sha256(context.receipt_key.encode()).hexdigest()
        inserted = self.session.scalar(
            insert(CommandIntakeReceipt)
            .values(
                id=str(uuid4()),
                actor_id=str(context.actor_id),
                key_sha256=key,
                fingerprint=fingerprint,
                subject_fingerprint=subject_fingerprint,
                kind=kind,
                state="receiving",
                command_id=None,
                error_code=None,
                error_status=None,
                retryable=False,
                attempts=1,
                request_id=context.request_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=["actor_id", "key_sha256"])
            .returning(CommandIntakeReceipt.id)
        )
        receipt = self.session.scalars(
            select(CommandIntakeReceipt)
            .where(
                CommandIntakeReceipt.actor_id == str(context.actor_id),
                CommandIntakeReceipt.key_sha256 == key,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        return receipt, inserted is not None

    def by_key(self, actor_id: str, key: str) -> CommandRecord | None:
        return self.session.scalar(
            select(CommandRecord)
            .where(
                CommandRecord.actor_id == actor_id,
                CommandRecord.key_sha256 == hashlib.sha256(key.encode()).hexdigest(),
            )
            .execution_options(populate_existing=True)
        )

    def command(self, command_id: str, *, lock: bool = False) -> CommandRecord | None:
        statement = (
            select(CommandRecord)
            .where(CommandRecord.id == command_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def admission(self, command_id: str) -> CommandAdmission | None:
        return self.session.get(CommandAdmission, command_id, populate_existing=True)

    def capacity_guard(self) -> bool:
        if self.session.get_bind().dialect.name == "postgresql":
            key = int.from_bytes(
                hashlib.sha256(b"aris.command.admission.capacity").digest()[:8], signed=True
            )
            return bool(
                self.session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
            )
        return True

    def pending_count(self, now: datetime) -> int:
        count = self.session.scalar(
            select(func.count())
            .select_from(CommandRecord)
            .where(
                CommandRecord.execution_not_after.is_not(None),
                or_(
                    CommandRecord.state == "queued",
                    and_(
                        CommandRecord.state == "awaiting_confirmation",
                        CommandRecord.confirmation_expires_at > now,
                    ),
                ),
            )
        )
        return int(count or 0)

    def add_admission(self, admission: CommandAdmission) -> None:
        self.session.add(admission)

    def add_job(self, command_id: str, now: datetime) -> CommandJob:
        job = CommandJob(
            id=str(uuid4()),
            command_id=command_id,
            state="queued",
            attempts=0,
            available_at=now,
            lease_token=None,
            lease_expires_at=None,
            last_error_code=None,
            created_at=now,
            completed_at=None,
        )
        self.session.add(job)
        return job

    def job_reference(self, command_id: str, actor_id: str) -> str | None:
        return self.session.scalar(
            select(CommandJob.id)
            .join(CommandRecord, CommandRecord.id == CommandJob.command_id)
            .where(CommandJob.command_id == command_id, CommandRecord.actor_id == actor_id)
        )

    def job(self, job_id: str, *, lock: bool = False) -> CommandJob | None:
        statement = (
            select(CommandJob)
            .where(CommandJob.id == job_id)
            .execution_options(populate_existing=True)
        )
        if lock:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def command_namespace(self, command_id: str) -> tuple[str, str] | None:
        row = self.session.execute(
            select(CommandAdmission.provider, CommandAdmission.namespace).where(
                CommandAdmission.command_id == command_id
            )
        ).one_or_none()
        return (row[0], row[1]) if row else None

    def next_job(
        self, now: datetime, *, command_types: tuple[str, ...], namespace: IdentityNamespace
    ) -> CommandJob | None:
        return self.session.scalar(
            select(CommandJob)
            .join(CommandRecord, CommandRecord.id == CommandJob.command_id)
            .join(CommandAdmission, CommandAdmission.command_id == CommandRecord.id)
            .where(
                CommandRecord.command_type.in_(command_types),
                CommandAdmission.provider == namespace.provider,
                CommandAdmission.namespace == namespace.namespace,
                or_(
                    and_(CommandJob.state == "queued", CommandJob.available_at <= now),
                    and_(CommandJob.state == "running", CommandJob.lease_expires_at <= now),
                ),
            )
            .order_by(CommandJob.available_at, CommandJob.created_at, CommandJob.id)
            .limit(1)
            .with_for_update(of=CommandJob, skip_locked=True)
            .execution_options(populate_existing=True)
        )
