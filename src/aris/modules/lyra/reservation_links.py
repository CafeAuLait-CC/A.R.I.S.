"""Explicit reservation association; no inferred booking, usage or quota authority."""

from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.contracts import DomainError
from aris.modules.lyra.residency_attribution import ResidencyAttributionAnalysis
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.nova.usage_link_schemas import ReservationUsageLink, ReservationUsageQuery

MAX_EXPLICIT_LINKS = 128


class ReservationLinkReader(Protocol):
    def resolve_in_transaction(
        self, session: Session, query: ReservationUsageQuery
    ) -> ReservationUsageLink: ...


class ExplicitReservationReference(IntervalContract):
    session_index: int = Field(ge=0, lt=MAX_EXPLICIT_LINKS, strict=True)
    reservation_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    expected_history_fingerprint: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ReservationLinkRequest(IntervalContract):
    attribution: ResidencyAttributionAnalysis = Field(repr=False)
    references: tuple[ExplicitReservationReference, ...] = Field(
        default=(), max_length=MAX_EXPLICIT_LINKS
    )

    @model_validator(mode="after")
    def source_targets(self) -> "ReservationLinkRequest":
        if any(
            item.session_index >= len(self.attribution.attributions) for item in self.references
        ):
            raise ValueError("explicit reservation target is outside the source candidate set")
        keys = [(item.session_index, item.reservation_id) for item in self.references]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate reservation references for one source candidate")
        return self


class LinkedResidency(IntervalContract):
    session_index: int = Field(ge=0, lt=MAX_EXPLICIT_LINKS, strict=True)
    state: Literal["linked_candidate", "not_provided", "needs_review", "not_evaluated"]
    reason: Literal[
        "EXPLICIT_RESERVATION_MATCH",
        "NO_REFERENCE",
        "MULTIPLE_REFERENCES",
        "INSUFFICIENT_EVIDENCE",
        "IDENTITY_UNRESOLVED",
        "RESERVATION_NOT_MATCHED",
        "RESERVATION_UNKNOWN",
    ]
    association: ReservationUsageLink | None = Field(default=None, repr=False)


def _local_state(request: ReservationLinkRequest, index: int) -> tuple[str, str] | None:
    references = [item for item in request.references if item.session_index == index]
    if not references:
        return "not_provided", "NO_REFERENCE"
    if len(references) != 1:
        return "needs_review", "MULTIPLE_REFERENCES"
    source = request.attribution.attributions[index]
    if source.state == "insufficient_evidence":
        return "not_evaluated", "INSUFFICIENT_EVIDENCE"
    if source.identity.status != "resolved":
        return "not_evaluated", "IDENTITY_UNRESOLVED"
    return None


def _query(request: ReservationLinkRequest, index: int) -> ReservationUsageQuery:
    reference = next(item for item in request.references if item.session_index == index)
    candidate = request.attribution.residency.sessions[index]
    identity = request.attribution.attributions[index].identity
    assert identity.user_id is not None
    return ReservationUsageQuery(
        reservation_id=reference.reservation_id,
        expected_version=reference.expected_version,
        expected_history_fingerprint=reference.expected_history_fingerprint,
        owner_id=identity.user_id,
        gpu_uuid=candidate.gpu_uuid,
        start_at=candidate.evidence[0].previous.compute_observed_at,
        end_at=candidate.evidence[-1].current.compute_observed_at,
        as_of=request.attribution.as_of,
    )


class ResidencyReservationAnalysis(IntervalContract):
    candidate_only: Literal[True] = True
    formal_usage_available: Literal[False] = False
    request: ReservationLinkRequest = Field(repr=False)
    links: tuple[LinkedResidency, ...] = Field(max_length=MAX_EXPLICIT_LINKS)

    @model_validator(mode="after")
    def honest_associations(self) -> "ResidencyReservationAnalysis":
        if len(self.links) != len(self.request.attribution.attributions):
            raise ValueError("every source candidate requires an explicit association outcome")
        for index, link in enumerate(self.links):
            if link.session_index != index:
                raise ValueError("reservation association order differs from source candidates")
            local = _local_state(self.request, index)
            if local is not None:
                if (link.state, link.reason) != local or link.association is not None:
                    raise ValueError("reservation association requires eligible explicit evidence")
                continue
            association = link.association
            if association is None or association.query != _query(self.request, index):
                raise ValueError("reservation association differs from its original source window")
            expected = (
                ("linked_candidate", "EXPLICIT_RESERVATION_MATCH")
                if association.state == "matched"
                else ("needs_review", "RESERVATION_NOT_MATCHED")
                if association.state == "not_matched"
                else ("needs_review", "RESERVATION_UNKNOWN")
            )
            if (link.state, link.reason) != expected:
                raise ValueError("reservation association cannot claim stronger evidence")
        return self


class ResidencyReservationService:
    def __init__(
        self, sessions: sessionmaker[Session], reservations: ReservationLinkReader
    ) -> None:
        self.sessions, self.reservations = sessions, reservations

    def link(self, request: ReservationLinkRequest) -> ResidencyReservationAnalysis:
        with self.sessions() as session:
            return self.link_in_transaction(session, request)

    def link_in_transaction(
        self, session: Session, request: ReservationLinkRequest
    ) -> ResidencyReservationAnalysis:
        request = ReservationLinkRequest.model_validate(request.model_dump(mode="json"))
        links = []
        for index in range(len(request.attribution.attributions)):
            local = _local_state(request, index)
            if local is not None:
                links.append(LinkedResidency(session_index=index, state=local[0], reason=local[1]))
                continue
            query = _query(request, index)
            association = self.reservations.resolve_in_transaction(session, query)
            association = ReservationUsageLink.model_validate(association.model_dump(mode="json"))
            links.append(
                LinkedResidency(
                    session_index=index,
                    state="linked_candidate" if association.state == "matched" else "needs_review",
                    reason="EXPLICIT_RESERVATION_MATCH"
                    if association.state == "matched"
                    else "RESERVATION_NOT_MATCHED"
                    if association.state == "not_matched"
                    else "RESERVATION_UNKNOWN",
                    association=association,
                )
            )
        result = ResidencyReservationAnalysis(request=request, links=tuple(links))
        # Recheck exact historical sources after the whole batch, including
        # unknown results. A partial answer is never returned after a conflict.
        for link in result.links:
            original = link.association
            if original is None:
                continue
            query = original.query.model_copy(
                update={"expected_history_fingerprint": original.history_fingerprint}
            )
            current = self.reservations.resolve_in_transaction(session, query)
            current = ReservationUsageLink.model_validate(current.model_dump(mode="json"))
            if (
                current.query != query
                or current.model_copy(update={"query": original.query}) != original
            ):
                raise DomainError(
                    "CONFLICT", "Reservation source changed during batch association", 409
                )
        return result
