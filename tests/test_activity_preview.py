import hashlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from aris.core.contracts import DomainError
from aris.modules.lyra.activity_preview import ActivityPreviewRule, group_compute_observations
from aris.modules.lyra.reporting_gaps import archived_reporting_gaps
from aris.modules.vega.usage_archive_schemas import (
    ArchivedUsageItem,
    ArchivedUsageReference,
    UsageArchiveHead,
    UsageArchiveHeadQuery,
    UsageArchivePage,
    UsageArchiveQuery,
    archive_head_fingerprint,
    archive_page_fingerprint,
)
from tests.test_historical_usage_facts import owner, ownership, projection
from tests.test_resource_telemetry import resource_batch

BASE = datetime(2026, 11, 18, 12, tzinfo=UTC)
RULE = ActivityPreviewRule(source_ref="fixture-diagnostic-only", max_gap_seconds=45)


def fact(
    seq,
    seconds,
    *,
    compute=None,
    uid=1001,
    idle=False,
    model=None,
    gpu_quality="ok",
    no_targets=False,
):
    raw = resource_batch(
        seq=seq,
        observed_at=BASE + timedelta(seconds=seconds),
        compute_age=0 if compute is None else seconds - compute,
        gpu_quality=gpu_quality,
    )
    ownership(raw, [] if idle else [owner(uid=uid, username="private-fixture-name")])
    if idle:
        raw["resources"]["compute"].update(
            reported_processes=0,
            gpus=[{"gpu_uuid": "GPU-a", "reported_processes": 0, "reported_memory_mib": None}],
        )
    if model is not None:
        raw["resources"]["gpu"]["gpus"][0]["model"] = model
    if no_targets:
        raw["resources"]["gpu"]["gpus"] = []
        raw["resources"]["compute"].update(reported_processes=0, gpus=[])
    value = projection(raw)
    return ArchivedUsageItem(
        reference=ArchivedUsageReference(
            agent_id=value.agent_id,
            node_id=value.node_id,
            message_id=value.message_id,
            seq=value.seq,
            kind="telemetry",
            fingerprint=value.source_fingerprint,
            observed_at=value.observed_at,
            received_at=value.received_at,
        ),
        observation=value,
    )


def heart(seq, seconds):
    return ArchivedUsageItem(
        reference=ArchivedUsageReference(
            agent_id="agent-a",
            node_id="node-a",
            message_id=uuid4(),
            seq=seq,
            kind="heartbeat",
            fingerprint=hashlib.sha256(f"fixture-heart-{seq}".encode()).hexdigest(),
            observed_at=BASE + timedelta(seconds=seconds),
            received_at=datetime.now(UTC),
        )
    )


def source(items, *, highest=None, max_messages=None, characters=None):
    highest = (
        highest if highest is not None else max((item.reference.seq for item in items), default=0)
    )
    maximum = max_messages or max(1, highest)
    now = datetime.now(UTC) + timedelta(seconds=1)
    head = UsageArchiveHead.model_construct(
        query=UsageArchiveHeadQuery(agent_id="agent-a", node_id="node-a"),
        highest_committed_seq=highest,
        read_at=now,
        fingerprint="0" * 64,
    )
    head = UsageArchiveHead.model_validate(
        {**head.model_dump(mode="json"), "fingerprint": archive_head_fingerprint(head)}
    )
    by_seq = {item.reference.seq: item for item in items}
    cursor = max(0, highest - maximum)
    pages = []
    while cursor < highest:
        query = UsageArchiveQuery(
            agent_id="agent-a", node_id="node-a", after_seq=cursor, through_seq=highest
        )
        selected = []
        for seq in range(cursor + 1, query.page_through_seq + 1):
            if seq not in by_seq:
                break
            selected.append(by_seq[seq])
        end = cursor + len(selected)
        state = (
            "range_end"
            if end == highest
            else "page_end"
            if end == query.page_through_seq
            else "gap"
        )
        page = UsageArchivePage.model_construct(
            query=query,
            items=tuple(selected),
            source_characters=(len(selected) * 1000 if characters is None else characters)
            if selected
            else 0,
            next_after_seq=end,
            state=state,
            missing_seq=end + 1 if state == "gap" else None,
            read_at=now,
            fingerprint="0" * 64,
        )
        pages.append(
            UsageArchivePage.model_validate(
                {**page.model_dump(mode="json"), "fingerprint": archive_page_fingerprint(page)}
            )
        )
        cursor = end
        if state == "gap":
            break
    return head, tuple(pages), maximum


def grouped(items, *, rule=RULE, **changes):
    head, pages, maximum = source(items, **changes)
    return group_compute_observations(head, pages, rule, max_messages=maximum)


def test_reporting_gaps_separate_clock_order_and_preserve_missing_source_boundary():
    items = []
    for seq in range(1, 132):
        observed = seq + (180 if seq >= 130 else 0) + (180 if seq == 131 else 0)
        received = seq + (179 if seq >= 129 else 0) - (2 if seq == 131 else 0)
        item = heart(seq, observed)
        items.append(
            item.model_copy(
                update={
                    "reference": item.reference.model_copy(
                        update={"received_at": BASE + timedelta(seconds=received)}
                    )
                }
            )
        )
    head, pages, maximum = source(items, highest=133)
    preview = group_compute_observations(head, pages, RULE, max_messages=maximum)
    result = archived_reporting_gaps(preview, pages)
    assert preview.missing_seq == 132 and not result.source_range_complete
    assert result.source_preview_fingerprint == preview.fingerprint
    assert result.pairs_with_uncertain_clock_order == 1
    delivery, observation, reordered = result.gaps
    assert delivery.previous_report.seq == "128" and delivery.next_report.seq == "129"
    assert delivery.large_received_interval and not delivery.large_observed_interval
    assert observation.previous_report.seq == "129" and observation.next_report.seq == "130"
    assert observation.large_observed_interval and not observation.large_received_interval
    assert reordered.previous_report.seq == "130" and reordered.next_report.seq == "131"
    assert reordered.large_observed_interval and not reordered.large_received_interval
    assert observation.cause == delivery.cause == reordered.cause == "unknown"


def test_long_fixed_page_chain_groups_samples_without_usage_authority_or_raw_owners():
    items = []
    for index in range(512):
        items.extend((fact(index * 2 + 1, index * 15), heart(index * 2 + 2, index * 15 + 7)))
    head, pages, maximum = source(items)
    value = group_compute_observations(head, pages, RULE, max_messages=maximum)
    assert (
        len(value.groups) == 1 and value.source_packets == 1024 and value.heartbeat_packets == 512
    )
    assert value.distinct_compute_frames == 512 and value.input_gpu_facts == 512
    assert value.groups[0].envelopes == value.groups[0].distinct_compute_samples == 512
    assert value.groups[0].state == "reported_busy" and value.range_complete
    assert (
        not value.continuous_usage_established
        and not value.retention_enabled
        and value.total_usage_microseconds is None
    )
    assert (
        "private-fixture-name" not in value.model_dump_json()
        and '"uid"' not in value.model_dump_json()
    )
    later = head.model_copy(update={"read_at": head.read_at + timedelta(minutes=1)})
    assert (
        group_compute_observations(later, pages, RULE, max_messages=maximum).fingerprint
        == value.fingerprint
    )


def test_owner_inventory_model_and_unknown_quality_changes_split_original_groups():
    value = grouped(
        [
            fact(1, 0),
            fact(2, 15, uid=2002),
            fact(3, 30, uid=2002, model="Different model"),
            fact(4, 45, gpu_quality="error"),
            fact(5, 60, idle=True, no_targets=True),
            fact(6, 75, idle=True),
        ]
    )
    assert len(value.groups) == 6
    assert [group.state for group in value.groups] == ["reported_busy"] * 3 + ["unknown"] * 2 + [
        "reported_idle"
    ]
    assert value.groups[2].model == "Different model" and value.groups[3].gpu_quality == "error"
    assert (
        value.groups[4].reason == "NOT_IN_REPORTED_INVENTORY"
        and value.missing_inventory_markers == 1
    )
    assert value.no_target_telemetry == 1 and value.groups[-1].reported_processes == 0


def test_cached_time_and_conflicts_stay_unknown_until_new_sample():
    value = grouped(
        [
            fact(1, 0),
            fact(2, 15, compute=0),
            fact(3, 30, compute=0, uid=2002),
            fact(4, 40, compute=0),
            fact(5, 45),
        ]
    )
    first = value.groups[0]
    assert (
        first.envelopes == 2
        and first.distinct_compute_samples == 1
        and first.repeated_compute_envelopes == 1
    )
    assert (
        first.last_source.observed_at == BASE + timedelta(seconds=15)
        and first.last_compute_observed_at == BASE
    )
    assert all(
        group.reason == "SAME_TIME_FACT_CONFLICT" and group.state == "unknown"
        for group in value.groups[1:-1]
    )
    assert value.groups[-1].state == "reported_busy" and value.distinct_compute_frames == 2
    assert value.repeated_compute_frames == 3


def test_clock_high_water_quarantines_old_times_in_source_order_across_page_boundary():
    items = [fact(index + 1, index) for index in range(128)]
    items.extend(
        [fact(129, 128, compute=120), fact(130, 129, compute=127), fact(131, 130, compute=128)]
    )
    value = grouped(items)
    assert len(value.source_pages) == 2
    unknown = [group for group in value.groups if group.state == "unknown"]
    assert unknown and all(group.reason == "SOURCE_CLOCK_REGRESSION" for group in unknown)
    assert value.groups[-1].state == "reported_busy" and value.groups[-1].first_source.seq == 131
    assert value.distinct_compute_frames == 129


def test_sample_gaps_missing_sequences_empty_source_and_explicit_rule_changes_are_distinct():
    value = grouped([fact(1, 0, idle=True), heart(2, 7), fact(3, 180, idle=True)])
    assert (
        len(value.groups) == 2
        and len(value.gaps) == 1
        and all(group.state == "reported_idle" for group in value.groups)
    )
    missing = grouped([fact(1, 0, idle=True), fact(3, 180, idle=True)])
    assert missing.missing_seq == 2 and missing.last_read_seq == 1 and not missing.range_complete
    assert missing.source_packets == 1 and len(missing.groups) == 1
    empty = grouped([], highest=0)
    assert not empty.groups and empty.range_complete and not empty.continuous_usage_established
    none = grouped([heart(2, 7)], highest=2)
    assert none.missing_seq == 1 and none.source_pages[0].source_characters == 0 and not none.groups
    changed = grouped(
        [fact(1, 0), fact(2, 50)], rule=RULE.model_copy(update={"max_gap_seconds": 60})
    )
    original = grouped([fact(1, 0), fact(2, 50)])
    assert (
        len(changed.groups) == 1
        and len(original.groups) == 2
        and changed.rule_fingerprint != original.rule_fingerprint
    )


def test_source_chain_fingerprints_and_budgets_fail_before_truncated_success():
    head, pages, maximum = source([fact(1, 0), fact(2, 15)])
    with pytest.raises((DomainError, ValidationError)):
        group_compute_observations(
            head,
            (pages[0].model_copy(update={"fingerprint": "f" * 64}),),
            RULE,
            max_messages=maximum,
        )
    with pytest.raises(DomainError):
        group_compute_observations(head, pages + pages, RULE, max_messages=maximum)
    with pytest.raises(DomainError) as error:
        group_compute_observations(head, pages, RULE, max_messages=1025)
    assert error.value.code == "ACTIVITY_PREVIEW_TOO_LARGE"
    raw = resource_batch(observed_at=BASE)
    template = raw["resources"]["gpu"]["gpus"][0]
    raw["resources"]["gpu"]["gpus"] = [
        {**template, "gpu_uuid": f"GPU-{index}", "index": index} for index in range(33)
    ]
    targets = [{"gpu_uuid": f"GPU-{index}", "reported_processes": 1} for index in range(33)]
    raw["resources"]["compute"].update(reported_processes=33, gpus=targets)
    ownership(raw, [{**owner(gpus=targets), "reported_processes": 33}])
    wide = projection(raw)
    item = ArchivedUsageItem(
        reference=ArchivedUsageReference(
            agent_id=wide.agent_id,
            node_id=wide.node_id,
            message_id=wide.message_id,
            seq=wide.seq,
            kind="telemetry",
            fingerprint=wide.source_fingerprint,
            observed_at=wide.observed_at,
            received_at=wide.received_at,
        ),
        observation=wide,
    )
    with pytest.raises(DomainError) as error:
        grouped([item])
    assert error.value.code == "ACTIVITY_PREVIEW_TOO_LARGE"
    head, pages, maximum = source(
        [fact(index + 1, index) for index in range(640)], characters=2 * 1024 * 1024
    )
    with pytest.raises(DomainError) as error:
        group_compute_observations(head, pages, RULE, max_messages=maximum)
    assert error.value.code == "ACTIVITY_PREVIEW_TOO_LARGE"
    with pytest.raises(ValidationError):
        ActivityPreviewRule(source_ref="fixture", max_gap_seconds=True)


def test_fixed_older_window_has_its_own_complete_range_below_the_current_source_head():
    from aris.modules.vega.usage_archive_schemas import archive_head_fingerprint

    items = [fact(seq, seq * 15) for seq in range(1, 7)]
    head, pages, maximum = source(items, highest=6, max_messages=3)
    head = head.model_copy(update={"highest_committed_seq": 9})
    head = type(head).model_validate(
        {**head.model_dump(mode="json"), "fingerprint": archive_head_fingerprint(head)}
    )
    value = group_compute_observations(head, pages, RULE, max_messages=maximum, through_seq=6)
    assert value.highest_committed_seq == 9 and value.selected_through_seq == 6
    assert value.requested_after_seq == 3 and value.last_read_seq == 6
    assert value.range_complete and value.source_packets == 3 and value.missing_seq is None
    for end in (0, True, 10, 7):
        with pytest.raises(DomainError):
            group_compute_observations(head, pages, RULE, max_messages=maximum, through_seq=end)
