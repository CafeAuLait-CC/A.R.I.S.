from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.modules.nova.models import PolicySourceVersion
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference


@dataclass(frozen=True)
class PolicySourceRecord:
    id: str
    name: str
    source_version: int
    canonical_json: str
    fingerprint: str
    created_by: str
    command_id: str
    request_id: str
    reason: str
    created_at: datetime


class PolicyCatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert_if_absent(self, record: PolicySourceRecord) -> bool:
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("unsupported policy source database")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        statement = (
            insert(PolicySourceVersion)
            .values(**record.__dict__)
            .on_conflict_do_nothing(index_elements=["name", "source_version"])
            .returning(PolicySourceVersion.id)
        )
        return self.session.scalar(statement) is not None

    def exact(self, reference: PolicySourceReference) -> PolicySourceRecord | None:
        row = self.session.execute(
            select(
                PolicySourceVersion.id,
                PolicySourceVersion.name,
                PolicySourceVersion.source_version,
                PolicySourceVersion.canonical_json,
                PolicySourceVersion.fingerprint,
                PolicySourceVersion.created_by,
                PolicySourceVersion.command_id,
                PolicySourceVersion.request_id,
                PolicySourceVersion.reason,
                PolicySourceVersion.created_at,
            ).where(
                PolicySourceVersion.name == reference.name,
                PolicySourceVersion.source_version == reference.version,
            )
        ).one_or_none()
        return PolicySourceRecord(*row) if row is not None else None
