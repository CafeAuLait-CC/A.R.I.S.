from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from aris.apps.composition.lyra import build_lyra_components
from aris.core.access.schemas import AccessScope
from aris.core.contracts import DomainError
from aris.modules.lyra.coverage_schemas import ProcessingCoverageConfiguration
from aris.modules.lyra.ledger_schemas import UsagePostingConfiguration
from aris.modules.lyra.residency_sessions import ResidencyFormationRule, residency_fingerprint
from aris.modules.lyra.source_prefix_schemas import SourcePrefixReference
from aris.modules.lyra.trace_consumer_schemas import UsageTraceConfiguration
from aris.modules.lyra.trace_state import UsageContinuityRule
from aris.modules.nova.report_calendar import weekly_report_period
from aris.modules.nova.weekly_coverage import weekly_coverage_target
from tests.test_coverage_builds import build_service, drain
from tests.test_report_calendar import plan
from tests.test_stable_recording_worker import (
    admin_state,
    derivations,
    history_state,
    ledger,
    partition_recording,
    stable_config,
)
from tests.test_usage_ledger import POST

__all__ = ["admin_state", "derivations", "history_state", "ledger", "partition_recording"]


def domain():
    continuity = UsageContinuityRule(max_gap_seconds=45)
    formation = ResidencyFormationRule(minimum_intervals=2, minimum_duration_seconds=30)
    return ProcessingCoverageConfiguration(
        trace=UsageTraceConfiguration(
            consumer_id="weekly-source",
            source_ref="weekly-explicit",
            agent_id="agent-a",
            node_id="node-a",
            initial_after_seq=0,
            continuity=continuity,
            formation=formation,
        ),
        recording=UsagePostingConfiguration(
            source_ref="weekly-method",
            recording_start_at=datetime(2026, 1, 1, tzinfo=UTC),
            timing_fingerprint=residency_fingerprint(continuity, formation),
        ),
        gpu_uuids=("GPU-a",),
    )


def test_due_calendar_binds_personal_owner_node_scope_and_original_week_endpoints():
    cfg = domain()
    for mode in ("usage.mine", "usage.node"):
        calendar = plan(report_type=mode)
        period = weekly_report_period(calendar, calendar.first_week)
        target = weekly_coverage_target(calendar, period, cfg, now=period.scheduled_for)
        assert target.query.owner_id == (calendar.owner_id if mode == "usage.mine" else None)
        assert (target.query.start_at, target.query.end_at, target.query.timezone) == (
            period.start_at,
            period.end_at,
            calendar.timezone,
        )
        assert (
            target.id != period.id and target.expected_configuration_fingerprint == cfg.fingerprint
        )
        assert (
            weekly_coverage_target(
                calendar, period, cfg, now=period.scheduled_for + timedelta(days=1)
            )
            == target
        )
        assert target.query.end_at - target.query.start_at == timedelta(hours=167)
        assert "prefix_checkpoint" not in target.model_dump()


def test_changed_inputs_keep_the_same_week_key_for_admission_conflicts():
    calendar = plan(report_type="usage.mine")
    cfg = domain()
    period = weekly_report_period(calendar, calendar.first_week)
    original = weekly_coverage_target(
        calendar, period, cfg, now=period.scheduled_for + timedelta(days=1)
    )
    other_calendar = calendar.model_copy(update={"timezone": "Europe/London"})
    other_period = weekly_report_period(other_calendar, calendar.first_week)
    changed = weekly_coverage_target(
        other_calendar, other_period, cfg, now=period.scheduled_for + timedelta(days=1)
    )
    assert original.id == changed.id and original.query != changed.query
    changed_cfg = cfg.model_copy(update={"gpu_uuids": ("GPU-other",)})
    target = weekly_coverage_target(calendar, period, changed_cfg, now=period.scheduled_for)
    assert (
        target.id == original.id
        and target.expected_configuration_fingerprint != original.expected_configuration_fingerprint
    )
    node = calendar.model_copy(update={"report_type": "usage.node"})
    assert (
        weekly_coverage_target(
            node, weekly_report_period(node, node.first_week), cfg, now=period.scheduled_for
        ).id
        != original.id
    )


def test_mismatched_period_scope_type_or_not_yet_due_is_rejected():
    calendar = plan(report_type="usage.mine", delay_minutes=30)
    cfg = domain()
    period = weekly_report_period(calendar, calendar.first_week)
    cases = [
        (calendar, period, period.scheduled_for - timedelta(microseconds=1)),
        (calendar, period, datetime(2026, 4, 1)),
        (
            calendar.model_copy(update={"report_type": "recorded-node-weekly"}),
            period,
            period.scheduled_for,
        ),
        (
            calendar.model_copy(update={"scope": AccessScope(type="global", id="*")}),
            period,
            period.scheduled_for,
        ),
        (
            calendar.model_copy(update={"scope": AccessScope(type="node", id="node-b")}),
            period,
            period.scheduled_for,
        ),
        (
            calendar,
            period.model_copy(update={"start_at": period.start_at + timedelta(seconds=1)}),
            period.scheduled_for,
        ),
    ]
    for item, value, now in cases:
        with pytest.raises(ValueError):
            weekly_coverage_target(item, value, cfg, now=now)


def test_prefix_is_context_checked_and_passed_unchanged_for_actual_admission():
    calendar = plan(report_type="usage.mine")
    cfg = domain()
    period = weekly_report_period(calendar, calendar.first_week)
    prefix = SourcePrefixReference(
        id=uuid4(),
        fingerprint="1" * 64,
        configuration_fingerprint=cfg.trace.fingerprint,
        through_revision=2,
        last_seq=32,
        attestation_id=uuid4(),
        attestation_fingerprint="2" * 64,
        max_observed_at=period.start_at - timedelta(seconds=1),
        created_at=period.start_at,
    )
    target = weekly_coverage_target(
        calendar, period, cfg, now=period.scheduled_for, prefix_checkpoint=prefix
    )
    assert target.prefix_checkpoint == prefix
    for changes in (
        {"configuration_fingerprint": "f" * 64},
        {"max_observed_at": period.start_at},
        {"created_at": period.scheduled_for + timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError):
            weekly_coverage_target(
                calendar,
                period,
                cfg,
                now=period.scheduled_for,
                prefix_checkpoint=prefix.model_copy(update=changes),
            )


def test_due_week_target_admits_and_resumes_original_build_without_claiming_full_week(
    partition_recording,
):
    from aris.modules.lyra.coverage_service import ProcessingCoverageService

    state = partition_recording
    calendar = plan(report_type="usage.node", timezone="UTC", first_week=date(2026, 11, 16))
    period = weekly_report_period(calendar, calendar.first_week)
    state[4]["now"] = max(state[4]["now"], period.end_at + timedelta(seconds=1))
    runtime = stable_config(state)
    graph = build_lyra_components(state[0].sessions, runtime, clock=lambda: state[4]["now"])
    for _ in range(8):
        assert not graph.run_once()["failures"]
    producer = graph.pump.pipelines[0]
    config = ProcessingCoverageConfiguration(
        trace=producer.configuration.trace, recording=runtime.recording, gpu_uuids=("GPU-a",)
    )
    coverage = ProcessingCoverageService(
        state[0].sessions,
        producer.attestations,
        producer.trace.archive,
        producer.trace.observations,
        producer.plans.acceptances.identities,
        configuration=config,
        clock=lambda: state[4]["now"],
    )
    builds = build_service(coverage)
    target = weekly_coverage_target(calendar, period, config, now=state[4]["now"])
    admitted = builds.admit_current(POST, target)
    assert admitted.request.id == target.id
    assert builds.admit_current(POST, target) == admitted
    result = drain(builds, admitted).snapshot
    assert result.window.query == target.query
    assert result.window.recorded_microseconds == 135000000
    assert result.window.coverage == "partial" and result.window.total_usage_microseconds is None
    assert builds.windows.reverify(result.request.id) == result
    changed = calendar.model_copy(update={"timezone": "Europe/London"})
    other_period = weekly_report_period(changed, changed.first_week)
    other = weekly_coverage_target(changed, other_period, config, now=state[4]["now"])
    # November London and UTC have the same UTC endpoints but a distinct calendar label.
    assert other.id == target.id and other.query != target.query
    with pytest.raises(DomainError, match="different input"):
        builds.admit_current(POST, other)
