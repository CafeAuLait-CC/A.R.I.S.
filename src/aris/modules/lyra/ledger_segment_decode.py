"""Reuse only identical, already-validated SQL rows inside one explicit transaction scope."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session, SessionTransaction

from aris.modules.lyra.ledger_segment_schemas import StoredLedgerSegment
from aris.modules.lyra.ledger_window_read import (
    MAX_LEDGER_WINDOW_SEGMENT_BYTES,
    MAX_LEDGER_WINDOW_SEGMENTS,
)

_SCOPE = "aris.lyra.verified_segment_decoding"


@dataclass
class _SegmentDecoding:
    transaction: SessionTransaction
    records: dict[tuple[tuple[str, Any], ...], StoredLedgerSegment] = field(default_factory=dict)
    encoded_bytes: int = 0

    def decode(
        self,
        row: dict[str, Any],
        validate: Callable[[dict[str, Any]], StoredLedgerSegment],
    ) -> StoredLedgerSegment:
        # Include all SQL metadata, not just ID/fingerprint; every call still
        # receives a newly read row, including its complete original JSON.
        key = tuple(sorted(row.items()))
        existing = self.records.get(key)
        if existing is not None:
            return existing
        result = validate(row)
        size = len(row["canonical_segment"].encode())
        if (
            len(self.records) < MAX_LEDGER_WINDOW_SEGMENTS
            and self.encoded_bytes + size <= MAX_LEDGER_WINDOW_SEGMENT_BYTES
        ):
            self.records[key] = result
            self.encoded_bytes += size
        return result


def decode_segment_row(
    session: Session,
    row: dict[str, Any],
    validate: Callable[[dict[str, Any]], StoredLedgerSegment],
) -> StoredLedgerSegment:
    scope = session.info.get(_SCOPE)
    if isinstance(scope, _SegmentDecoding) and scope.transaction is session.get_transaction():
        return scope.decode(row, validate)
    return validate(row)


@contextmanager
def segment_decoding_scope(session: Session) -> Iterator[None]:
    transaction = session.get_transaction()
    if transaction is None:
        raise ValueError("segment decoding requires an existing owned transaction")
    previous = session.info.get(_SCOPE)
    session.info[_SCOPE] = _SegmentDecoding(transaction)
    try:
        yield
    finally:
        if previous is None:
            session.info.pop(_SCOPE, None)
        else:
            session.info[_SCOPE] = previous
