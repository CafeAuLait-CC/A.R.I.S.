"""Lyra-owned first evidence; no caller can replace an accepted window."""

import json

from sqlalchemy import LargeBinary, cast, func, select
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.lyra.models import StableWindowAcceptanceRow
from aris.modules.lyra.stable_window_acceptance_schemas import (
    MAX_ACCEPTED_WINDOW_BYTES,
    StoredAcceptedStableWindow,
)
from aris.modules.lyra.trace_consumer_repo import UsageTraceRepository
from aris.modules.lyra.trace_consumer_schemas import canonical_trace


class StableWindowAcceptanceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock(self, layout_key: str) -> None:
        UsageTraceRepository(self.session).lock("stable_window_acceptance", layout_key)

    def get(self, key: str) -> StoredAcceptedStableWindow | None:
        column = StableWindowAcceptanceRow.canonical_record
        size = (
            func.octet_length(column)
            if self.session.get_bind().dialect.name == "postgresql"
            else func.length(cast(column, LargeBinary))
        )
        length = self.session.scalar(select(size).where(StableWindowAcceptanceRow.key == key))
        if length is None:
            return None
        if not 0 < length <= MAX_ACCEPTED_WINDOW_BYTES:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Accepted evidence exceeds its budget", 503)
        row = (
            self.session.execute(
                select(*StableWindowAcceptanceRow.__table__.c).where(
                    StableWindowAcceptanceRow.key == key
                )
            )
            .mappings()
            .one()
        )
        try:
            raw = row["canonical_record"]
            value = StoredAcceptedStableWindow.model_validate(json.loads(raw))
            if canonical_trace(value) != raw or (
                value.proof.window.key,
                value.proof.layout_key,
                value.proof.original.parent_run.first.node_id,
                value.proof.original.parent_run.id,
                value.configuration.fingerprint,
                value.request.identity_revision,
                str(value.event_id),
                value.created_at,
                value.fingerprint,
            ) != (
                row["key"],
                row["layout_key"],
                row["node_id"],
                row["run_id"],
                row["configuration_fingerprint"],
                row["identity_revision"],
                row["event_id"],
                utc(row["created_at"]),
                row["fingerprint"],
            ):
                raise ValueError("accepted window metadata differs")
            return value
        except (ValueError, TypeError, KeyError, RecursionError):
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Stored accepted window is inconsistent", 503
            ) from None

    def append(self, value: StoredAcceptedStableWindow) -> None:
        self.session.add(
            StableWindowAcceptanceRow(
                key=value.proof.window.key,
                layout_key=value.proof.layout_key,
                node_id=value.proof.original.parent_run.first.node_id,
                run_id=value.proof.original.parent_run.id,
                configuration_fingerprint=value.configuration.fingerprint,
                identity_revision=value.request.identity_revision,
                event_id=str(value.event_id),
                created_at=value.created_at,
                fingerprint=value.fingerprint,
                canonical_record=canonical_trace(value),
            )
        )
