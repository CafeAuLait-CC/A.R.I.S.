"""Small, complete original source spans for historical consumers; no database writes."""

import hashlib
import json
from datetime import UTC, datetime

from pydantic import AwareDatetime, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.modules.vega.repo import VegaRepository
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_archive_schemas import ArchivedUsageReference
from aris.modules.vega.usage_schemas import UsageFactContract, UsageObservationFact
from aris.modules.vega.usage_service import _original_report, _usage_fact


class UsageSourceSpanQuery(UsageFactContract):
    agent_id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    first_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    last_seq: int = Field(ge=1, le=9223372036854775807, strict=True)
    as_of: AwareDatetime

    @field_validator("as_of")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def bounded_span(self) -> "UsageSourceSpanQuery":
        if not 0 <= self.last_seq - self.first_seq < 128:
            raise ValueError("source evidence requires at most 128 contiguous messages")
        return self


class UsageSourceSpan(UsageFactContract):
    query: UsageSourceSpanQuery
    references: tuple[ArchivedUsageReference, ...] = Field(min_length=1, max_length=128)
    observations: tuple[UsageObservationFact, ...] = Field(max_length=128)
    healthy_heartbeats: bool
    source_characters: int = Field(ge=0, le=2 * 1024 * 1024)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def original_binding(self) -> "UsageSourceSpan":
        q = self.query
        if tuple(r.seq for r in self.references) != tuple(range(q.first_seq, q.last_seq + 1)):
            raise ValueError("source span must include every original sequence")
        if any(
            (r.agent_id, r.node_id) != (q.agent_id, q.node_id)
            or max(r.observed_at, r.received_at) > q.as_of
            for r in self.references
        ):
            raise ValueError("source span differs from its identity or knowledge cutoff")
        telemetry = {r.seq: r for r in self.references if r.kind == "telemetry"}
        if tuple(f.seq for f in self.observations) != tuple(telemetry):
            raise ValueError("source span omits or repeats a telemetry message")
        for fact in self.observations:
            ref = telemetry[fact.seq]
            if (
                fact.agent_id,
                fact.node_id,
                fact.message_id,
                fact.source_fingerprint,
                fact.observed_at,
                fact.received_at,
            ) != (
                ref.agent_id,
                ref.node_id,
                ref.message_id,
                ref.fingerprint,
                ref.observed_at,
                ref.received_at,
            ):
                raise ValueError("source fact differs from its original reference")
        if self.fingerprint != span_fingerprint(self):
            raise ValueError("source span fingerprint differs")
        return self


def span_fingerprint(value: UsageSourceSpan) -> str:
    body = json.dumps(
        value.model_dump(mode="json", exclude={"fingerprint"}),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(b"aris.vega.source.span\x00" + body.encode()).hexdigest()


class VegaUsageSpans:
    def span_in_transaction(self, session: Session, query: UsageSourceSpanQuery) -> UsageSourceSpan:
        result = self.spans_in_transaction(session, (query,))[0]
        if result is None:
            raise DomainError(
                "DEPENDENCY_UNAVAILABLE", "Historical source is incomplete or inconsistent", 503
            )
        return result

    def spans_in_transaction(
        self,
        session: Session,
        queries: tuple[UsageSourceSpanQuery, ...],
        *,
        source_character_budget: int = 4 * 1024 * 1024,
    ) -> tuple[UsageSourceSpan | None, ...]:
        if not 1 <= len(queries) <= 64 or not 1 <= source_character_budget <= 4 * 1024 * 1024:
            raise ValueError("source evidence exceeds its bounded batch")
        queries = tuple(
            UsageSourceSpanQuery.model_validate(q.model_dump(mode="json")) for q in queries
        )
        first = queries[0]
        if any(
            (q.agent_id, q.node_id, q.as_of) != (first.agent_id, first.node_id, first.as_of)
            for q in queries
        ):
            raise ValueError("source batch requires one binding and cutoff")
        with session.no_autoflush:
            repo = VegaRepository(session)
            metadata = repo.usage_spans_metadata(
                first.agent_id, tuple((q.first_seq, q.last_seq) for q in queries)
            )
            if sum(m[2] for m in metadata) > source_character_budget:
                raise DomainError(
                    "USAGE_SOURCE_TOO_LARGE", "Historical source exceeds its budget", 503
                )
            keys = tuple(m[0] for m in metadata)
            rows = tuple(
                row
                for offset in range(0, len(keys), 128)
                for row in repo.usage_messages(first.agent_id, keys[offset : offset + 128])
            )
            expected = {m[0]: m[1] for m in metadata}
            if {r.message_id: r.seq for r in rows} != expected:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Historical source changed during read", 503
                )
            originals = {}
            sizes = {m[1]: m[2] for m in metadata}
            for row in rows:
                try:
                    report = _original_report(row, first.agent_id, first.node_id)
                    ref = ArchivedUsageReference(
                        agent_id=row.agent_id,
                        node_id=row.node_id,
                        message_id=row.message_id,
                        seq=row.seq,
                        kind=row.message_kind,
                        fingerprint=row.fingerprint,
                        observed_at=utc(row.observed_at),
                        received_at=utc(row.received_at),
                    )
                    fact = (
                        _usage_fact(
                            report, fingerprint=row.fingerprint, received_at=utc(row.received_at)
                        )
                        if isinstance(report, ResourceTelemetryBatch)
                        else None
                    )
                    healthy = (
                        isinstance(report, ResourceTelemetryBatch)
                        or report.collector_status == "ok"
                    )
                    originals[row.seq] = (ref, fact, healthy)
                except (ValueError, TypeError, KeyError, RecursionError):
                    continue
            answers: list[UsageSourceSpan | None] = []
            for query in queries:
                selected = [
                    originals.get(seq) for seq in range(query.first_seq, query.last_seq + 1)
                ]
                characters = sum(
                    sizes.get(seq, 0) for seq in range(query.first_seq, query.last_seq + 1)
                )
                if any(item is None for item in selected) or characters > 2 * 1024 * 1024:
                    answers.append(None)
                    continue
                items = [item for item in selected if item is not None]
                value = UsageSourceSpan.model_construct(
                    query=query,
                    references=tuple(item[0] for item in items),
                    observations=tuple(item[1] for item in items if item[1] is not None),
                    healthy_heartbeats=all(item[2] for item in items),
                    source_characters=characters,
                    fingerprint="0" * 64,
                )
                try:
                    answers.append(
                        UsageSourceSpan.model_validate(
                            {
                                **value.model_dump(mode="json"),
                                "fingerprint": span_fingerprint(value),
                            }
                        )
                    )
                except (ValueError, TypeError, KeyError, RecursionError):
                    answers.append(None)
            return tuple(answers)
