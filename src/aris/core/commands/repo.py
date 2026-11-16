from datetime import datetime
from uuid import uuid4

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import CommandContext, CommandRequest


class CommandRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def claim(
        self,
        context: CommandContext,
        request: CommandRequest,
        parameters: dict[str, JsonValue],
        key_sha256: str,
        fingerprint: str,
        now: datetime,
    ) -> tuple[CommandRecord, bool]:
        insert = sqlite_insert if self.session.get_bind().dialect.name == "sqlite" else pg_insert
        inserted = self.session.scalar(
            insert(CommandRecord)
            .values(
                id=str(uuid4()),
                actor_id=str(context.actor_id),
                key_sha256=key_sha256,
                command_type=request.command,
                scope_type=request.scope.type,
                scope_id=request.scope.id,
                fingerprint=fingerprint,
                request_id=context.request_id,
                parameters=parameters,
                state="pending",
                result=None,
                created_at=now,
                completed_at=None,
            )
            .on_conflict_do_nothing(index_elements=["actor_id", "key_sha256"])
            .returning(CommandRecord.id)
        )
        record = self.session.scalars(
            select(CommandRecord)
            .where(
                CommandRecord.actor_id == str(context.actor_id),
                CommandRecord.key_sha256 == key_sha256,
            )
            .with_for_update()
        ).one()
        return record, inserted is not None
