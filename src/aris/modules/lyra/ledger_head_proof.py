"""Service-verified receipt heads within a fixed ledger prefix, independent of time filtering."""

from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Literal

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.ledger_repo import inconsistent
from aris.modules.lyra.ledger_schemas import UsageLedgerHead, canonical_ledger, ledger_fingerprint
from aris.modules.lyra.ledger_segment_repo import LedgerSegmentRepository
from aris.modules.lyra.ledger_segment_schemas import LedgerSegmentReference, ledger_entry_index
from aris.modules.lyra.ledger_window_read import LedgerWindowRead
from aris.modules.lyra.ledger_window_service import LedgerWindowReader
from aris.modules.lyra.usage_intervals import IntervalContract

MAX_RECEIPT_HEADS = 128
MAX_HEAD_INDEX_BYTES = 8 * 1024 * 1024


class _HeadContent(IntervalContract):
    through: UsageLedgerHead
    heads: tuple[UsageLedgerHead, ...] = Field(max_length=MAX_RECEIPT_HEADS)


@dataclass(frozen=True)
class VerifiedLedgerHeads:
    """Internal service output; cannot be submitted as a public authority or coverage claim."""

    through: UsageLedgerHead
    heads: tuple[UsageLedgerHead, ...]
    index_segments: tuple[LedgerSegmentReference, ...]
    original_tail_heads: tuple[UsageLedgerHead, ...]
    read_at: datetime
    verified_at: datetime
    fingerprint: str
    verification_scope: Literal["original_heads_in_verified_ledger_snapshot"] = (
        "original_heads_in_verified_ledger_snapshot"
    )
    quota_usage_available: Literal[False] = False

    def contains(self, head: UsageLedgerHead) -> bool:
        return head in self.heads


class LedgerHeadVerifier:
    def __init__(self, reader: LedgerWindowReader) -> None:
        self.reader = reader

    def verify(
        self, view: LedgerWindowRead, heads: tuple[UsageLedgerHead, ...]
    ) -> VerifiedLedgerHeads:
        try:
            with self.reader.sessions() as db:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                elif db.get_bind().dialect.name == "sqlite":
                    db.execute(text("BEGIN"))
                return self.verify_in_transaction(db, view, heads)
        except DBAPIError as error:
            raise DomainError(
                database_failure(error), "Ledger receipt dependency database is unavailable", 503
            ) from None

    def verify_in_transaction(
        self,
        db: Session,
        view: LedgerWindowRead,
        heads: tuple[UsageLedgerHead, ...],
        *,
        deadline: float | None = None,
    ) -> VerifiedLedgerHeads:
        if len(heads) > MAX_RECEIPT_HEADS:
            raise DomainError("USAGE_REPORT_TOO_LARGE", "Split ledger receipt dependencies", 503)
        heads = tuple(
            UsageLedgerHead.model_validate(head.model_dump(mode="json")) for head in heads
        )
        now = self.reader._now()
        if (
            view.head.node_id != self.reader.configuration.node_id
            or view.query.node_id != view.head.node_id
            or view.query.snapshot != view.head
            or view.read_at > now
            or view.verified_at > now
        ):
            raise DomainError("CONFLICT", "Ledger dependency view differs from its reader", 409)
        by_revision: dict[int, UsageLedgerHead] = {}
        for head in heads:
            if head.node_id != view.head.node_id or not 1 <= head.revision <= view.head.revision:
                raise DomainError("CONFLICT", "Receipt head exceeds its captured ledger", 409)
            previous = by_revision.get(head.revision)
            if previous is not None and previous != head:
                raise DomainError("CONFLICT", "Receipt heads disagree at one revision", 409)
            by_revision[head.revision] = head
        ordered = tuple(by_revision[revision] for revision in sorted(by_revision))
        deadline = min(
            deadline if deadline is not None else float("inf"),
            monotonic() + self.reader.timeout_ms / 1000,
        )
        self.reader._within(deadline)
        remaining = max(1, int((deadline - monotonic()) * 1000))
        transaction_budget(db, min(100, remaining), min(1000, remaining), remaining)
        segments = LedgerSegmentRepository(db).covering_many(
            view.head.node_id, tuple(by_revision), max_bytes=MAX_HEAD_INDEX_BYTES
        )
        points = {}
        for segment in segments:
            if segment.configuration != self.reader.configuration:
                raise inconsistent()
            for entry in segment.entries:
                if entry.revision in by_revision:
                    if entry.revision in points:
                        raise inconsistent()
                    points[entry.revision] = entry
            self.reader._within(deadline)
        missing = set(by_revision) - set(points)
        before = view.anchor.head.revision if view.anchor else 0
        if any(revision <= before for revision in missing):
            raise inconsistent()
        tail_heads = []
        if missing:
            original = self.reader.segments.verify_tail_in_transaction(
                db,
                view.head,
                view.anchor,
                self.reader.configuration,
                at=view.read_at,
                deadline=deadline,
            )
            for value in original:
                if value.revision in missing:
                    points[value.revision] = ledger_entry_index(value)
                    tail_heads.append(
                        UsageLedgerHead(
                            node_id=value.node_id,
                            revision=value.revision,
                            fingerprint=value.fingerprint,
                        )
                    )
        for head in ordered:
            point = points.get(head.revision)
            if (
                point is None
                or point.fingerprint != head.fingerprint
                or point.recorded_at > view.read_at
            ):
                raise DomainError(
                    "CONFLICT", "Receipt head differs from original ledger content", 409
                )
        verified_at = self.reader._now()
        if verified_at < view.read_at or any(value.created_at > verified_at for value in segments):
            raise inconsistent()
        self.reader._within(deadline)
        return VerifiedLedgerHeads(
            through=view.head,
            heads=ordered,
            index_segments=tuple(value.reference() for value in segments),
            original_tail_heads=tuple(tail_heads),
            read_at=view.read_at,
            verified_at=verified_at,
            fingerprint=ledger_fingerprint(
                "verified_committed_ledger_heads",
                canonical_ledger(_HeadContent(through=view.head, heads=ordered)),
            ),
        )
