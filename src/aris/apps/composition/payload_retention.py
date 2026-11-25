"""One explicit old-prefix operation; no HTTP route, daemon or blanket age deletion."""

from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.modules.lyra.retention_origin_service import (
    ORIGIN_READ_SECONDS,
    pre_recording_retention_in_transaction,
)
from aris.modules.vega.archive_guard_service import lock_archive_retirement_in_transaction
from aris.modules.vega.payload_retirement_schemas import (
    PayloadRetirementContext,
    PayloadRetirementReceipt,
    PayloadRetirementRequest,
)
from aris.modules.vega.payload_retirement_service import VegaPayloadMaintenance
from aris.modules.vega.usage_archive_service import VegaUsageArchive


def retire_pre_recording_prefix(
    sessions: sessionmaker[Session],
    context: PayloadRetirementContext,
    request: PayloadRetirementRequest,
    *,
    timezone: str,
) -> PayloadRetirementReceipt:
    """Recheck all origin dependencies and apply one exact batch in the same transaction."""
    request = PayloadRetirementRequest.model_validate(request.model_dump(mode="json"))
    maintenance = VegaPayloadMaintenance(sessions)
    try:
        with sessions() as db:
            transaction_budget(db, 1000, 3000, ORIGIN_READ_SECONDS * 1000)
            completed = maintenance.completed_in_transaction(db, context, request)
            if completed is not None:
                return completed
            lock_archive_retirement_in_transaction(db, request.source)
            preview = pre_recording_retention_in_transaction(
                db,
                VegaUsageArchive(sessions),
                request.source,
                before=request.before,
                timezone=timezone,
            )
            prefix = preview.payload_prefix
            if (
                preview.blockers
                or prefix is None
                or preview.fingerprint != request.dependency_fingerprint
                or (prefix.first_seq, prefix.through_seq)
                != (request.reports[0].seq, request.reports[-1].seq)
            ):
                raise DomainError(
                    "CONFLICT", "Archive dependencies or selected prefix changed", 409
                )
            result = maintenance.retire_in_transaction(db, context, request)
            commit_owned_transaction(db)
            return result
    except DBAPIError as error:
        raise DomainError(
            database_failure(error), "Early archive maintenance is unavailable", 503
        ) from None
