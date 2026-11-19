"""Selected-node recorded totals, retaining each node's independent immutable ledger prefix."""

import json
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from aris.core.access.schemas import Identifier, UserPrincipal
from aris.modules.lyra.ledger_schemas import UsageLedgerHead, ledger_fingerprint
from aris.modules.lyra.recorded_usage import MAX_REPORT_ENTRIES
from aris.modules.lyra.usage_history import UsageHistory, UsageHistoryDay
from aris.modules.lyra.usage_intervals import IntervalContract


class GroupUsageQuery(IntervalContract):
    node_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    start_at: AwareDatetime
    end_at: AwareDatetime
    timezone: str = Field(min_length=1, max_length=128)
    snapshots: tuple[UsageLedgerHead, ...] | None = Field(default=None, max_length=8)

    @field_validator("node_ids")
    @classmethod
    def unique_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("group nodes must be unique")
        return tuple(sorted(values))

    @field_validator("start_at", "end_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("snapshots")
    @classmethod
    def ordered_heads(
        cls, values: tuple[UsageLedgerHead, ...] | None
    ) -> tuple[UsageLedgerHead, ...] | None:
        return tuple(sorted(values, key=lambda head: head.node_id)) if values is not None else None

    @model_validator(mode="after")
    def bounded(self) -> "GroupUsageQuery":
        UserPrincipal.iana_timezone(self.timezone)
        if not timedelta(0) < self.end_at - self.start_at <= timedelta(days=366):
            raise ValueError("group history requires a bounded positive window")
        if (
            self.snapshots is not None
            and tuple(head.node_id for head in self.snapshots) != self.node_ids
        ):
            raise ValueError("group snapshots must name every selected node exactly once")
        return self


class GroupUsageOwner(IntervalContract):
    owner_id: UUID
    recorded_microseconds: int = Field(ge=0)
    node_count: int = Field(ge=1)
    gpu_instance_count: int = Field(ge=1)
    segment_count: int = Field(ge=1)


class GroupUsageReport(IntervalContract):
    assessment_scope: Literal["selected_nodes_recorded_usage"] = "selected_nodes_recorded_usage"
    source_coverage: Literal["unknown"] = "unknown"
    total_usage_microseconds: None = None
    quota_usage_available: Literal[False] = False
    weighted: Literal[False] = False
    snapshot_scope: Literal["independent_fixed_node_prefixes"] = "independent_fixed_node_prefixes"
    query: GroupUsageQuery
    owner_id: UUID | None
    histories: tuple[UsageHistory, ...] = Field(min_length=1, max_length=8)
    read_started_at: AwareDatetime
    read_finished_at: AwareDatetime
    recorded_microseconds: int = Field(ge=0)
    gpu_instance_count: int = Field(ge=0)
    segment_count: int = Field(ge=0, le=MAX_REPORT_ENTRIES)
    owners: tuple[GroupUsageOwner, ...] = Field(max_length=1024)
    days: tuple[UsageHistoryDay, ...] = Field(max_length=368)
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


def group_usage_report(
    query: GroupUsageQuery,
    histories: tuple[UsageHistory, ...],
    *,
    owner_id: UUID | None,
    started_at: datetime,
    finished_at: datetime,
) -> GroupUsageReport:
    query = GroupUsageQuery.model_validate(query.model_dump(mode="json"))
    histories = tuple(sorted(histories, key=lambda item: item.node_id))
    if (
        tuple(item.node_id for item in histories) != query.node_ids
        or not histories
        or any(
            (item.start_at, item.end_at, item.timezone, item.owner_id)
            != (query.start_at, query.end_at, query.timezone, owner_id)
            for item in histories
        )
        or any(item.read_at > finished_at for item in histories)
        or any(
            item.segment_count != len(item.rows)
            or sum(row.recorded_microseconds for row in item.rows) != item.recorded_microseconds
            or sum(day.recorded_microseconds for day in item.days) != item.recorded_microseconds
            or sum(value.recorded_microseconds for value in item.owners)
            != item.recorded_microseconds
            or (owner_id is not None and any(row.owner_id != owner_id for row in item.rows))
            for item in histories
        )
        or started_at > finished_at
    ):
        raise ValueError("group histories differ from the selected scope and window")
    heads = tuple(item.snapshot for item in histories)
    if query.snapshots is not None and query.snapshots != heads:
        raise ValueError("group histories differ from the selected ledger prefixes")
    query = query.model_copy(update={"snapshots": heads})
    owners = []
    for owner in sorted(
        {item.owner_id for history in histories for item in history.owners}, key=str
    ):
        values = [
            (history.node_id, row)
            for history in histories
            for row in history.rows
            if row.owner_id == owner
        ]
        owners.append(
            GroupUsageOwner(
                owner_id=owner,
                recorded_microseconds=sum(row.recorded_microseconds for _, row in values),
                node_count=len({node for node, _ in values}),
                gpu_instance_count=len({(node, row.gpu_uuid) for node, row in values}),
                segment_count=len(values),
            )
        )
    dates = tuple(day.date for day in histories[0].days)
    if any(tuple(day.date for day in history.days) != dates for history in histories):
        raise ValueError("node daily calendars differ")
    days = tuple(
        UsageHistoryDay(
            date=day,
            recorded_microseconds=sum(
                history.days[index].recorded_microseconds for history in histories
            ),
        )
        for index, day in enumerate(dates)
    )
    payload = {
        "query": query.model_dump(mode="json"),
        "owner_id": str(owner_id) if owner_id else None,
        "node_fingerprints": [[item.node_id, item.fingerprint] for item in histories],
    }
    return GroupUsageReport(
        query=query,
        owner_id=owner_id,
        histories=histories,
        read_started_at=started_at,
        read_finished_at=finished_at,
        recorded_microseconds=sum(item.recorded_microseconds for item in histories),
        gpu_instance_count=sum(item.gpu_count for item in histories),
        segment_count=sum(item.segment_count for item in histories),
        owners=tuple(owners),
        days=days,
        fingerprint=ledger_fingerprint(
            "selected_node_recorded_usage",
            json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
        ),
    )
