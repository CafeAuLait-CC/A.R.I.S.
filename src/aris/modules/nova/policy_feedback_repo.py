"""Nova-owned bounded queries for immutable human feedback."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.models import PolicyReviewFeedbackRow
from aris.modules.nova.policy_feedback_schemas import (
    MAX_POLICY_FEEDBACK_BYTES,
    PolicyFeedbackListQuery,
    StoredPolicyFeedback,
    canonical_policy_feedback,
)


class PolicyFeedbackRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, value: StoredPolicyFeedback) -> bool:
        request, material = value.request, value.request.material
        insert = (
            pg_insert if self.session.get_bind().dialect.name == "postgresql" else sqlite_insert
        )
        statement = (
            insert(PolicyReviewFeedbackRow)
            .values(
                id=str(value.id),
                source_id=str(value.source_id),
                name=request.source.name,
                source_version=request.source.version,
                source_fingerprint=request.expected_source_fingerprint,
                material_id=str(material.id) if material else None,
                material_fingerprint=material.fingerprint if material else None,
                kind=request.kind,
                created_by=str(value.created_by),
                command_id=str(value.command_id),
                request_id=value.request_id,
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_feedback=canonical_policy_feedback(value),
            )
            .on_conflict_do_nothing(index_elements=["command_id"])
            .returning(PolicyReviewFeedbackRow.id)
        )
        return self.session.scalar(statement) is not None

    @staticmethod
    def _read(row: RowMapping) -> StoredPolicyFeedback:
        try:
            raw = row["canonical_feedback"]
            if len(raw.encode()) > MAX_POLICY_FEEDBACK_BYTES:
                raise ValueError("feedback exceeds budget")
            value = StoredPolicyFeedback.model_validate_json(raw)
            request, material = value.request, value.request.material
            expected = {
                "id": str(value.id),
                "source_id": str(value.source_id),
                "name": request.source.name,
                "source_version": request.source.version,
                "source_fingerprint": request.expected_source_fingerprint,
                "material_id": str(material.id) if material else None,
                "material_fingerprint": material.fingerprint if material else None,
                "kind": request.kind,
                "created_by": str(value.created_by),
                "command_id": str(value.command_id),
                "request_id": value.request_id,
                "fingerprint": value.fingerprint,
            }
            if (
                raw != canonical_policy_feedback(value)
                or any(row[key] != item for key, item in expected.items())
                or utc(row["created_at"]) != value.created_at
            ):
                raise ValueError("feedback metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored policy feedback is inconsistent", 503
            ) from None

    def get(self, id: UUID) -> StoredPolicyFeedback | None:
        row = (
            self.session.execute(
                select(*PolicyReviewFeedbackRow.__table__.c).where(
                    PolicyReviewFeedbackRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        return self._read(row) if row is not None else None

    def page(
        self, query: PolicyFeedbackListQuery, source_id: UUID
    ) -> tuple[StoredPolicyFeedback, ...]:
        row = PolicyReviewFeedbackRow
        clauses = [row.source_id == str(source_id)]
        if query.before is not None:
            clauses.append(
                or_(
                    row.created_at < query.before.created_at,
                    and_(row.created_at == query.before.created_at, row.id < str(query.before.id)),
                )
            )
        values = self.session.execute(
            select(*row.__table__.c)
            .where(*clauses)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(query.limit + 1)
        ).mappings()
        return tuple(self._read(value) for value in values)
