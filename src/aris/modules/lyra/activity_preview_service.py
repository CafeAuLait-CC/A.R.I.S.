"""Current-authorized source-only diagnostics, with a fixed read-only archive snapshot."""

from time import monotonic

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.service import AccessService
from aris.core.commands.budgets import database_failure, transaction_budget
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.modules.lyra.activity_preview import (
    MAX_PREVIEW_CHARACTERS,
    MAX_PREVIEW_MESSAGES,
    ActivityPreview,
    ActivityPreviewRule,
    group_compute_observations,
)
from aris.modules.lyra.hardware_preview import ActivityHardwarePreview, summarize_gpu_samples
from aris.modules.vega.gpu_hardware_schemas import GpuHardwareBatch, GpuHardwareBatchQuery
from aris.modules.vega.gpu_hardware_service import VegaGpuHardwareObservations
from aris.modules.vega.usage_archive_schemas import (
    MAX_ARCHIVE_SOURCE_CHARACTERS,
    UsageArchiveHeadQuery,
    UsageArchivePage,
    UsageArchiveQuery,
)
from aris.modules.vega.usage_archive_service import VegaUsageArchive


class ActivityPreviewService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        source: VegaUsageArchive,
        *,
        timeout_ms: int = 4000,
        hardware_source: VegaGpuHardwareObservations | None = None,
    ) -> None:
        if source.sessions is not sessions:
            raise ValueError("activity diagnostics require one source database session factory")
        if type(timeout_ms) is not int or not 1000 <= timeout_ms <= 10000:
            raise ValueError("activity diagnostics require a bounded explicit read timeout")
        self.sessions, self.source, self.timeout_ms = sessions, source, timeout_ms
        if hardware_source is not None and hardware_source.sessions is not sessions:
            raise ValueError("hardware preview requires the same source database")
        self.hardware_source = hardware_source
        self.access = AccessService(sessions)

    def recent(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        rule: ActivityPreviewRule,
        *,
        max_messages: int,
    ) -> ActivityPreview:
        value = self._recent(actor, source, rule, max_messages=max_messages, hardware=False)
        assert isinstance(value, ActivityPreview)
        return value

    def recent_with_hardware(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        rule: ActivityPreviewRule,
        *,
        max_messages: int,
    ) -> ActivityHardwarePreview:
        value = self._recent(actor, source, rule, max_messages=max_messages, hardware=True)
        assert isinstance(value, ActivityHardwarePreview)
        return value

    def _current(self, request: AccessRequest) -> None:
        with self.sessions() as session, session.no_autoflush:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET TRANSACTION READ ONLY"))
            transaction_budget(session, 100, 750, min(1500, self.timeout_ms))
            self.access.require_in_transaction(session, request)

    def _recent(
        self,
        actor: ActorContext,
        source: UsageArchiveHeadQuery,
        rule: ActivityPreviewRule,
        *,
        max_messages: int,
        hardware: bool,
    ) -> ActivityPreview | ActivityHardwarePreview:
        if hardware and self.hardware_source is None:
            raise DomainError("USAGE_NOT_CONFIGURED", "GPU hardware preview is not configured", 503)
        actor = ActorContext.model_validate(actor.model_dump(mode="json"))
        source = UsageArchiveHeadQuery.model_validate(source.model_dump(mode="json"))
        rule = ActivityPreviewRule.model_validate(rule.model_dump(mode="json"))
        if type(max_messages) is not int or not 1 <= max_messages <= MAX_PREVIEW_MESSAGES:
            raise DomainError("VALIDATION_ERROR", "Select a bounded recent source range", 422)
        request = AccessRequest(
            user_id=actor.actor_id,
            permission="usage.read_all",
            scope=AccessScope(type="node", id=source.node_id),
        )
        deadline = monotonic() + self.timeout_ms / 1000
        try:
            self._current(request)
            with self.sessions() as session, session.no_autoflush:
                if session.get_bind().dialect.name == "postgresql":
                    session.execute(
                        text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    )
                transaction_budget(session, 100, 750, self.timeout_ms)
                self.access.require_in_transaction(session, request)
                ceiling = self.source.head_in_transaction(session, source)
                cursor = max(0, ceiling.highest_committed_seq - max_messages)
                pages: list[UsageArchivePage] = []
                hardware_batches: list[GpuHardwareBatch] = []
                characters = 0
                while cursor < ceiling.highest_committed_seq:
                    if len(pages) >= 8 or characters >= MAX_PREVIEW_CHARACTERS:
                        raise DomainError(
                            "ACTIVITY_PREVIEW_TOO_LARGE",
                            "Recent archive exceeds the preview budget",
                            503,
                        )
                    if monotonic() >= deadline:
                        raise DomainError(
                            "RETRYABLE_DATABASE_ERROR", "Activity source read budget exceeded", 503
                        )
                    query = UsageArchiveQuery(
                        agent_id=source.agent_id,
                        node_id=source.node_id,
                        after_seq=cursor,
                        through_seq=ceiling.highest_committed_seq,
                        limit=min(128, ceiling.highest_committed_seq - cursor),
                        source_character_budget=min(
                            MAX_ARCHIVE_SOURCE_CHARACTERS, MAX_PREVIEW_CHARACTERS - characters
                        ),
                    )
                    page = self.source.page_in_transaction(session, query)
                    pages.append(page)
                    references = tuple(
                        item.reference for item in page.items if item.observation is not None
                    )
                    if hardware and references:
                        assert self.hardware_source is not None
                        hardware_batches.append(
                            self.hardware_source.batch_in_transaction(
                                session, GpuHardwareBatchQuery(references=references)
                            )
                        )
                    characters += page.source_characters
                    cursor = page.next_after_seq
                    if page.state == "gap":
                        break
                preview = group_compute_observations(
                    ceiling, tuple(pages), rule, max_messages=max_messages
                )
                result: ActivityPreview | ActivityHardwarePreview = (
                    ActivityHardwarePreview(
                        preview=preview,
                        hardware=summarize_gpu_samples(
                            preview, tuple(pages), tuple(hardware_batches)
                        ),
                    )
                    if hardware
                    else preview
                )
            # Source repeatability is independent of current authorization.
            # This fresh transaction observes revocation after the source snapshot.
            self._current(request)
            if monotonic() >= deadline:
                raise DomainError(
                    "RETRYABLE_DATABASE_ERROR", "Activity preview read budget exceeded", 503
                )
            return result
        except DBAPIError as error:
            code = database_failure(error)
            raise DomainError(
                code if code == "RETRYABLE_DATABASE_ERROR" else "DEPENDENCY_UNAVAILABLE",
                "Activity source database is unavailable",
                503,
            ) from None
