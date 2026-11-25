"""One explicit old-prefix operation; no HTTP route, daemon or blanket age deletion."""

from sqlalchemy import text
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

MAX_RETIREMENT_GROUP_BATCHES = 8


def retire_pre_recording_group(
    sessions: sessionmaker[Session],
    context: PayloadRetirementContext,
    requests: tuple[PayloadRetirementRequest, ...],
    *,
    timezone: str,
) -> tuple[PayloadRetirementReceipt, ...]:
    """One dependency capture protects at most 1024 packets under the same source lock.

    Each constituent batch still verifies its exact oldest raw prefix, original
    packet fingerprints and current fact anchors. No dependency reference can
    be captured concurrently while the group owns the exclusive source lock.
    """
    if not 1 <= len(requests) <= MAX_RETIREMENT_GROUP_BATCHES:
        raise ValueError("Retirement groups require one to eight bounded batches")
    requests = tuple(
        PayloadRetirementRequest.model_validate(value.model_dump(mode="json")) for value in requests
    )
    first = requests[0]
    if len({value.id for value in requests}) != len(requests) or any(
        value.source != first.source
        or value.before != first.before
        or value.dependency_fingerprint != first.dependency_fingerprint
        or (index and value.reports[0].seq != requests[index - 1].reports[-1].seq + 1)
        for index, value in enumerate(requests)
    ):
        raise ValueError("Retirement group batches must share one consecutive assessed source")
    maintenance = VegaPayloadMaintenance(sessions)
    try:
        with sessions() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
            transaction_budget(db, 1000, 3000, ORIGIN_READ_SECONDS * 1000)
            lock_archive_retirement_in_transaction(db, first.source)
            completed = tuple(
                maintenance.completed_in_transaction(db, context, value) for value in requests
            )
            if all(value is not None for value in completed):
                return tuple(value for value in completed if value is not None)
            if any(value is not None for value in completed):
                raise DomainError("CONFLICT", "Retirement group is only partially completed", 409)
            preview = pre_recording_retention_in_transaction(
                db, VegaUsageArchive(sessions), first.source, before=first.before, timezone=timezone
            )
            bounds = [
                value
                for value in (
                    preview.recording_exclusion_through_seq,
                    preview.reporting_history_through_seq,
                )
                if value is not None
            ]
            bounds.extend(
                value - 1
                for value in (
                    preview.earliest_derived_source_seq,
                    preview.earliest_build_source_seq,
                )
                if value is not None
            )
            prefix = preview.payload_prefix
            if (
                preview.blockers
                or prefix is None
                or not bounds
                or preview.fingerprint != first.dependency_fingerprint
                or prefix.first_seq != first.reports[0].seq
                or requests[-1].reports[-1].seq > min(bounds)
            ):
                raise DomainError("CONFLICT", "Archive group dependencies or prefix changed", 409)
            result = tuple(
                maintenance.retire_in_transaction(db, context, value) for value in requests
            )
            commit_owned_transaction(db)
            return result
    except DBAPIError as error:
        raise DomainError(
            database_failure(error), "Early archive group maintenance is unavailable", 503
        ) from None


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
            # Dependencies committed by a capture that held the shared source
            # lock must be visible after our exclusive lock finishes waiting.
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
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
