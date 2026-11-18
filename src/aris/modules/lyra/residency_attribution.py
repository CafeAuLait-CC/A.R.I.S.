"""Historical platform attribution of sampled candidates, without ledger writes."""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, Protocol

from pydantic import AwareDatetime, Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.history_schemas import (
    MAX_HISTORICAL_MAPPING_QUERIES,
    HistoricalNodeUserBatch,
    HistoricalNodeUserBatchResult,
    HistoricalNodeUserQuery,
    HistoricalNodeUserResolution,
)
from aris.core.contracts import DomainError
from aris.modules.lyra.residency_sessions import (
    ResidencyFormationRule,
    ResidencySessionAnalysis,
    build_residency_sessions,
)
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.lyra.usage_trace import MAX_TRACE_MESSAGES
from aris.modules.vega.usage_schemas import UsageObservationFact


class HistoricalIdentityReader(Protocol):
    def resolve_many_in_transaction(
        self, session: Session, request: HistoricalNodeUserBatch
    ) -> HistoricalNodeUserBatchResult: ...


class _KnowledgeCutoff(IntervalContract):
    as_of: AwareDatetime
    through_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)


class AttributedResidency(IntervalContract):
    session_index: int = Field(ge=0, lt=MAX_HISTORICAL_MAPPING_QUERIES, strict=True)
    identity: HistoricalNodeUserResolution = Field(repr=False)
    state: Literal["attributed_candidate", "insufficient_evidence", "identity_unresolved"]


class ResidencyAttributionAnalysis(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    as_of: AwareDatetime
    directory_revision: int | None = Field(default=None, ge=1, le=9223372036854775807, strict=True)
    residency: ResidencySessionAnalysis = Field(repr=False)
    attributions: tuple[AttributedResidency, ...] = Field(max_length=MAX_HISTORICAL_MAPPING_QUERIES)

    @model_validator(mode="after")
    def complete_attribution(self) -> "ResidencyAttributionAnalysis":
        if len(self.residency.sessions) != len(self.attributions):
            raise ValueError("every residency candidate requires its own attribution result")
        for index, item in enumerate(self.attributions):
            if item.session_index != index:
                raise ValueError("attribution order differs from source candidates")
            candidate = self.residency.sessions[index]
            query = item.identity.query
            start = candidate.evidence[0].previous.compute_observed_at
            end = candidate.evidence[-1].current.compute_observed_at
            if (
                query.node_id != self.residency.trace.node_id
                or query.os_username != candidate.username
                or query.start_at != start
                or query.end_at != end
                or query.as_of != self.as_of
                or item.identity.directory_revision != self.directory_revision
            ):
                raise ValueError("identity evidence does not cover its original residency window")
            expected = (
                "insufficient_evidence"
                if candidate.state == "insufficient_evidence"
                else "attributed_candidate"
                if item.identity.status == "resolved"
                else "identity_unresolved"
            )
            if item.state != expected:
                raise ValueError("candidate attribution cannot claim stronger evidence")
        return self


class ResidencyAttributionService:
    def __init__(
        self, sessions: sessionmaker[Session], identities: HistoricalIdentityReader
    ) -> None:
        self.sessions, self.identities = sessions, identities

    def analyze(
        self,
        facts: Sequence[UsageObservationFact],
        *,
        continuity: UsageContinuityRule,
        formation: ResidencyFormationRule,
        as_of: datetime,
        through_revision: int | None = None,
    ) -> ResidencyAttributionAnalysis:
        with self.sessions() as session:
            return self.analyze_in_transaction(
                session,
                facts,
                continuity=continuity,
                formation=formation,
                as_of=as_of,
                through_revision=through_revision,
            )

    def analyze_in_transaction(
        self,
        session: Session,
        facts: Sequence[UsageObservationFact],
        *,
        continuity: UsageContinuityRule,
        formation: ResidencyFormationRule,
        as_of: datetime,
        through_revision: int | None = None,
    ) -> ResidencyAttributionAnalysis:
        cutoff = _KnowledgeCutoff(as_of=as_of, through_revision=through_revision)
        if not 1 <= len(facts) <= MAX_TRACE_MESSAGES:
            raise DomainError(
                "VALIDATION_ERROR", "Attribution requires a bounded source trace", 422
            )
        facts = tuple(UsageObservationFact.model_validate(f.model_dump(mode="json")) for f in facts)
        if any(max(f.received_at, f.observed_at) > cutoff.as_of for f in facts):
            raise DomainError("VALIDATION_ERROR", "Source facts follow the knowledge cutoff", 422)
        residency = build_residency_sessions(facts, continuity=continuity, formation=formation)
        if len(residency.sessions) > MAX_HISTORICAL_MAPPING_QUERIES:
            raise DomainError(
                "VALIDATION_ERROR", "Residency attribution exceeds its candidate budget", 422
            )
        queries = tuple(
            HistoricalNodeUserQuery(
                node_id=residency.trace.node_id,
                os_username=candidate.username,
                start_at=candidate.evidence[0].previous.compute_observed_at,
                end_at=candidate.evidence[-1].current.compute_observed_at,
                as_of=cutoff.as_of,
                through_revision=cutoff.through_revision,
            )
            for candidate in residency.sessions
        )
        if not queries:
            # An empty candidate list is about process evidence, not a claim
            # that the identity directory has been read or is initialized.
            return ResidencyAttributionAnalysis(
                as_of=cutoff.as_of,
                residency=residency,
                attributions=(),
            )
        request = HistoricalNodeUserBatch(queries=queries)
        snapshot = self.identities.resolve_many_in_transaction(session, request)
        snapshot = HistoricalNodeUserBatchResult.model_validate(snapshot.model_dump(mode="json"))
        return ResidencyAttributionAnalysis(
            as_of=cutoff.as_of,
            directory_revision=snapshot.directory_revision,
            residency=residency,
            attributions=tuple(
                AttributedResidency(
                    session_index=index,
                    identity=identity,
                    state=(
                        "insufficient_evidence"
                        if candidate.state == "insufficient_evidence"
                        else "attributed_candidate"
                        if identity.status == "resolved"
                        else "identity_unresolved"
                    ),
                )
                for index, (candidate, identity) in enumerate(
                    zip(residency.sessions, snapshot.answers, strict=True)
                )
            ),
        )
