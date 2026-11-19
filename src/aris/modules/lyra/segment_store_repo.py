"""Lyra-owned immutable segment evidence and bounded source-index queries."""

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import (
    UsageResidencySegmentRow,
    UsageResidencySegmentSetRow,
    UsageTraceAttestationRow,
)
from aris.modules.lyra.segment_store_schemas import (
    MAX_SEGMENT_LOOKUP_CHARACTERS,
    MAX_SEGMENT_SET_BYTES,
    ResidencySegmentLookup,
    StoredResidencySegmentItem,
    StoredResidencySegmentSet,
    segment_index_identity,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


def inconsistent_segments() -> DomainError:
    return DomainError(
        "DEPENDENCY_UNAVAILABLE", "Stored residency segment evidence is inconsistent", 503
    )


class ResidencySegmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, kind: str, key: str) -> None:
        if self.session.get_bind().dialect.name == "postgresql":
            value = int.from_bytes(
                hashlib.sha256(
                    b"aris.lyra.segment_store.lock\x00" + kind.encode() + b"\x00" + key.encode()
                ).digest()[:8],
                signed=True,
            )
            self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": value})

    @staticmethod
    def _decode(row: Mapping[str, Any]) -> StoredResidencySegmentSet:
        try:
            raw = row["canonical_record"]
            if len(raw.encode()) > MAX_SEGMENT_SET_BYTES:
                raise ValueError("oversized saved residency segment set")
            value = StoredResidencySegmentSet.model_validate(json.loads(raw))
            if canonical_trace(value) != raw or (
                str(value.request.id),
                str(value.event_id),
                str(value.request.source.attestation_id),
                value.analysis.attestation.consumer_id,
                len(value.analysis.segments),
                value.created_at,
                value.fingerprint,
                value.analysis.attestation.fingerprint,
            ) != (
                row["id"],
                row["event_id"],
                row["attestation_id"],
                row["consumer_id"],
                row["segment_count"],
                utc(row["created_at"]),
                row["fingerprint"],
                row["attestation_fp"],
            ):
                raise ValueError("saved residency segment metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise inconsistent_segments() from None

    def _sets(self, ids: tuple[str, ...]) -> dict[str, StoredResidencySegmentSet]:
        if not ids:
            return {}
        rows = (
            self.session.execute(
                select(
                    *UsageResidencySegmentSetRow.__table__.c,
                    UsageTraceAttestationRow.fingerprint.label("attestation_fp"),
                )
                .join(
                    UsageTraceAttestationRow,
                    UsageTraceAttestationRow.id == UsageResidencySegmentSetRow.attestation_id,
                )
                .where(UsageResidencySegmentSetRow.id.in_(ids))
            )
            .mappings()
            .all()
        )
        if len(rows) != len(ids):
            raise inconsistent_segments()
        return {row["id"]: self._decode(dict(row)) for row in rows}

    def get(self, id: UUID, *, by_attestation: bool = False) -> StoredResidencySegmentSet | None:
        column = (
            UsageResidencySegmentSetRow.attestation_id
            if by_attestation
            else UsageResidencySegmentSetRow.id
        )
        row = (
            self.session.execute(
                select(
                    *UsageResidencySegmentSetRow.__table__.c,
                    UsageTraceAttestationRow.fingerprint.label("attestation_fp"),
                )
                .join(
                    UsageTraceAttestationRow,
                    UsageTraceAttestationRow.id == UsageResidencySegmentSetRow.attestation_id,
                )
                .where(column == str(id))
            )
            .mappings()
            .one_or_none()
        )
        return self._decode(dict(row)) if row is not None else None

    @staticmethod
    def _index_fields(value: StoredResidencySegmentSet, index: int) -> dict[str, Any]:
        segment = value.analysis.segments[index]
        return dict(
            id=segment_index_identity(value.request.id, segment),
            set_id=str(value.request.id),
            segment_index=index,
            consumer_id=value.analysis.attestation.consumer_id,
            run_id=segment.run_at_segment_end.id,
            interval_offset=segment.interval_offset,
            through_interval=segment.run_at_segment_end.intervals,
            first_seq=segment.first.seq,
            last_seq=segment.last.seq,
        )

    def append_set(self, value: StoredResidencySegmentSet) -> None:
        self.session.add(
            UsageResidencySegmentSetRow(
                id=str(value.request.id),
                event_id=str(value.event_id),
                attestation_id=str(value.request.source.attestation_id),
                consumer_id=value.analysis.attestation.consumer_id,
                segment_count=len(value.analysis.segments),
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_record=canonical_trace(value),
            )
        )

    def append_index(self, value: StoredResidencySegmentSet) -> None:
        self.session.add_all(
            UsageResidencySegmentRow(**self._index_fields(value, index))
            for index in range(len(value.analysis.segments))
        )

    def verify_index(self, value: StoredResidencySegmentSet) -> None:
        rows = (
            self.session.execute(
                select(*UsageResidencySegmentRow.__table__.c)
                .where(UsageResidencySegmentRow.set_id == str(value.request.id))
                .order_by(UsageResidencySegmentRow.segment_index)
            )
            .mappings()
            .all()
        )
        if [dict(row) for row in rows] != [
            self._index_fields(value, index) for index in range(len(value.analysis.segments))
        ]:
            raise inconsistent_segments()

    def lookup(self, query: ResidencySegmentLookup) -> tuple[StoredResidencySegmentItem, ...]:
        headers = (
            self.session.execute(
                select(
                    *UsageResidencySegmentRow.__table__.c,
                    UsageResidencySegmentSetRow.fingerprint.label("set_fp"),
                    func.length(UsageResidencySegmentSetRow.canonical_record).label("source_chars"),
                )
                .join(
                    UsageResidencySegmentSetRow,
                    UsageResidencySegmentSetRow.id == UsageResidencySegmentRow.set_id,
                )
                .where(
                    UsageResidencySegmentRow.consumer_id == query.consumer_id,
                    UsageResidencySegmentRow.run_id == query.run_id,
                    UsageResidencySegmentRow.interval_offset < query.through_interval,
                    UsageResidencySegmentRow.through_interval > query.after_interval,
                )
                .order_by(UsageResidencySegmentRow.interval_offset)
                .limit(query.limit)
            )
            .mappings()
            .all()
        )
        prefix: list[dict[str, Any]] = []
        expected = query.after_interval
        lengths = {}
        for header in headers:
            row = dict(header)
            if row["interval_offset"] > expected:
                break
            if (prefix and row["interval_offset"] != expected) or row[
                "through_interval"
            ] <= expected:
                raise inconsistent_segments()
            prefix.append(dict(row))
            lengths[row["set_id"]] = row["source_chars"]
            expected = min(row["through_interval"], query.through_interval)
            if expected == query.through_interval:
                break
        if (
            any(type(size) is not int or size <= 0 for size in lengths.values())
            or sum(lengths.values()) > MAX_SEGMENT_LOOKUP_CHARACTERS
        ):
            raise DomainError(
                "USAGE_EVIDENCE_TOO_LARGE",
                "Residency segment lookup exceeds its source budget",
                503,
            )
        sets = self._sets(tuple(lengths))
        result = []
        for row in prefix:
            value = sets[row["set_id"]]
            index = row["segment_index"]
            if not 0 <= index < len(value.analysis.segments) or row["set_fp"] != value.fingerprint:
                raise inconsistent_segments()
            if any(
                row[key] != expected for key, expected in self._index_fields(value, index).items()
            ):
                raise inconsistent_segments()
            result.append(
                StoredResidencySegmentItem(
                    set_id=value.request.id,
                    set_created_at=value.created_at,
                    set_fingerprint=value.fingerprint,
                    segment_index=index,
                    consumer_id=value.analysis.attestation.consumer_id,
                    segment=value.analysis.segments[index],
                )
            )
        return tuple(result)
