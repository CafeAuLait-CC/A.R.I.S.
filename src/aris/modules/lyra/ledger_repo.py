"""Lyra-owned SQL; mutable claims cannot invent a known-empty historical ledger."""

import hashlib
import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, select, text, update
from sqlalchemy.orm import Session, aliased

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.ledger_plan import ActiveUsageMeasurement, LedgerCoverage, MeasuredWindow
from aris.modules.lyra.ledger_schemas import (
    MAX_LEDGER_BYTES,
    PostedMeasurement,
    ReversedMeasurement,
    StoredUsageOperation,
    UsageLedgerEntry,
    UsageLedgerHead,
    UsageLedgerReceipt,
    UsageOperationRequest,
    canonical_ledger,
    ledger_fingerprint,
    measurement_key,
    operation_request_adapter,
)
from aris.modules.lyra.models import (
    UsageLedgerClaim,
    UsageLedgerEntryRow,
    UsageLedgerOperationRow,
    UsageLedgerState,
)


def inconsistent() -> DomainError:
    return DomainError("DEPENDENCY_UNAVAILABLE", "Stored usage ledger is inconsistent", 503)


class UsageLedgerRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, kind: str, key: str) -> None:
        dialect = self.session.get_bind().dialect.name
        if dialect == "postgresql":
            number = int.from_bytes(
                hashlib.sha256(
                    b"aris.lyra.ledger.lock\x00" + kind.encode() + b"\x00" + key.encode()
                ).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SET LOCAL lock_timeout = '1000ms'"))
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})
        elif dialect != "sqlite":
            raise RuntimeError("unsupported usage ledger database")

    @staticmethod
    def _entry(row: dict[str, Any]) -> UsageLedgerEntry:
        try:
            raw = row["canonical_entry"]
            if len(raw.encode()) > MAX_LEDGER_BYTES:
                raise ValueError("oversized ledger entry")
            entry = UsageLedgerEntry.model_validate(json.loads(raw))
            window = entry.details.measurement
            if (
                canonical_ledger(entry) != raw
                or (
                    str(entry.id),
                    str(entry.event_id),
                    entry.node_id,
                    entry.revision,
                    entry.details.kind,
                    entry.fingerprint,
                    entry.previous_fingerprint,
                )
                != (
                    row["id"],
                    row["event_id"],
                    row["node_id"],
                    row["revision"],
                    row["kind"],
                    row["fingerprint"],
                    row["previous_fingerprint"],
                )
                or (window.gpu_uuid, str(window.owner_id), window.observed_microseconds)
                != (row["gpu_uuid"], row["owner_id"], row["observed_microseconds"])
                or (window.start_at, window.end_at, entry.recorded_at)
                != (utc(row["start_at"]), utc(row["end_at"]), utc(row["recorded_at"]))
                or row["measurement_key"]
                != (
                    measurement_key(window)
                    if isinstance(entry.details, PostedMeasurement)
                    else None
                )
                or row["target_id"]
                != (
                    str(entry.details.target_id)
                    if isinstance(entry.details, ReversedMeasurement)
                    else None
                )
                or row["target_kind"]
                != ("measurement" if isinstance(entry.details, ReversedMeasurement) else None)
            ):
                raise ValueError("ledger metadata differs from its canonical entry")
            return entry
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def _one(self, statement: Any) -> UsageLedgerEntry | None:
        row = self.session.execute(statement).mappings().one_or_none()
        return self._entry(dict(row)) if row is not None else None

    def entry(self, entry_id: UUID) -> UsageLedgerEntry | None:
        return self._one(
            select(*UsageLedgerEntryRow.__table__.c).where(UsageLedgerEntryRow.id == str(entry_id))
        )

    def revision(self, node_id: str, revision: int) -> UsageLedgerEntry | None:
        return self._one(
            select(*UsageLedgerEntryRow.__table__.c).where(
                UsageLedgerEntryRow.node_id == node_id, UsageLedgerEntryRow.revision == revision
            )
        )

    def measured(self, key: str) -> UsageLedgerEntry | None:
        return self._one(
            select(*UsageLedgerEntryRow.__table__.c).where(
                UsageLedgerEntryRow.measurement_key == key
            )
        )

    def head(self, node_id: str) -> UsageLedgerHead:
        state = (
            self.session.execute(
                select(*UsageLedgerState.__table__.c).where(UsageLedgerState.node_id == node_id)
            )
            .mappings()
            .one_or_none()
        )
        last = self._one(
            select(*UsageLedgerEntryRow.__table__.c)
            .where(UsageLedgerEntryRow.node_id == node_id)
            .order_by(UsageLedgerEntryRow.revision.desc())
            .limit(1)
        )
        if state is None and last is None:
            return UsageLedgerHead(node_id=node_id, revision=0)
        if (
            state is None
            or last is None
            or (state["revision"], state["fingerprint"]) != (last.revision, last.fingerprint)
        ):
            raise inconsistent()
        return UsageLedgerHead(
            node_id=node_id, revision=last.revision, fingerprint=last.fingerprint
        )

    def operation(
        self, operation_id: UUID, *, duplicate: bool = True
    ) -> StoredUsageOperation | None:
        row = (
            self.session.execute(
                select(*UsageLedgerOperationRow.__table__.c).where(
                    UsageLedgerOperationRow.id == str(operation_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        try:
            raw_request, raw_receipt = row["canonical_request"], row["canonical_result"]
            if max(len(raw_request.encode()), len(raw_receipt.encode())) > MAX_LEDGER_BYTES:
                raise ValueError("oversized operation")
            request = operation_request_adapter.validate_python(json.loads(raw_request))
            receipt = UsageLedgerReceipt.model_validate(json.loads(raw_receipt))
            fixed = StoredUsageOperation(request=request, receipt=receipt)
            if (
                str(request.id) != row["id"]
                or receipt.duplicate
                or receipt.created_at != utc(row["created_at"])
                or canonical_ledger(request) != raw_request
                or canonical_ledger(receipt) != raw_receipt
                or receipt.request_fingerprint != row["request_fingerprint"]
                or ledger_fingerprint("result", raw_receipt) != row["result_fingerprint"]
            ):
                raise ValueError("ledger operation differs from its fixed receipt")
            head = self.head(receipt.head.node_id)
            prefix = self.revision(receipt.head.node_id, receipt.head.revision)
            if (
                head.revision < receipt.head.revision
                or prefix is None
                or prefix.fingerprint != receipt.head.fingerprint
            ):
                raise ValueError("receipt does not name a committed ledger prefix")
            for entry_id, fingerprint in zip(
                receipt.entries, receipt.entry_fingerprints, strict=True
            ):
                entry = self.entry(entry_id)
                if (
                    entry is None
                    or (entry.fingerprint, entry.node_id) != (fingerprint, head.node_id)
                    or entry.revision > receipt.head.revision
                    or entry.recorded_at > receipt.created_at
                ):
                    raise ValueError("receipt does not bind its original entries")
            return fixed.model_copy(
                update={"receipt": receipt.model_copy(update={"duplicate": duplicate})}
            )
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent() from None

    def coverage(self, window: MeasuredWindow, *, at: datetime) -> LedgerCoverage:
        reversals = aliased(UsageLedgerEntryRow)
        active = (
            select(*UsageLedgerEntryRow.__table__.c)
            .where(
                UsageLedgerEntryRow.kind == "measurement",
                UsageLedgerEntryRow.gpu_uuid == window.gpu_uuid,
                UsageLedgerEntryRow.start_at < window.end_at,
                UsageLedgerEntryRow.end_at > window.start_at,
                ~exists(select(reversals.id).where(reversals.target_id == UsageLedgerEntryRow.id)),
            )
            .order_by(UsageLedgerEntryRow.start_at, UsageLedgerEntryRow.id)
            .limit(1025)
        )
        rows = self.session.execute(active).mappings().all()
        claims = (
            self.session.execute(
                select(*UsageLedgerClaim.__table__.c)
                .where(
                    UsageLedgerClaim.gpu_uuid == window.gpu_uuid,
                    UsageLedgerClaim.start_at < window.end_at,
                    UsageLedgerClaim.end_at > window.start_at,
                )
                .limit(1025)
            )
            .mappings()
            .all()
        )
        complete = len(rows) <= 1024 and len(claims) <= 1024
        entries = [self._entry(dict(row)) for row in rows] if complete else []
        if complete:
            if any(entry.node_id != window.node_id for entry in entries):
                raise DomainError("USAGE_NEEDS_REVIEW", "GPU history spans conflicting nodes", 409)
            by_id = {str(entry.id): entry for entry in entries}
            if {row["entry_id"] for row in claims} != set(by_id):
                raise inconsistent()
            for claim in claims:
                measured = by_id[claim["entry_id"]].details.measurement
                if (
                    claim["entry_kind"],
                    claim["node_id"],
                    claim["gpu_uuid"],
                    utc(claim["start_at"]),
                    utc(claim["end_at"]),
                ) != (
                    "measurement",
                    measured.node_id,
                    measured.gpu_uuid,
                    measured.start_at,
                    measured.end_at,
                ):
                    raise inconsistent()
        return LedgerCoverage(
            node_id=window.node_id,
            gpu_uuids=(window.gpu_uuid,),
            start_at=window.start_at,
            end_at=window.end_at,
            read_at=at,
            state="complete" if complete else "partial",
            records=tuple(
                ActiveUsageMeasurement(
                    id=entry.id,
                    **entry.details.measurement.model_dump(
                        include=set(MeasuredWindow.model_fields)
                    ),
                )
                for entry in entries
            ),
        )

    def append(self, entry: UsageLedgerEntry, previous: UsageLedgerHead) -> None:
        if (
            entry.node_id != previous.node_id
            or entry.revision != previous.revision + 1
            or entry.previous_fingerprint != previous.fingerprint
        ):
            raise DomainError("CONFLICT", "Ledger prefix changed before append", 409)
        if previous.revision:
            prior = self.revision(previous.node_id, previous.revision)
            if prior is None or entry.recorded_at < prior.recorded_at:
                raise DomainError("CONFLICT", "Ledger clock moved before its prefix", 409)
        window = entry.details.measurement
        self.session.add(
            UsageLedgerEntryRow(
                id=str(entry.id),
                event_id=str(entry.event_id),
                node_id=entry.node_id,
                gpu_uuid=window.gpu_uuid,
                owner_id=str(window.owner_id),
                revision=entry.revision,
                kind=entry.details.kind,
                measurement_key=measurement_key(window)
                if isinstance(entry.details, PostedMeasurement)
                else None,
                target_id=str(entry.details.target_id)
                if isinstance(entry.details, ReversedMeasurement)
                else None,
                target_kind="measurement"
                if isinstance(entry.details, ReversedMeasurement)
                else None,
                start_at=window.start_at,
                end_at=window.end_at,
                observed_microseconds=window.observed_microseconds,
                recorded_at=entry.recorded_at,
                previous_fingerprint=entry.previous_fingerprint,
                fingerprint=entry.fingerprint,
                canonical_entry=canonical_ledger(entry),
            )
        )
        if previous.revision == 0:
            self.session.add(
                UsageLedgerState(
                    node_id=entry.node_id, revision=entry.revision, fingerprint=entry.fingerprint
                )
            )
        else:
            changed = self.session.execute(
                update(UsageLedgerState)
                .where(
                    UsageLedgerState.node_id == entry.node_id,
                    UsageLedgerState.revision == previous.revision,
                    UsageLedgerState.fingerprint == previous.fingerprint,
                )
                .values(revision=entry.revision, fingerprint=entry.fingerprint)
                .returning(UsageLedgerState.node_id)
            )
            if changed.scalar_one_or_none() != entry.node_id:
                raise DomainError("CONFLICT", "Ledger prefix changed before append", 409)

    def claim(self, entry: UsageLedgerEntry) -> None:
        if not isinstance(entry.details, PostedMeasurement):
            raise ValueError("only a measured entry can claim its window")
        window = entry.details.measurement
        self.session.add(
            UsageLedgerClaim(
                entry_id=str(entry.id),
                entry_kind="measurement",
                node_id=entry.node_id,
                gpu_uuid=window.gpu_uuid,
                start_at=window.start_at,
                end_at=window.end_at,
            )
        )

    def record_operation(self, request: UsageOperationRequest, receipt: UsageLedgerReceipt) -> None:
        StoredUsageOperation(request=request, receipt=receipt)
        raw_request, raw_receipt = canonical_ledger(request), canonical_ledger(receipt)
        if (
            receipt.duplicate
            or max(len(raw_request.encode()), len(raw_receipt.encode())) > MAX_LEDGER_BYTES
        ):
            raise ValueError("invalid ledger operation storage")
        self.session.add(
            UsageLedgerOperationRow(
                id=str(request.id),
                request_fingerprint=receipt.request_fingerprint,
                result_fingerprint=ledger_fingerprint("result", raw_receipt),
                canonical_request=raw_request,
                canonical_result=raw_receipt,
                created_at=receipt.created_at,
            )
        )
