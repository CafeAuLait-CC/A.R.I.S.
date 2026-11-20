"""Nova-only storage, with bounded summaries separate from original contexts."""

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.nova.models import PolicyHistoricalReviewRow
from aris.modules.nova.policy_catalog_schemas import PolicySourceReference
from aris.modules.nova.policy_history_record_schemas import (
    MAX_HISTORICAL_REVIEW_BYTES,
    HistoricalReviewListQuery,
    HistoricalReviewSummary,
    StoredHistoricalPolicyReview,
    canonical_history_record,
)


def inconsistent_history_record() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored historical review is inconsistent", 503)


class PolicyHistoricalReviewRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    @staticmethod
    def _metadata(value: StoredHistoricalPolicyReview) -> dict[str, str | int]:
        request, query = value.request, value.request.query
        return {
            "id": str(value.id),
            "source_id": str(value.source_id),
            "name": query.policy.name,
            "source_version": query.policy.version,
            "source_fingerprint": query.expected_policy_fingerprint,
            "node_id": query.usage.node_id,
            "snapshot_id": str(query.usage.snapshot_id),
            "context_fingerprint": request.expected_context_fingerprint,
            "implementation_ref": request.expected_implementation_ref,
            "created_by": str(value.created_by),
            "command_id": str(value.command_id),
            "request_id": value.request_id,
            "fingerprint": value.fingerprint,
        }

    def insert(self, value: StoredHistoricalPolicyReview) -> bool:
        dialect = self.session.get_bind().dialect.name
        if dialect not in {"postgresql", "sqlite"}:
            raise RuntimeError("unsupported historical review database")
        insert = pg_insert if dialect == "postgresql" else sqlite_insert
        return (
            self.session.scalar(
                insert(PolicyHistoricalReviewRow)
                .values(
                    **self._metadata(value),
                    created_at=value.created_at,
                    canonical_record=canonical_history_record(value),
                )
                .on_conflict_do_nothing(index_elements=["command_id"])
                .returning(PolicyHistoricalReviewRow.id)
            )
            is not None
        )

    def get(self, id: UUID, owner_id: UUID) -> StoredHistoricalPolicyReview | None:
        row = (
            self.session.execute(
                select(*PolicyHistoricalReviewRow.__table__.c).where(
                    PolicyHistoricalReviewRow.id == str(id),
                    PolicyHistoricalReviewRow.created_by == str(owner_id),
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw = row["canonical_record"]
            if len(raw.encode()) > MAX_HISTORICAL_REVIEW_BYTES:
                raise ValueError("historical review exceeds budget")
            value = StoredHistoricalPolicyReview.model_validate_json(raw)
            if (
                raw != canonical_history_record(value)
                or any(row[key] != item for key, item in self._metadata(value).items())
                or utc(row["created_at"]) != value.created_at
            ):
                raise ValueError("historical review metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_history_record() from None

    def page(
        self, query: HistoricalReviewListQuery, source_id: UUID, owner_id: UUID
    ) -> tuple[HistoricalReviewSummary, ...]:
        row = PolicyHistoricalReviewRow
        clauses = [
            row.source_id == str(source_id),
            row.created_by == str(owner_id),
            row.node_id == query.node_id,
        ]
        if query.before is not None:
            clauses.append(
                or_(
                    row.created_at < query.before.created_at,
                    and_(row.created_at == query.before.created_at, row.id < str(query.before.id)),
                )
            )
        rows = self.session.execute(
            select(
                row.id,
                row.name,
                row.source_version,
                row.source_fingerprint,
                row.node_id,
                row.snapshot_id,
                row.context_fingerprint,
                row.implementation_ref,
                row.created_at,
                row.fingerprint,
            )
            .where(*clauses)
            .order_by(row.created_at.desc(), row.id.desc())
            .limit(query.limit + 1)
        )
        try:
            return tuple(
                HistoricalReviewSummary(
                    id=value.id,
                    source=PolicySourceReference(name=value.name, version=value.source_version),
                    source_fingerprint=value.source_fingerprint,
                    node_id=value.node_id,
                    snapshot_id=value.snapshot_id,
                    context_fingerprint=value.context_fingerprint,
                    implementation_ref=value.implementation_ref,
                    created_at=utc(value.created_at),
                    fingerprint=value.fingerprint,
                )
                for value in rows
            )
        except (ValueError, TypeError, KeyError):
            raise inconsistent_history_record() from None
