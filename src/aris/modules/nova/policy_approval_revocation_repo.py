"""Append one immutable revocation per approval; never update its statement."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.time import utc
from aris.modules.nova.models import PolicySourceApprovalRevocationRow
from aris.modules.nova.policy_approval_repo import inconsistent_approval
from aris.modules.nova.policy_approval_schemas import StoredPolicyApprovalRevocation
from aris.modules.nova.policy_history_record_schemas import canonical_history_record


class PolicyApprovalRevocationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _metadata(value: StoredPolicyApprovalRevocation) -> dict[str, str]:
        return {
            "id": str(value.id),
            "approval_id": str(value.request.approval_id),
            "approval_fingerprint": value.request.expected_approval_fingerprint,
            "source_fingerprint": value.request.expected_source_fingerprint,
            "revoked_by": str(value.revoked_by),
            "command_id": str(value.command_id),
            "request_id": value.request_id,
            "fingerprint": value.fingerprint,
        }

    def insert(self, value: StoredPolicyApprovalRevocation) -> bool:
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("unsupported approval revocation database")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        return (
            self.session.scalar(
                insert(PolicySourceApprovalRevocationRow)
                .values(
                    **self._metadata(value),
                    revoked_at=value.revoked_at,
                    canonical_revocation=canonical_history_record(value),
                )
                .on_conflict_do_nothing(index_elements=["approval_id"])
                .returning(PolicySourceApprovalRevocationRow.id)
            )
            is not None
        )

    def get(self, approval_id: UUID, owner_id: UUID) -> StoredPolicyApprovalRevocation | None:
        row = (
            self.session.execute(
                select(*PolicySourceApprovalRevocationRow.__table__.c).where(
                    PolicySourceApprovalRevocationRow.approval_id == str(approval_id),
                    PolicySourceApprovalRevocationRow.revoked_by == str(owner_id),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_revocation"]
            if len(raw.encode()) > 8192:
                raise ValueError("revocation exceeds budget")
            value = StoredPolicyApprovalRevocation.model_validate_json(raw)
            if (
                raw != canonical_history_record(value)
                or any(row[key] != item for key, item in self._metadata(value).items())
                or utc(row["revoked_at"]) != value.revoked_at
            ):
                raise ValueError("revocation metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_approval() from None
