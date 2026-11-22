"""Persist only abnormal pairs from a verified archived reporting range."""

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import chain

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.core.db import commit_owned_transaction
from aris.modules.lyra.activity_preview import ActivityPreviewRule
from aris.modules.lyra.activity_preview_service import ActivityPreviewService
from aris.modules.lyra.reporting_gap_history_repo import ReportingGapHistoryRepository
from aris.modules.lyra.reporting_gap_history_schemas import (
    MAX_SAVED_GAPS,
    ReportingGapCapture,
    ReportingGapScan,
    ReportingGapScanPosition,
    StoredReportingGap,
)
from aris.modules.lyra.reporting_gaps import (
    DEFAULT_REPORTING_GAP_SECONDS,
    ReportingGap,
    ReportingPoint,
    reporting_gaps,
)
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_SEQ,
    UsageArchiveHeadQuery,
    UsageArchiveQuery,
)


class ReportingGapHistoryService:
    def __init__(
        self, preview: ActivityPreviewService, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        self.preview = preview
        self.sessions = preview.sessions
        self.access = AccessService(self.sessions)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _permission(actor: ActorContext, source: UsageArchiveHeadQuery) -> AccessRequest:
        return AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_all",
            scope=AccessScope(type="node", id=source.node_id),
        )

    @staticmethod
    def _failure(error: DBAPIError) -> DomainError:
        return DomainError(database_failure(error), "Reporting gap history is unavailable", 503)

    def capture(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        *,
        max_messages: int,
        through_seq: int,
    ) -> ReportingGapCapture:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
        if type(through_seq) is not int or not 1 <= through_seq <= MAX_ARCHIVE_SEQ:
            raise DomainError("VALIDATION_ERROR", "Select a committed archive sequence", 422)
        view = self.preview.recent_reporting_gaps(
            actor,
            source,
            ActivityPreviewRule(
                source_ref="reporting-gap-history", max_gap_seconds=DEFAULT_REPORTING_GAP_SECONDS
            ),
            max_messages=max_messages,
            through_seq=through_seq,
        )
        records = self._records(source, view.gaps, view.interval_threshold_seconds)
        saved = 0
        if records:
            try:
                with self.sessions() as db:
                    transaction_budget(db, 100, 1000, 3000)
                    request = self._permission(actor, source)
                    self.access.require_in_transaction(db, request)
                    saved = ReportingGapHistoryRepository(db).save(source, records)
                    self.access.require_in_transaction(db, request)
                    commit_owned_transaction(db)
            except DBAPIError as error:
                raise self._failure(error) from None
        return ReportingGapCapture(
            source=source,
            source_range_complete=view.source_range_complete,
            saved=saved,
            already_saved=len(records) - saved,
        )

    def _records(
        self, source: UsageArchiveHeadQuery, gaps: tuple[ReportingGap, ...], threshold_seconds: int
    ) -> tuple[StoredReportingGap, ...]:
        if len(gaps) > MAX_SAVED_GAPS:
            raise DomainError("REPORTING_GAP_BATCH_TOO_LARGE", "Select a smaller source range", 422)
        now = self.clock()
        records = []
        for gap in gaps:
            draft = StoredReportingGap.model_construct(
                source=source,
                gap=gap,
                threshold_seconds=threshold_seconds,
                recorded_at=now,
                fingerprint="0" * 64,
            )
            records.append(
                StoredReportingGap.model_validate(
                    {
                        **draft.model_dump(mode="json"),
                        "fingerprint": draft.content_fingerprint(),
                    }
                )
            )
        return tuple(records)

    def scan_next(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        *,
        first_seq: int,
        max_messages: int = MAX_SAVED_GAPS,
    ) -> ReportingGapScan:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
        if (
            type(first_seq) is not int
            or not 1 <= first_seq <= MAX_ARCHIVE_SEQ
            or type(max_messages) is not int
            or not 1 <= max_messages <= MAX_SAVED_GAPS
        ):
            raise DomainError("VALIDATION_ERROR", "Select a bounded reporting scan", 422)
        request = self._permission(actor, source)
        try:
            with self.sessions() as db, db.no_autoflush:
                transaction_budget(db, 100, 1000, 5000)
                self.access.require_in_transaction(db, request)
                repository = ReportingGapHistoryRepository(db)
                previous = repository.position(source)
                head = self.preview.source.head_in_transaction(db, source).highest_committed_seq
                after = first_seq - 1
                uncertain = 0
                if previous is not None:
                    after = int(previous.last_report.seq)
                    uncertain = previous.uncertain_clock_pairs
                    if previous.first_seq != first_seq or after > head:
                        raise DomainError("CONFLICT", "Reporting scan origin or head differs", 409)
                if after >= head:
                    self.access.require_in_transaction(db, request)
                    return ReportingGapScan(
                        source=source,
                        first_seq=first_seq,
                        through_seq=after if previous is not None else None,
                        archive_head=head,
                        state="caught_up",
                        missing_seq=None,
                        saved=0,
                        already_saved=0,
                        uncertain_clock_pairs=uncertain,
                    )
                page = self.preview.source.reporting_page_in_transaction(
                    db,
                    UsageArchiveQuery(
                        agent_id=source.agent_id,
                        node_id=source.node_id,
                        after_seq=after,
                        through_seq=head,
                        limit=max_messages,
                    ),
                )
                reports = tuple(ReportingPoint.from_archive(report) for report in page.reports)
                records: tuple[StoredReportingGap, ...] = ()
                saved = 0
                if reports:
                    anchor = (previous.last_report,) if previous is not None else ()
                    gaps, additional_uncertain = reporting_gaps(
                        chain(anchor, reports), DEFAULT_REPORTING_GAP_SECONDS
                    )
                    uncertain += additional_uncertain
                    draft = ReportingGapScanPosition.model_construct(
                        source=source,
                        first_seq=first_seq,
                        last_report=reports[-1],
                        uncertain_clock_pairs=uncertain,
                        fingerprint="0" * 64,
                    )
                    position = ReportingGapScanPosition.model_validate(
                        {
                            **draft.model_dump(mode="json"),
                            "fingerprint": draft.content_fingerprint(),
                        }
                    )
                    repository.advance(position, previous)
                    records = self._records(source, gaps, DEFAULT_REPORTING_GAP_SECONDS)
                    if records:
                        saved = repository.save(source, records)
                self.access.require_in_transaction(db, request)
                if reports:
                    commit_owned_transaction(db)
                return ReportingGapScan(
                    source=source,
                    first_seq=first_seq,
                    through_seq=page.next_after_seq if (reports or previous is not None) else None,
                    archive_head=head,
                    state={"range_end": "caught_up", "page_end": "more", "gap": "missing"}[
                        page.state
                    ],
                    missing_seq=page.missing_seq,
                    saved=saved,
                    already_saved=len(records) - saved,
                    uncertain_clock_pairs=uncertain,
                )
        except DBAPIError as error:
            raise self._failure(error) from None

    def latest(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        *,
        limit: int = MAX_SAVED_GAPS,
        before_seq: int | None = None,
    ) -> tuple[StoredReportingGap, ...]:
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_SAVED_GAPS
            or (
                before_seq is not None
                and (type(before_seq) is not int or not 1 <= before_seq <= MAX_ARCHIVE_SEQ)
            )
        ):
            raise DomainError("VALIDATION_ERROR", "Select a bounded reporting gap range", 422)
        request = self._permission(actor, source)
        try:
            with self.sessions() as db, db.no_autoflush:
                if db.get_bind().dialect.name == "postgresql":
                    db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                transaction_budget(db, 100, 1000, 3000)
                self.access.require_in_transaction(db, request)
                result = ReportingGapHistoryRepository(db).latest(source, limit, before_seq)
            self.access.require_permission(request)
            return result
        except DBAPIError as error:
            raise self._failure(error) from None
