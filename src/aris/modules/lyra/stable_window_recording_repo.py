"""Reconstruct immutable stable receipts independently of active recording handlers."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.modules.lyra.derivation_repo import UsageDerivationRepository
from aris.modules.lyra.derivation_schemas import UsageDerivationRequest
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import StoredUsageOperation, UsagePostingRequest
from aris.modules.lyra.models import (
    StableRecordingLinkRow,
    StableWindowAcceptanceRow,
    UsageLedgerOperationRow,
)
from aris.modules.lyra.recording_order_repo import RecordingReceiptOrderRepository
from aris.modules.lyra.stable_window_acceptance_repo import StableWindowAcceptanceRepository
from aris.modules.lyra.stable_window_acceptance_schemas import StoredAcceptedStableWindow
from aris.modules.lyra.stable_window_recording_schemas import (
    RecordAcceptedStableWindow,
    StableWindowRecordingEvidence,
    StableWindowRecordingReceipt,
)


class StableWindowRecordingRepository:
    @staticmethod
    def link(session: Session, receipt: StableWindowRecordingReceipt) -> None:
        session.add(
            StableRecordingLinkRow(
                operation_id=str(receipt.id),
                window_key=receipt.request.key,
                entry_id=str(receipt.ledger.entries[0]),
            )
        )
        session.flush()
        RecordingReceiptOrderRepository(session).append(
            "stable",
            receipt.id,
            receipt.ledger.head.node_id,
        )

    def evidence_for_operation(
        self, session: Session, operation_id: UUID
    ) -> StableWindowRecordingEvidence | None:
        row = (
            session.execute(
                select(*StableRecordingLinkRow.__table__.c).where(
                    StableRecordingLinkRow.operation_id == str(operation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        accepted = StableWindowAcceptanceRepository(session).get(row["window_key"])
        if accepted is None:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stable link first acceptance is unavailable", 503
            )
        request = RecordAcceptedStableWindow(
            key=accepted.proof.window.key, expected_acceptance_fingerprint=accepted.fingerprint
        )
        receipt = self.get(session, request)
        if (
            receipt is None
            or request.operation_id != operation_id
            or str(receipt.ledger.entries[0]) != row["entry_id"]
        ):
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Stable recording link differs", 503)
        return StableWindowRecordingEvidence(
            acceptance=accepted, recording=receipt.model_copy(update={"duplicate": False})
        )

    def for_entry(
        self,
        session: Session,
        *,
        node_id: str,
        run_id: str,
        entry_id: UUID,
        trace_fingerprint: str,
        recording_fingerprint: str,
        read_at: datetime,
        through_recording_revision: int | None = None,
    ) -> StableWindowRecordingEvidence | None:
        query = (
            select(StableRecordingLinkRow.operation_id)
            .join(
                StableWindowAcceptanceRow,
                StableWindowAcceptanceRow.key == StableRecordingLinkRow.window_key,
            )
            .join(
                UsageLedgerOperationRow,
                UsageLedgerOperationRow.id == StableRecordingLinkRow.operation_id,
            )
            .where(
                StableRecordingLinkRow.entry_id == str(entry_id),
                StableWindowAcceptanceRow.node_id == node_id,
                StableWindowAcceptanceRow.run_id == run_id,
                StableWindowAcceptanceRow.configuration_fingerprint == recording_fingerprint,
                StableWindowAcceptanceRow.canonical_record.contains(
                    '"expected_trace_configuration_fingerprint":"' + trace_fingerprint + '"',
                    autoescape=True,
                ),
                UsageLedgerOperationRow.created_at <= read_at,
            )
            .order_by(UsageLedgerOperationRow.created_at, StableRecordingLinkRow.operation_id)
            .limit(1)
        )
        if through_recording_revision is not None:
            query = query.where(
                RecordingReceiptOrderRepository.membership(
                    "stable",
                    StableRecordingLinkRow.operation_id,
                    node_id,
                    through_recording_revision,
                )
            )
        operation_id = session.scalar(query)
        return self.evidence_for_operation(session, UUID(operation_id)) if operation_id else None

    @staticmethod
    def derivation_request(
        accepted: StoredAcceptedStableWindow, request: RecordAcceptedStableWindow
    ) -> UsageDerivationRequest:
        original = accepted.proof.original
        return UsageDerivationRequest(
            id=request.derivation_id,
            sources=original.sources,
            as_of=accepted.request.window.reference.source_cutoff,
            identity_revision=accepted.request.identity_revision,
            continuity=original.continuity,
            formation=original.formation,
        )

    def posting(
        self,
        session: Session,
        accepted: StoredAcceptedStableWindow,
        request: RecordAcceptedStableWindow,
    ) -> UsagePostingRequest:
        stored = UsageDerivationRepository(session).get(request.derivation_id)
        if stored is None:
            raise DomainError("NOT_FOUND", "Stable derivation not found", 404)
        artifact = stored.artifact
        attributed = artifact.analysis.request.attribution
        matches = [
            index
            for index, candidate in enumerate(attributed.residency.sessions)
            if candidate == accepted.proof.original.candidate
        ]
        if artifact.request != self.derivation_request(accepted, request) or len(matches) != 1:
            raise DomainError("CONFLICT", "Stable derivation differs from first evidence", 409)
        index = matches[0]
        identity = attributed.attributions[index].identity
        if identity.status != "resolved" or identity.user_id != accepted.parent_identity.user_id:
            raise DomainError("USAGE_NEEDS_REVIEW", "Stable owner differs from entire parent", 409)
        return UsagePostingRequest(
            id=request.operation_id,
            derivation_id=request.derivation_id,
            session_index=index,
            expected_result_fingerprint=stored.receipt.result_fingerprint,
            expected_configuration_fingerprint=accepted.configuration.fingerprint,
        )

    def receipt(
        self,
        session: Session,
        accepted: StoredAcceptedStableWindow,
        request: RecordAcceptedStableWindow,
        operation: StoredUsageOperation,
        *,
        duplicate: bool,
    ) -> StableWindowRecordingReceipt:
        expected = self.posting(session, accepted, request)
        if operation.request != expected or operation.receipt.head.node_id != (
            accepted.proof.original.parent_run.first.node_id
        ):
            raise DomainError("CONFLICT", "Stable ledger operation has different evidence", 409)
        return StableWindowRecordingReceipt(
            request=request,
            acceptance=accepted.receipt(duplicate=False),
            derivation_result_fingerprint=expected.expected_result_fingerprint,
            session_index=expected.session_index,
            ledger=operation.receipt.model_copy(update={"duplicate": False}),
            duplicate=duplicate,
        )

    def get(
        self, session: Session, request: RecordAcceptedStableWindow
    ) -> StableWindowRecordingReceipt | None:
        accepted = StableWindowAcceptanceRepository(session).get(request.key)
        if accepted is None:
            raise DomainError("NOT_FOUND", "Accepted stable window not found", 404)
        if accepted.fingerprint != request.expected_acceptance_fingerprint:
            raise DomainError("CONFLICT", "First accepted stable window differs", 409)
        operation = UsageLedgerRepository(session).operation(request.operation_id)
        return (
            self.receipt(session, accepted, request, operation, duplicate=True)
            if operation
            else None
        )
