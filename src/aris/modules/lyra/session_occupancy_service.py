"""Bounded read-only session evidence; the original residency ledger stays unchanged."""

from collections import defaultdict
from datetime import datetime
from time import monotonic
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from aris.core.access.history_schemas import HistoricalNodeUserQuery
from aris.core.access.history_service import NodeUserHistoryService
from aris.core.contracts import DomainError
from aris.modules.lyra.continuous_usage import ContinuousUsageHistory, UsagePresentationGap
from aris.modules.lyra.derivation_repo import UsageDerivationRepository
from aris.modules.lyra.derivation_schemas import StoredUsageDerivation
from aris.modules.lyra.ledger_repo import UsageLedgerRepository
from aris.modules.lyra.ledger_schemas import PostedMeasurement, UsageLedgerEntry, ledger_fingerprint
from aris.modules.lyra.recorded_usage import MICROSECOND
from aris.modules.lyra.session_occupancy import (
    SessionGapAssessment,
    SessionOccupancySummary,
    assess_gap_sources,
    session_fingerprint,
)
from aris.modules.lyra.usage_history import UsageHistoryRow
from aris.modules.vega.usage_span import UsageSourceSpan, UsageSourceSpanQuery

MAX_SESSION_GAPS = 64


class SessionSourceReader(Protocol):
    def spans_in_transaction(
        self,
        session: Session,
        queries: tuple[UsageSourceSpanQuery, ...],
        *,
        source_character_budget: int = 4 * 1024 * 1024,
    ) -> tuple[UsageSourceSpan | None, ...]: ...


class SessionOccupancyService:
    def __init__(self, sources: SessionSourceReader, identities: NodeUserHistoryService) -> None:
        self.sources, self.identities = sources, identities

    @staticmethod
    def _endpoint(
        row: UsageHistoryRow,
        original: UsageLedgerEntry | None,
        stored: StoredUsageDerivation | None,
        *,
        at: datetime,
        last: bool,
        node_id: str,
    ) -> tuple[str, int, str]:
        if (
            original is None
            or stored is None
            or original.fingerprint != row.entry_fingerprint
            or not isinstance(original.details, PostedMeasurement)
        ):
            raise ValueError("session boundary differs from its original ledger entry")
        m = original.details.measurement
        if (
            m.source_derivation_id != stored.receipt.id
            or m.source_result_fingerprint != stored.receipt.result_fingerprint
            or (m.node_id, m.owner_id, m.gpu_uuid, m.model)
            != (node_id, row.owner_id, row.gpu_uuid, row.model)
            or not m.start_at <= row.start_at < row.end_at <= m.end_at
        ):
            raise ValueError("session boundary differs from its derivation or measurement")
        sources = [s for s in stored.artifact.sources if s.compute_observed_at == at]
        if not sources:
            raise ValueError("session boundary lacks its exact original source")
        source = sources[-1] if last else sources[0]
        return source.query.agent_id, source.query.expected_seq, source.fingerprint

    @staticmethod
    def _unknown(gap: UsagePresentationGap, reason: str) -> SessionGapAssessment:
        return SessionGapAssessment(
            interval_fingerprint=gap.interval_fingerprint,
            before_entry_id=str(gap.before.entry_id),
            after_entry_id=str(gap.after.entry_id),
            duration_microseconds=(gap.after.start_at - gap.before.end_at) // MICROSECOND,
            state="unknown",
            reason=reason,
        )

    def read_in_transaction(
        self,
        session: Session,
        view: ContinuousUsageHistory,
        *,
        deadline: float,
        identity_revision: int | None = None,
        freeze_identity: bool = False,
    ) -> SessionOccupancySummary:
        """Internal: callers establish/recheck current permissions; no new authority is granted."""
        history, gaps = view.history, view.gaps
        revision = self.identities.revision_in_transaction(session)
        if (
            identity_revision is not None and (revision is None or identity_revision > revision)
        ) or (freeze_identity and identity_revision is None and revision is not None):
            raise DomainError(
                "VALIDATION_ERROR", "Frozen identity history revision is invalid", 422
            )
        bound = identity_revision if identity_revision is not None else revision
        results = [self._unknown(g, "SOURCE_EVIDENCE_UNAVAILABLE") for g in gaps]
        # Reserve time for the caller's final current-identity and permission recheck.
        evidence_deadline = deadline - 0.25
        if len(gaps) <= MAX_SESSION_GAPS and monotonic() < evidence_deadline:
            self._verify(session, view, results, bound, evidence_deadline)
        elif gaps:
            results = [self._unknown(g, "SESSION_EVIDENCE_BUDGET_EXCEEDED") for g in gaps]
        original = history.recorded_microseconds
        retained = sum(g.duration_microseconds for g in results if g.state == "retained")
        unknown = sum(g.duration_microseconds for g in results if g.state == "unknown")
        values: dict[str, Any] = dict(
            as_of=history.read_at,
            identity_revision=bound,
            history_fingerprint=history.fingerprint,
            original_recorded_microseconds=original,
            retained_microseconds=retained,
            excluded_microseconds=sum(
                g.duration_microseconds for g in results if g.state == "excluded"
            ),
            unverified_microseconds=unknown,
            verified_session_microseconds=original + retained,
            session_microseconds=None if unknown else original + retained,
            gaps=tuple(results),
        )
        unchecked = SessionOccupancySummary.model_construct(**values, fingerprint="0" * 64)
        return SessionOccupancySummary(**values, fingerprint=session_fingerprint(unchecked))

    def _verify(
        self,
        session: Session,
        view: ContinuousUsageHistory,
        results: list[SessionGapAssessment],
        revision: int | None,
        deadline: float,
    ) -> None:
        gaps, history = view.gaps, view.history
        try:
            records = UsageDerivationRepository(session).get_many(
                tuple(
                    sorted(
                        {r.source_derivation_id for g in gaps for r in (g.before, g.after)}, key=str
                    )
                )
            )
            entries = UsageLedgerRepository(session).entries(
                tuple(sorted({r.entry_id for g in gaps for r in (g.before, g.after)}, key=str))
            )
        except DomainError:
            return
        requests: dict[int, tuple[UsageSourceSpanQuery, str, str]] = {}
        groups: dict[str, set[UsageSourceSpanQuery]] = defaultdict(set)
        for index, gap in enumerate(gaps):
            try:
                agent, first, before = self._endpoint(
                    gap.before,
                    entries.get(gap.before.entry_id),
                    records.get(gap.before.source_derivation_id),
                    at=gap.before.end_at,
                    last=True,
                    node_id=history.node_id,
                )
                other, last, after = self._endpoint(
                    gap.after,
                    entries.get(gap.after.entry_id),
                    records.get(gap.after.source_derivation_id),
                    at=gap.after.start_at,
                    last=False,
                    node_id=history.node_id,
                )
                if agent != other:
                    continue
                q = UsageSourceSpanQuery(
                    agent_id=agent,
                    node_id=history.node_id,
                    first_seq=first,
                    last_seq=last,
                    as_of=history.read_at,
                )
                requests[index] = (q, before, after)
                groups[agent].add(q)
            except (ValueError, TypeError, KeyError):
                continue
        spans: dict[UsageSourceSpanQuery, UsageSourceSpan | None] = {}
        remaining = 4 * 1024 * 1024
        for _, queries in sorted(groups.items()):
            if remaining <= 0 or monotonic() >= deadline:
                break
            ordered = tuple(sorted(queries, key=lambda q: (q.first_seq, q.last_seq)))
            try:
                values = self.sources.spans_in_transaction(
                    session, ordered, source_character_budget=remaining
                )
                if len(values) != len(ordered):
                    raise ValueError("source batch length differs")
                spans.update(zip(ordered, values, strict=True))
                remaining -= sum(s.source_characters for s in values if s is not None)
            except (DomainError, ValueError, TypeError):
                continue
        retained: dict[tuple[int, str, UUID], list[int]] = defaultdict(list)
        for index, (q, before, after) in requests.items():
            span = spans.get(q)
            if span is None or (
                span.references[0].fingerprint,
                span.references[-1].fingerprint,
            ) != (before, after):
                continue
            assessment = assess_gap_sources(gaps[index], span)
            if assessment.state == "retained":
                if assessment.uid is not None and assessment.username is not None:
                    retained[
                        (assessment.uid, assessment.username, gaps[index].before.owner_id)
                    ].append(index)
            else:
                results[index] = SessionGapAssessment.model_validate(
                    {
                        **results[index].model_dump(),
                        "state": assessment.state,
                        "reason": assessment.reason,
                        "source_fingerprint": span.fingerprint,
                    }
                )
        for (_, username, owner_id), indices in retained.items():
            if revision is None or monotonic() >= deadline:
                continue
            try:
                identity = self.identities.resolve_in_transaction(
                    session,
                    HistoricalNodeUserQuery(
                        node_id=history.node_id,
                        os_username=username,
                        start_at=min(gaps[i].before.end_at for i in indices),
                        end_at=max(gaps[i].after.start_at for i in indices),
                        as_of=history.read_at,
                        through_revision=revision,
                    ),
                )
                verified = identity.status == "resolved" and identity.user_id == owner_id
                proof = (
                    ledger_fingerprint("session_identity", identity.model_dump_json())
                    if verified
                    else None
                )
                for index in indices:
                    span = spans[requests[index][0]]
                    assert span is not None
                    results[index] = SessionGapAssessment.model_validate(
                        {
                            **results[index].model_dump(),
                            "state": "retained" if verified else "unknown",
                            "reason": "VERIFIED_SHORT_IDLE_GAP"
                            if verified
                            else "IDENTITY_HISTORY_UNVERIFIED",
                            "source_fingerprint": span.fingerprint,
                            "identity_fingerprint": proof,
                        }
                    )
            except DomainError:
                continue
