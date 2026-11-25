"""A bounded lossless format for original Vega acknowledgments, not telemetry."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

MAX_BLOCK_RECEIPTS = 128
ENTRY_BYTES = 49


@dataclass(frozen=True)
class OriginalReceipt:
    seq: int
    message_id: str
    fingerprint: str
    applied_to_current: bool


def encode_receipts(rows: tuple[OriginalReceipt, ...]) -> bytes:
    if not 1 <= len(rows) <= MAX_BLOCK_RECEIPTS or rows[0].seq < 1:
        raise ValueError("Receipt block size or start is invalid")
    if len({row.message_id for row in rows}) != len(rows):
        raise ValueError("Receipt block message IDs must be distinct")
    parts = []
    for index, row in enumerate(rows):
        if (
            row.seq != rows[0].seq + index
            or str(UUID(row.message_id)) != row.message_id
            or len(row.fingerprint) != 64
            or bytes.fromhex(row.fingerprint).hex() != row.fingerprint
            or not isinstance(row.applied_to_current, bool)
        ):
            raise ValueError("Receipt block identities must be canonical and consecutive")
        parts.append(
            UUID(row.message_id).bytes
            + bytes.fromhex(row.fingerprint)
            + bytes([row.applied_to_current])
        )
    return b"".join(parts)


def block_fingerprint(agent_id: str, first: int, last: int, packed: bytes) -> str:
    header = json.dumps([agent_id, first, last], separators=(",", ":")).encode()
    return hashlib.sha256(b"aris.vega.receipt-block.v1\x00" + header + b"\x00" + packed).hexdigest()


def decode_receipts(
    agent_id: str, first: int, last: int, packed: bytes, message_ids: list[str], fingerprint: str
) -> tuple[OriginalReceipt, ...]:
    if (
        first < 1
        or not 1 <= last - first + 1 <= MAX_BLOCK_RECEIPTS
        or len(packed) != (last - first + 1) * ENTRY_BYTES
        or block_fingerprint(agent_id, first, last, packed) != fingerprint
    ):
        raise ValueError("Stored receipt block shape or fingerprint differs")
    rows = []
    for index in range(last - first + 1):
        item = packed[index * ENTRY_BYTES : (index + 1) * ENTRY_BYTES]
        if item[48] not in (0, 1):
            raise ValueError("Stored receipt block acknowledgment flag is invalid")
        rows.append(
            OriginalReceipt(
                first + index, str(UUID(bytes=item[:16])), item[16:48].hex(), bool(item[48])
            )
        )
    if [row.message_id for row in rows] != message_ids or len(set(message_ids)) != len(rows):
        raise ValueError("Stored receipt block message index differs")
    return tuple(rows)
