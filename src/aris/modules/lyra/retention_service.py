"""Report durable source dependencies to a local operator."""

from datetime import UTC, datetime
from time import monotonic
from typing import Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import Contract, DomainError
from aris.modules.lyra.retention_repo import RetentionRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive

MAX_CONSUMERS = 16
READ_SECONDS = 3


class TraceRetentionDependency(Contract):
    consumer_id: str
    agent_id: str
    node_id: str
    revision: int
    initial_after_seq: int
    processed_through_seq: int
    highest_committed_seq: int
    last_observation_anchor_seq: int | None
    earliest_open_run_seq: int | None
    open_runs: int


class RetentionEvidenceCounts(Contract):
    source_attestations: int
    coverage_build_plans: int
    coverage_build_steps: int
    saved_windows: int
    saved_recorded_weekly_reports: int


class LyraRetentionInventory(Contract):
    read_only: Literal[True] = True
    deletion_enabled: Literal[False] = False
    eligibility: Literal["not_assessed"] = "not_assessed"
    generated_at: AwareDatetime
    consumers: tuple[TraceRetentionDependency, ...] = Field(max_length=MAX_CONSUMERS)
    recording_jobs_by_state: dict[str, int]
    evidence: RetentionEvidenceCounts


def inspect_retention_dependencies(sessions: sessionmaker[Session]) -> LyraRetentionInventory:
    deadline = monotonic() + READ_SECONDS
    try:
        with sessions() as session, session.no_autoflush:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            elif session.get_bind().dialect.name == "sqlite":
                session.execute(text("BEGIN"))
            transaction_budget(session, 100, 1000, READ_SECONDS * 1000)
            repo = RetentionRepository(session)
            count = repo.cursor_count()
            if count > MAX_CONSUMERS:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Retention inventory exceeds consumer limit", 503
                )
            consumers = []
            archive = VegaUsageArchive(sessions)
            for position in repo.positions(count):
                config = position.configuration
                head = archive.head_in_transaction(
                    session,
                    UsageArchiveHeadQuery(
                        agent_id=config.agent_id,
                        node_id=config.node_id,
                    ),
                )
                if head.highest_committed_seq < position.after_seq:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Archive is behind the recorded cursor", 503
                    )
                runs = position.residency.open_runs if position.residency else ()
                consumers.append(
                    TraceRetentionDependency(
                        consumer_id=config.consumer_id,
                        agent_id=config.agent_id,
                        node_id=config.node_id,
                        revision=position.revision,
                        initial_after_seq=config.initial_after_seq,
                        processed_through_seq=position.after_seq,
                        highest_committed_seq=head.highest_committed_seq,
                        last_observation_anchor_seq=position.trace.anchor.reference.seq
                        if position.trace
                        else None,
                        earliest_open_run_seq=min((run.first.seq for run in runs), default=None),
                        open_runs=len(runs),
                    )
                )
            result = LyraRetentionInventory(
                generated_at=datetime.now(UTC),
                consumers=tuple(consumers),
                recording_jobs_by_state=repo.jobs_by_state(),
                evidence=RetentionEvidenceCounts.model_validate(repo.evidence_counts()),
            )
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Retention inventory timed out", 503)
            return result
    except DBAPIError as error:
        code = database_failure(error)
        raise DomainError(
            code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
            "Retention inventory is unavailable",
            503,
        ) from None
