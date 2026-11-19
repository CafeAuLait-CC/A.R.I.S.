"""Nova-owned bounded storage queries for immutable example review material."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.models import PolicyReviewMaterialRow
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_material_schemas import (
    MAX_POLICY_MATERIAL_BYTES,
    PolicyMaterialListQuery,
    PolicyMaterialSummary,
    StoredPolicyReviewMaterial,
    canonical_policy_material,
)


def inconsistent_material() -> DomainError:
    return DomainError(
        "DEPENDENCY_UNAVAILABLE", "Stored policy review material is inconsistent", 503
    )


class PolicyMaterialRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def scope(self, id: UUID) -> PolicySourceReference | None:
        row = self.session.execute(
            select(PolicyReviewMaterialRow.name, PolicyReviewMaterialRow.source_version).where(
                PolicyReviewMaterialRow.id == str(id)
            )
        ).one_or_none()
        try:
            return PolicySourceReference(name=row[0], version=row[1]) if row is not None else None
        except (ValueError, TypeError):
            raise inconsistent_material() from None

    def insert_if_absent(self, value: StoredPolicyReviewMaterial) -> bool:
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("unsupported policy material database")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        request = value.request
        statement = (
            insert(PolicyReviewMaterialRow)
            .values(
                id=str(value.id),
                source_id=str(value.source_id),
                name=request.source.name,
                source_version=request.source.version,
                source_fingerprint=request.expected_source_fingerprint,
                implementation_ref=value.implementation_ref,
                scenarios=len(request.scenarios),
                created_by=str(value.created_by),
                command_id=str(value.command_id),
                request_id=value.request_id,
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_material=canonical_policy_material(value),
            )
            .on_conflict_do_nothing(index_elements=["command_id"])
            .returning(PolicyReviewMaterialRow.id)
        )
        return self.session.scalar(statement) is not None

    def get(self, id: UUID) -> StoredPolicyReviewMaterial | None:
        row = (
            self.session.execute(
                select(*PolicyReviewMaterialRow.__table__.c).where(
                    PolicyReviewMaterialRow.id == str(id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_material"]
            if len(raw.encode()) > MAX_POLICY_MATERIAL_BYTES:
                raise ValueError("material exceeds budget")
            value = StoredPolicyReviewMaterial.model_validate_json(raw)
            request = value.request
            if canonical_policy_material(value) != raw or (
                str(value.id),
                str(value.source_id),
                request.source.name,
                request.source.version,
                request.expected_source_fingerprint,
                value.implementation_ref,
                len(request.scenarios),
                str(value.created_by),
                str(value.command_id),
                value.request_id,
                value.created_at,
                value.fingerprint,
            ) != (
                row["id"],
                row["source_id"],
                row["name"],
                row["source_version"],
                row["source_fingerprint"],
                row["implementation_ref"],
                row["scenarios"],
                row["created_by"],
                row["command_id"],
                row["request_id"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("material metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_material() from None

    def page(
        self, query: PolicyMaterialListQuery, source_id: UUID
    ) -> tuple[PolicyMaterialSummary, ...]:
        row = PolicyReviewMaterialRow
        clauses = [row.source_id == str(source_id)]
        if query.before is not None:
            clauses.append(
                or_(
                    row.created_at < query.before.created_at,
                    and_(row.created_at == query.before.created_at, row.id < str(query.before.id)),
                )
            )
        values = self.session.execute(
            select(
                row.id,
                row.source_id,
                row.name,
                row.source_version,
                row.source_fingerprint,
                row.implementation_ref,
                row.scenarios,
                row.created_at,
                row.fingerprint,
            )
            .where(*clauses)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(query.limit + 1)
        )
        try:
            return tuple(
                PolicyMaterialSummary(
                    id=value.id,
                    source=PolicySourceReference(name=value.name, version=value.source_version),
                    source_id=value.source_id,
                    source_fingerprint=value.source_fingerprint,
                    implementation_ref=value.implementation_ref,
                    scenarios=value.scenarios,
                    created_at=utc(value.created_at),
                    fingerprint=value.fingerprint,
                )
                for value in values
            )
        except (ValueError, TypeError, KeyError):
            raise inconsistent_material() from None
