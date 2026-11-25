"""Inspect excluded early archives without treating them as measured usage."""

from datetime import UTC, datetime, time, timedelta
from time import monotonic
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_build_repo import CoverageBuildRepository
from aris.modules.lyra.retention_repo import RetentionRepository
from aris.modules.lyra.retention_service import (
    MAX_CONSUMERS,
    MAX_REVIEWED_BUILDS,
    coverage_build_dependencies_in_transaction,
    reporting_history_position_in_transaction,
)
from aris.modules.lyra.trace_consumer_schemas import canonical_trace, trace_fingerprint
from aris.modules.lyra.usage_intervals import IntervalContract
from aris.modules.vega.storage_schemas import RetentionPrefixPreview
from aris.modules.vega.usage_archive_schemas import UsageArchiveHeadQuery
from aris.modules.vega.usage_archive_service import VegaUsageArchive

MAX_ORIGIN_REQUESTS = 8192
MAX_ORIGIN_CHARACTERS = 8 * 1024 * 1024
ORIGIN_READ_SECONDS = 30


class RecordingOrigin(IntervalContract):
    consumer_id: str
    configuration_fingerprint: str
    initial_after_seq: int


class PreRecordingRetentionPreview(IntervalContract):
    assessment_scope: Literal["pre_recording_payload_dependencies"] = (
        "pre_recording_payload_dependencies"
    )
    read_only: Literal[True] = True
    deletion_enabled: Literal[False] = False
    source: UsageArchiveHeadQuery
    before: AwareDatetime
    timezone: str
    recording_origins: tuple[RecordingOrigin, ...] = Field(max_length=MAX_CONSUMERS)
    reviewed_derivations: int
    reviewed_request_characters: int
    earliest_derived_source_seq: int | None
    reviewed_builds: int
    earliest_build_source_seq: int | None
    recording_exclusion_through_seq: int | None
    reporting_history_through_seq: int | None
    payload_prefix: RetentionPrefixPreview | None
    blockers: tuple[str, ...]
    remaining_checks: tuple[str, ...] = (
        "bounded_payload_exit_and_successor_rehearsal",
        "other_domain_event_and_audit_lifecycles",
    )
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def preview_pre_recording_retention(
    sessions: sessionmaker[Session],
    source: UsageArchiveHeadQuery,
    *,
    before: datetime,
    timezone: str,
    now: datetime | None = None,
) -> PreRecordingRetentionPreview:
    """Find at most 128 old packets excluded by every known recording origin."""
    source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
    now = now or datetime.now(UTC)
    if before.utcoffset() is None or now.utcoffset() is None:
        raise ValueError("Retention boundaries require timezones")
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    monday = local_now.date() - timedelta(days=local_now.weekday())
    current_week = datetime.combine(monday, time.min, zone)
    local_before = before.astimezone(zone)
    if (
        local_before.date().weekday() != 0
        or local_before.timetz().replace(tzinfo=None) != time.min
        or local_before > current_week
        or before <= datetime(1970, 1, 1, tzinfo=UTC)
    ):
        raise ValueError("Retention cutoff must be a closed natural-week boundary")
    deadline = monotonic() + ORIGIN_READ_SECONDS
    try:
        with sessions() as db, db.no_autoflush:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            else:
                db.execute(text("BEGIN"))
            transaction_budget(db, 100, 1000, ORIGIN_READ_SECONDS * 1000)
            repo = RetentionRepository(db)
            count = repo.cursor_count()
            if count > MAX_CONSUMERS:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Origin inventory exceeds consumer limit", 503
                )
            positions = tuple(
                value
                for value in repo.positions(count)
                if (value.configuration.agent_id, value.configuration.node_id)
                == (source.agent_id, source.node_id)
            )
            origins = tuple(
                RecordingOrigin(
                    consumer_id=value.configuration.consumer_id,
                    configuration_fingerprint=value.configuration.fingerprint,
                    initial_after_seq=value.configuration.initial_after_seq,
                )
                for value in positions
            )
            records, characters, derived_first = repo.derivation_source_bounds(
                source.agent_id,
                source.node_id,
                maximum_records=MAX_ORIGIN_REQUESTS,
                maximum_characters=MAX_ORIGIN_CHARACTERS,
            )
            builds = coverage_build_dependencies_in_transaction(db, source.node_id)
            build_first = None
            if not builds.truncated:
                build_repo = CoverageBuildRepository(db)
                for id in repo.coverage_build_ids(source.node_id, MAX_REVIEWED_BUILDS):
                    build = build_repo.get(id)
                    if build is None or build.source.query != source:
                        raise DomainError("CONFLICT", "Coverage build origin differs", 409)
                    first = build.configuration.trace.initial_after_seq + 1
                    build_first = first if build_first is None else min(build_first, first)
                    if monotonic() >= deadline:
                        raise DomainError(
                            "RETRYABLE_DATABASE_ERROR", "Origin inventory timed out", 503
                        )
            excluded = min((item.initial_after_seq for item in origins), default=None)
            bounds = [value - 1 for value in (derived_first, build_first) if value is not None]
            if excluded is not None:
                bounds.append(excluded)
            history = reporting_history_position_in_transaction(db, source)
            checked = (
                int(history.last_report.seq)
                if history is not None and history.first_seq == 1
                else None
            )
            blockers = []
            if excluded is None:
                blockers.append("recording_origins_missing")
            if builds.builds_without_saved_windows or builds.truncated:
                blockers.append("coverage_build_dependencies_incomplete")
            if checked is None:
                blockers.append("early_reporting_history_not_scanned")
            prefix = None
            if not blockers:
                assert checked is not None
                through = min(*bounds, checked)
                prefix = VegaUsageArchive(sessions).retention_prefix_in_transaction(
                    db,
                    source,
                    start=datetime(1970, 1, 1, tzinfo=UTC),
                    end=before,
                    through_seq=through,
                )
            if monotonic() >= deadline:
                raise DomainError("RETRYABLE_DATABASE_ERROR", "Origin inventory timed out", 503)
            value = PreRecordingRetentionPreview(
                source=source,
                before=before,
                timezone=timezone,
                recording_origins=origins,
                reviewed_derivations=records,
                reviewed_request_characters=characters,
                earliest_derived_source_seq=derived_first,
                reviewed_builds=builds.reviewed_builds,
                earliest_build_source_seq=build_first,
                recording_exclusion_through_seq=excluded,
                reporting_history_through_seq=checked,
                payload_prefix=prefix,
                blockers=tuple(blockers),
                fingerprint="0" * 64,
            )
            return value.model_copy(
                update={
                    "fingerprint": trace_fingerprint(
                        "pre_recording_retention", canonical_trace(value, exclude={"fingerprint"})
                    )
                }
            )
    except DBAPIError as error:
        raise DomainError(database_failure(error), "Origin inventory is unavailable", 503) from None
