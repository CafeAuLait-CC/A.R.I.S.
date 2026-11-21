"""Read-only bounded ledger evidence for local processing and final sampled windows."""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_ledger import CoverageLedger, indexed_coverage_ledger
from aris.modules.lyra.coverage_replay import CoverageReplay
from aris.modules.lyra.coverage_schemas import (
    ProcessingCoverageConfiguration,
    ProcessingCoverageRequest,
)
from aris.modules.lyra.ledger_head_proof import LedgerHeadVerifier
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.lyra.ledger_segment_schemas import ledger_read_configuration
from aris.modules.lyra.ledger_window_read import LedgerWindowQuery, LedgerWindowRead
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from aris.modules.lyra.processing_coverage import processing_ledger_range


class CoverageLedgerReader:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        node_id: str,
        *,
        clock: Callable[[], datetime],
        timeout_ms: int,
    ) -> None:
        self.reader = LedgerWindowReader(
            sessions, ledger_read_configuration(node_id), clock=clock, timeout_ms=timeout_ms
        )

    def local(
        self,
        db: Session,
        request: ProcessingCoverageRequest,
        configuration: ProcessingCoverageConfiguration,
        replay: CoverageReplay,
        read_at: datetime,
        *,
        deadline: float,
    ) -> tuple[CoverageLedger, LedgerWindowRead | None]:
        selected = processing_ledger_range(
            replay, request.start_at, request.end_at, configuration.recording.recording_start_at
        )
        if selected is not None:
            view = self.reader.read_in_transaction(
                db,
                LedgerWindowQuery(
                    node_id=request.ledger.node_id,
                    start_at=selected[0],
                    end_at=selected[1],
                    snapshot=request.ledger,
                ),
                read_at=read_at,
                deadline=deadline,
            )
            return indexed_coverage_ledger(view), view
        # There is no positive source edge to compare. Check the real boundary,
        # without inventing a time query or scanning unrelated lifetime entries.
        ledger = UsageLedgerRepository(db)
        current = ledger.head(request.ledger.node_id)
        if request.ledger.revision > current.revision or read_at > self.reader._now():
            raise DomainError("CONFLICT", "Coverage ledger boundary is not committed", 409)
        if request.ledger.revision:
            point = ledger.revision(request.ledger.node_id, request.ledger.revision)
            if (
                point is None
                or point.fingerprint != request.ledger.fingerprint
                or point.recorded_at > read_at
            ):
                raise DomainError("CONFLICT", "Coverage ledger boundary differs", 409)
        self.reader._within(deadline)
        return CoverageLedger(request.ledger, read_at, (), (), (), indexed=True), None

    def dependencies(
        self,
        db: Session,
        ledger: CoverageLedger,
        view: LedgerWindowRead | None,
        heads: tuple[UsageLedgerHead, ...],
        *,
        deadline: float,
    ) -> CoverageLedger:
        unique = {}
        for head in heads:
            if head.node_id != ledger.head.node_id:
                raise DomainError("CONFLICT", "Coverage dependency node differs", 409)
            if head.revision:
                unique[(head.revision, head.fingerprint)] = head
        if not unique:
            self.reader._within(deadline)
            return ledger
        if view is None:
            raise DomainError("CONFLICT", "Coverage receipts have no comparable source edge", 409)
        proof = LedgerHeadVerifier(self.reader).verify_in_transaction(
            db, view, tuple(unique.values()), deadline=deadline
        )
        return ledger.with_heads(proof)
