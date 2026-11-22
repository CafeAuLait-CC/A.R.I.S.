"""Report durable source dependencies to a local operator."""

from datetime import UTC, datetime
from itertools import batched
from time import monotonic
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import Contract, DomainError
from aris.modules.lyra.coverage_build_schemas import completed_coverage_window_id
from aris.modules.lyra.retention_repo import RetentionRepository
from aris.modules.lyra.source_prefix_repo import SourcePrefixRepository
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConfiguration
from aris.modules.lyra.window_schemas import UsageWindowQuery
from aris.modules.lyra.window_snapshot_repo import UsageWindowRepository
from aris.modules.vega.usage_archive_schemas import UsageArchiveFreeze, UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive

MAX_CONSUMERS = 16
MAX_REVIEWED_BUILDS = 1024
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


class TraceRetentionBoundary(Contract):
    consumer_id: str
    processed_through_seq: int
    checkpoint_through_seq: int | None
    earliest_protected_seq: int | None


class CoverageBuildDependencies(Contract):
    reviewed_builds: int
    builds_without_saved_windows: int
    truncated: bool


def coverage_build_dependencies_in_transaction(
    session: Session, node_id: str
) -> CoverageBuildDependencies:
    """Check whether admitted plans have corresponding immutable interval snapshots."""
    repo = RetentionRepository(session)
    ids = repo.coverage_build_ids(node_id, MAX_REVIEWED_BUILDS + 1)
    selected = ids[:MAX_REVIEWED_BUILDS]
    missing = 0
    for batch in batched(selected, 128):
        expected = tuple(completed_coverage_window_id(id) for id in batch)
        missing += len(expected) - repo.saved_coverage_build_count(node_id, expected)
    return CoverageBuildDependencies(
        reviewed_builds=len(selected),
        builds_without_saved_windows=missing,
        truncated=len(ids) > len(selected),
    )


def retained_day_archive_boundary_in_transaction(
    session: Session, query: UsageWindowQuery, snapshot_id: UUID, expected_fingerprint: str
) -> UsageArchiveFreeze | None:
    """Use the stored interval snapshot's fixed reading boundary, without rebuilding it."""
    saved = UsageWindowRepository(session).get(snapshot_id)
    if saved is None:
        return None
    if (
        saved.window.query != query
        or saved.fingerprint != expected_fingerprint
        or query.owner_id is not None
    ):
        raise DomainError("CONFLICT", "Retention day has a different fixed source", 409)
    source = saved.window.source
    return UsageArchiveFreeze(
        source=source.query,
        start_at=query.start_at,
        end_at=query.end_at,
        read_at=source.read_at,
        through_seq=source.highest_committed_seq,
    )


def trace_retention_boundaries_in_transaction(
    session: Session, configuration: UsageTraceConfiguration, before: datetime
) -> tuple[TraceRetentionBoundary, ...]:
    """Summarize all durable consumers of the same source without unprotecting any data."""
    repo = RetentionRepository(session)
    count = repo.cursor_count()
    if count > MAX_CONSUMERS:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Retention exceeds consumer limit", 503)
    checkpoints = SourcePrefixRepository(session)
    boundaries = []
    for position in repo.positions(count):
        config = position.configuration
        if (config.agent_id, config.node_id) != (configuration.agent_id, configuration.node_id):
            continue
        if config.consumer_id == configuration.consumer_id and config != configuration:
            raise DomainError("CONFLICT", "Retention trace configuration differs", 409)
        prefix = checkpoints.eligible(config, before, position.revision)
        anchors = (
            [run.first.seq for run in position.residency.open_runs] if position.residency else []
        )
        if position.trace is not None:
            anchors.append(position.trace.anchor.reference.seq)
        boundaries.append(
            TraceRetentionBoundary(
                consumer_id=config.consumer_id,
                processed_through_seq=position.after_seq,
                checkpoint_through_seq=prefix.reference().last_seq if prefix else None,
                earliest_protected_seq=min(anchors, default=None),
            )
        )
    return tuple(boundaries)


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
