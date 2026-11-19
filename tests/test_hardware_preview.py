from datetime import timedelta

import pytest
from sqlalchemy import text, update

from aris.core.contracts import DomainError
from aris.interfaces.usage.activity_service import hardware_view
from aris.modules.lyra.activity_preview import group_compute_observations
from aris.modules.lyra.hardware_preview import summarize_gpu_samples
from aris.modules.vega.gpu_hardware_schemas import (
    GpuHardwareBatch,
    GpuHardwareBatchQuery,
    GpuHardwareObservation,
    hardware_batch_fingerprint,
)
from aris.modules.vega.gpu_hardware_service import VegaGpuHardwareObservations
from aris.modules.vega.models import AgentMessage
from aris.modules.vega.resource_schemas import ResourceTelemetryBatch
from aris.modules.vega.usage_archive_schemas import ArchivedUsageItem, ArchivedUsageReference
from tests.test_activity_preview import BASE, RULE, source
from tests.test_activity_preview_service import SOURCE, preview_source
from tests.test_administration import admin_state
from tests.test_historical_usage_facts import projection
from tests.test_mapping_history import history_state
from tests.test_recorded_usage import readings
from tests.test_resource_telemetry import resource_batch
from tests.test_usage_derivations import derivations
from tests.test_usage_ledger import counts, ledger

__all__ = ["admin_state", "history_state", "derivations", "ledger", "readings", "preview_source"]


def inputs(specifications):
    items = []
    observations = []
    for index, spec in enumerate(specifications, 1):
        seconds = spec.get("seconds", (index - 1) * 15)
        raw = resource_batch(
            seq=index,
            observed_at=BASE + timedelta(seconds=seconds),
            gpu_quality=spec.get("quality", "ok"),
        )
        raw["resources"]["gpu"]["observed_at"] = (
            BASE + timedelta(seconds=spec.get("gpu_seconds", seconds))
        ).isoformat()
        if spec.get("no_gpu"):
            raw["resources"]["gpu"]["gpus"] = []
        for gpu in raw["resources"]["gpu"]["gpus"]:
            gpu.update(
                utilization_percent=spec.get("utilization", 10),
                memory_used_mib=spec.get("memory", 1000),
                temperature_c=spec.get("temperature"),
                model=spec.get("model", "fixture GPU"),
                memory_total_mib=spec.get("total", 49140),
            )
        fact = projection(raw)
        reference = ArchivedUsageReference(
            agent_id=fact.agent_id,
            node_id=fact.node_id,
            message_id=fact.message_id,
            seq=fact.seq,
            kind="telemetry",
            fingerprint=fact.source_fingerprint,
            observed_at=fact.observed_at,
            received_at=fact.received_at,
        )
        items.append(ArchivedUsageItem(reference=reference, observation=fact))
        component = ResourceTelemetryBatch.model_validate(raw).resources.gpu
        observations.append(
            GpuHardwareObservation(
                reference=reference,
                gpu_observed_at=component.observed_at,
                gpu_quality=component.quality,
                gpus=tuple(component.gpus),
            )
        )
    head, pages, maximum = source(items)
    preview = group_compute_observations(head, pages, RULE, max_messages=maximum)
    batches = []
    for offset in range(0, len(observations), 128):
        selected = tuple(observations[offset : offset + 128])
        query = GpuHardwareBatchQuery(references=tuple(item.reference for item in selected))
        draft = GpuHardwareBatch.model_construct(
            query=query,
            observations=selected,
            source_characters=len(selected) * 1000,
            fingerprint="0" * 64,
        )
        batches.append(
            GpuHardwareBatch.model_validate(
                {**draft.model_dump(mode="json"), "fingerprint": hardware_batch_fingerprint(draft)}
            )
        )
    return preview, pages, tuple(batches)


def summary(specifications):
    return summarize_gpu_samples(*inputs(specifications))


def test_independent_hardware_samples_exclude_cache_and_keep_missing_temperature_unknown():
    value = summary(
        [{}, {"gpu_seconds": 0}, {"utilization": 70, "memory": 2000, "temperature": -5}]
    )
    assert (value.independent_frames, value.cached_frames, value.rejected_frames) == (2, 1, 0)
    series = value.series[0]
    assert series.independent_samples == 2 and series.utilization_percent.sum == 80
    assert series.memory_used_mib.sum == 3000 and series.temperature_c.samples == 1
    assert series.temperature_c.minimum == -5 and series.missing_temperature_samples == 1
    assert series.first_gpu_observed_at == BASE and series.last_gpu_observed_at == BASE + timedelta(
        seconds=30
    )
    empty_temperature = summary([{}]).series[0].temperature_c
    assert (
        empty_temperature.samples == 0
        and empty_temperature.minimum is None
        and empty_temperature.maximum is None
    )
    assert [point.seq for point in series.points] == [1, 3]
    assert [point.break_before for point in series.points] == [True, False]
    assert series.points[0].temperature_c is None
    assert not value.time_weighted and not value.quota_usage_available


def test_same_time_hardware_conflict_removes_prior_sample_and_clock_recovery_uses_high_water():
    value = summary(
        [
            {},
            {"gpu_seconds": 0, "utilization": 11},
            {"utilization": 30},
            {"gpu_seconds": 15},
            {"gpu_seconds": 30, "utilization": 30},
            {"utilization": 60},
        ]
    )
    assert value.independent_frames == 2 and value.rejected_frames == 4
    assert value.rejection_reasons == {
        "SAME_TIME_HARDWARE_CONFLICT": 2,
        "SOURCE_CLOCK_REGRESSION": 2,
    }
    assert value.series[0].utilization_percent.sum == 90
    assert [point.seq for point in value.series[0].points] == [3, 6]
    assert all(point.break_before for point in value.series[0].points)
    specifications = [{} for _ in range(130)]
    specifications[128] = {"gpu_seconds": 0}
    paged = summary(specifications)
    assert paged.rejection_reasons == {"SOURCE_CLOCK_REGRESSION": 1}
    assert paged.independent_frames == 129
    assert paged.series[0].points[-1].seq == 130
    assert paged.series[0].points[-1].break_before


def test_stale_quality_empty_and_changed_capacity_are_distinct_from_zero_statistics():
    value = summary(
        [
            {"seconds": 120, "gpu_seconds": 0},
            {"seconds": 135, "temperature": 0},
            {"seconds": 150, "quality": "error"},
            {"seconds": 165, "no_gpu": True},
            {"seconds": 180, "model": "replacement", "total": 65536},
        ]
    )
    assert value.rejection_reasons == {"GPU_COMPONENT_STALE": 1, "GPU_COMPONENT_UNAVAILABLE": 1}
    assert value.no_target_frames == 2 and value.gpu_quality_frames == {"error": 1, "ok": 4}
    assert len(value.series) == 2
    assert {series.memory_total_mib for series in value.series} == {49140, 65536}
    original = next(series for series in value.series if series.model == "fixture GPU")
    assert original.independent_samples == 1 and original.rejected_envelopes == 1
    assert original.temperature_c.minimum == 0 and original.temperature_c.samples == 1
    absent = summary([{"seconds": 100, "gpu_seconds": 0}]).series[0]
    assert absent.first_source is None and absent.utilization_percent.minimum is None


def test_summary_binds_original_sources_and_preserves_large_numbers():
    preview, pages, batches = inputs([{"memory": (1 << 54) + 3, "total": (1 << 55) + 1}])
    value = summarize_gpu_samples(preview, pages, batches)
    output = hardware_view(value).model_dump(mode="json")
    assert output["series"][0]["memory_used_mib"]["sum"] == str((1 << 54) + 3)
    assert output["series"][0]["memory_used_mib"]["mean_denominator"] == "1"
    assert output["series"][0]["memory_total_mib"] == str((1 << 55) + 1)
    assert output["series"][0]["points"][0]["seq"] == "1"
    assert output["series"][0]["points"][0]["memory_used_mib"] == str((1 << 54) + 3)
    assert "username" not in value.model_dump_json()
    _, _, other = inputs([{}])
    with pytest.raises(DomainError) as error:
        summarize_gpu_samples(preview, pages, other)
    assert error.value.code == "CONFLICT"
    with pytest.raises(DomainError):
        summarize_gpu_samples(preview, (), batches)
    with pytest.raises(DomainError):
        summarize_gpu_samples(preview, pages, batches * 9)


def test_combined_hardware_read_is_opt_in_and_keeps_original_preview_and_ledger_unchanged(
    preview_source,
):
    reader, source, actor, readings = preview_source
    with pytest.raises(DomainError) as error:
        reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=5)
    assert error.value.code == "USAGE_NOT_CONFIGURED"
    baseline = counts(readings[0])
    original = reader.recent(actor, SOURCE, RULE, max_messages=5)
    reader.hardware_source = VegaGpuHardwareObservations(source.sessions)
    combined = reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=5)
    assert combined.preview.fingerprint == original.fingerprint
    assert combined.hardware.telemetry_frames == 3 and combined.hardware.independent_frames == 3
    assert combined.hardware.series[0].temperature_c.samples == 0
    assert counts(readings[0]) == baseline


def test_postgresql_combined_hardware_uses_the_archive_snapshot_before_source_change(
    preview_source, monkeypatch
):
    reader, source, actor, _ = preview_source
    if source.sessions.kw["bind"].dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL repeatable hardware source snapshot")
    reader.hardware_source = VegaGpuHardwareObservations(source.sessions)
    expected = reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=5)
    original = reader.hardware_source.batch_in_transaction
    changed = False

    def change(session, query):
        nonlocal changed
        assert session.scalar(text("SHOW transaction_read_only")) == "on"
        assert session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        if not changed:
            changed = True
            with source.sessions.begin() as writer:
                writer.execute(
                    update(AgentMessage).where(AgentMessage.seq == 3).values(fingerprint="f" * 64)
                )
        return original(session, query)

    monkeypatch.setattr(reader.hardware_source, "batch_in_transaction", change)
    actual = reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=5)
    assert actual.hardware.fingerprint == expected.hardware.fingerprint
    with pytest.raises(DomainError) as error:
        reader.recent_with_hardware(actor, SOURCE, RULE, max_messages=5)
    assert error.value.code == "DEPENDENCY_UNAVAILABLE"


def test_optional_hardware_http_view_uses_exact_sample_statistics_and_current_identity(
    preview_source, settings, monkeypatch
):
    from fastapi.testclient import TestClient
    from sqlalchemy import delete

    from aris.core.access.models import ExternalIdentity
    from aris.interfaces.usage.activity_service import ActivityPreviewAccess
    from tests.test_activity_preview_ui import HEADERS, QUERY, interface

    reader, source, actor, _ = preview_source
    app, surface, _, _ = interface(preview_source, settings)
    reader.hardware_source = VegaGpuHardwareObservations(source.sessions)
    with TestClient(app) as client:
        assert (
            client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY).json()[
                "hardware"
            ]
            is None
        )
        surface.activity = ActivityPreviewAccess(
            reader, surface.activity.config.model_copy(update={"hardware_metrics": True})
        )
        response = client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY)
        assert response.status_code == 200
        report = response.json()
        hardware = report["hardware"]
        assert hardware["source_preview_fingerprint"] == report["source_result_fingerprint"]
        assert hardware["telemetry_frames"] == 3 and hardware["independent_frames"] == 3
        assert hardware["series"][0]["memory_total_mib"] == "49140"
        assert hardware["series"][0]["temperature_c"]["mean_numerator"] is None
        assert hardware["series"][0]["first_source"]["seq"] == "3"
        assert client.get("/internal/usage/ui/assets/usage-hardware.js").status_code == 200
        assert client.get("/internal/usage/ui/assets/usage-hardware-chart.js").status_code == 200
        assert len(hardware["series"][0]["points"]) == 3
        original = reader.recent_with_hardware

        def revoke(*args, **kwargs):
            value = original(*args, **kwargs)
            with source.sessions.begin() as db:
                db.execute(
                    delete(ExternalIdentity).where(ExternalIdentity.user_id == str(actor.actor_id))
                )
            return value

        monkeypatch.setattr(reader, "recent_with_hardware", revoke)
        response = client.post("/internal/usage/activity/query", headers=HEADERS, json=QUERY)
        assert response.status_code == 403 and "telemetry_frames" not in response.text


def test_chart_points_break_on_gaps_missing_gpu_and_returning_hardware_series():
    value = summary(
        [
            {},
            {"no_gpu": True},
            {},
            {},
            {"seconds": 120},
            {"seconds": 135, "model": "replacement", "total": 65536},
            {"seconds": 150},
        ]
    )
    original = next(series for series in value.series if series.model == "fixture GPU")
    assert [point.seq for point in original.points] == [1, 3, 4, 5, 7]
    assert [point.break_before for point in original.points] == [True, True, False, True, True]
    assert original.independent_samples == 5
