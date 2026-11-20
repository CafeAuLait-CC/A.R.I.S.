"""Nova source approvals; directory reads do not load original approval bodies."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.models import PolicySourceApprovalRevocationRow, PolicySourceApprovalRow
from aris.modules.nova.policy_approval_schemas import (
    MAX_APPROVAL_BYTES,
    PolicyApprovalListQuery,
    PolicyApprovalSummary,
    StoredPolicyApproval,
)
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_history_record_schemas import canonical_history_record


def inconsistent_approval() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored policy approval is inconsistent", 503)


class PolicyApprovalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _metadata(value: StoredPolicyApproval) -> dict[str, str | int]:
        p = value.preview
        return {
            "id": str(value.id),
            "source_id": str(p.source_id),
            "name": p.request.source.name,
            "source_version": p.request.source.version,
            "source_fingerprint": p.request.expected_source_fingerprint,
            "requirements_fingerprint": p.request.expected_requirements_fingerprint,
            "approved_by": str(value.approved_by),
            "command_id": str(value.command_id),
            "request_id": value.request_id,
            "fingerprint": value.fingerprint,
        }

    def insert(self, value: StoredPolicyApproval) -> bool:
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("unsupported source approval database")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        return (
            self.session.scalar(
                insert(PolicySourceApprovalRow)
                .values(
                    **self._metadata(value),
                    approved_at=value.approved_at,
                    expires_at=value.expires_at,
                    canonical_approval=canonical_history_record(value),
                )
                .on_conflict_do_nothing(index_elements=["command_id"])
                .returning(PolicySourceApprovalRow.id)
            )
            is not None
        )

    def get(self, id: UUID, owner_id: UUID) -> StoredPolicyApproval | None:
        row = (
            self.session.execute(
                select(*PolicySourceApprovalRow.__table__.c).where(
                    PolicySourceApprovalRow.id == str(id),
                    PolicySourceApprovalRow.approved_by == str(owner_id),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_approval"]
            if len(raw.encode()) > MAX_APPROVAL_BYTES:
                raise ValueError("approval exceeds budget")
            value = StoredPolicyApproval.model_validate_json(raw)
            if (
                raw != canonical_history_record(value)
                or any(row[key] != item for key, item in self._metadata(value).items())
                or utc(row["approved_at"]) != value.approved_at
                or utc(row["expires_at"]) != value.expires_at
            ):
                raise ValueError("approval metadata differs from its original body")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_approval() from None

    def page(
        self, query: PolicyApprovalListQuery, source_id: UUID, owner_id: UUID
    ) -> tuple[PolicyApprovalSummary, ...]:
        row = PolicySourceApprovalRow
        revoked = PolicySourceApprovalRevocationRow
        clauses = [row.source_id == str(source_id), row.approved_by == str(owner_id)]
        if query.before is not None:
            clauses.append(
                or_(
                    row.approved_at < query.before.approved_at,
                    and_(
                        row.approved_at == query.before.approved_at, row.id < str(query.before.id)
                    ),
                )
            )
        values = self.session.execute(
            select(
                row.id,
                row.name,
                row.source_version,
                row.source_fingerprint,
                row.requirements_fingerprint,
                row.approved_by,
                row.approved_at,
                row.expires_at,
                row.fingerprint,
                revoked.id.label("revocation_id"),
                revoked.revoked_at,
                revoked.fingerprint.label("revocation_fingerprint"),
            )
            .outerjoin(revoked, revoked.approval_id == row.id)
            .where(*clauses)
            .order_by(row.approved_at.desc(), row.id.desc())
            .limit(query.limit + 1)
        )
        try:
            return tuple(
                PolicyApprovalSummary(
                    id=value.id,
                    source=PolicySourceReference(name=value.name, version=value.source_version),
                    source_fingerprint=value.source_fingerprint,
                    requirements_fingerprint=value.requirements_fingerprint,
                    approved_by=value.approved_by,
                    approved_at=utc(value.approved_at),
                    expires_at=utc(value.expires_at),
                    fingerprint=value.fingerprint,
                    revocation_id=value.revocation_id,
                    revoked_at=utc(value.revoked_at) if value.revoked_at is not None else None,
                    revocation_fingerprint=value.revocation_fingerprint,
                )
                for value in values
            )
        except (ValueError, TypeError, KeyError):
            raise inconsistent_approval() from None
