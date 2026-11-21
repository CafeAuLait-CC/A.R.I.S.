"""Internal measured-window views; never a new ledger or caller-supplied posting authority."""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal
from uuid import UUID

from aris.modules.lyra.ledger_head_proof import VerifiedLedgerHeads
from aris.modules.lyra.ledger_plan import MeasuredWindow
from aris.modules.lyra.ledger_schemas import ReversedMeasurement, UsageLedgerEntry, UsageLedgerHead
from aris.modules.lyra.ledger_segment_schemas import LedgerEntryIndex
from aris.modules.lyra.ledger_window_read import LedgerWindowQuery, LedgerWindowRead
from aris.modules.lyra.recorded_usage import _active_prefix


@dataclass(frozen=True)
class CoverageLedgerEntry:
    id: UUID
    fingerprint: str
    kind: Literal["measurement", "reversal"]
    measurement: MeasuredWindow


def coverage_entry(entry: UsageLedgerEntry) -> CoverageLedgerEntry:
    return CoverageLedgerEntry(
        entry.id,
        entry.fingerprint,
        "reversal" if isinstance(entry.details, ReversedMeasurement) else "measurement",
        entry.details.measurement,
    )


def indexed_coverage_entry(entry: LedgerEntryIndex, node_id: str) -> CoverageLedgerEntry:
    return CoverageLedgerEntry(entry.entry_id, entry.fingerprint, entry.kind, entry.window(node_id))


@dataclass(frozen=True)
class CoverageLedger:
    head: UsageLedgerHead
    read_at: datetime
    active: tuple[CoverageLedgerEntry, ...]
    reversals: tuple[CoverageLedgerEntry, ...]
    committed_heads: tuple[UsageLedgerHead, ...]
    indexed: bool = False
    selection: LedgerWindowQuery | None = None

    def contains(self, head: UsageLedgerHead) -> bool:
        if head.node_id != self.head.node_id:
            return False
        if head.revision == 0:
            return head.fingerprint is None
        return head in self.committed_heads

    def with_heads(self, proof: VerifiedLedgerHeads) -> "CoverageLedger":
        if not self.indexed or proof.through != self.head or proof.read_at != self.read_at:
            raise ValueError("coverage dependency proof differs from its ledger view")
        return replace(self, committed_heads=proof.heads)


def original_coverage_ledger(
    head: UsageLedgerHead, entries: tuple[UsageLedgerEntry, ...], read_at: datetime
) -> CoverageLedger:
    active = _active_prefix(head, entries, read_at)
    return CoverageLedger(
        head,
        read_at,
        tuple(coverage_entry(entry) for entry in active),
        tuple(
            coverage_entry(entry)
            for entry in entries
            if isinstance(entry.details, ReversedMeasurement)
        ),
        tuple(
            UsageLedgerHead(
                node_id=entry.node_id, revision=entry.revision, fingerprint=entry.fingerprint
            )
            for entry in entries
        ),
    )


def indexed_coverage_ledger(view: LedgerWindowRead) -> CoverageLedger:
    return CoverageLedger(
        view.head,
        view.read_at,
        tuple(indexed_coverage_entry(entry, view.head.node_id) for entry in view.active),
        tuple(indexed_coverage_entry(entry, view.head.node_id) for entry in view.reversals),
        (),
        indexed=True,
        selection=view.query,
    )
