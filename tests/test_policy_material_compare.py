from fractions import Fraction
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, text

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity
from aris.core.contracts import DomainError
from aris.core.db import session_factory
from aris.interfaces.policy.materials import PolicyMaterialComparisonInput
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, confirmed, reference
from tests.test_policy_read_interface import TOKEN, policy_surface
from tests.test_policy_review_material import counts, finish, materials, pending, request
from tests.test_policy_simulation import scenario

__all__ = ["command_state", "catalog_state", "policy_surface", "materials"]
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
def material_comparison(policy_surface, materials):
    base, state = policy_surface
    target_source = base.catalog.exact(actor(state[3]), reference()).source.model_dump(mode="json")
    target_source["metadata"]["version"] = 2
    target_source["spec"]["reservation"]["max_concurrent_gpus"] = 1
    target_source["spec"]["gpu_time_weights"]["default_weight"] = {
        "numerator": 997,
        "denominator": 991,
    }
    confirmed(state, key="stored-replay-target", source=target_source)
    proposed = request(
        materials,
        scenarios=(
            scenario(name='<script>alert("case")</script>', projected_concurrent_gpus=2),
            scenario(name="large-usage", used_microseconds=(1 << 44) + 1),
        ),
    )
    receipt = finish(materials, pending(materials, parameters=proposed))
    source = base.catalog.exact(actor(state[3]), reference())
    target = reference().model_copy(update={"version": 2})
    target_value = base.catalog.exact(actor(state[3]), target)
    reader = PolicyReviewMaterialService(base.catalog)
    surface = PolicyReadSurface(
        base.catalog,
        base.authenticator,
        base.policy_names,
        review_materials=reader,
        material_comparison_implementation_ref="fixture-replay-b",
    )
    query = PolicyMaterialComparisonInput(
        source=reference(),
        expected_source_fingerprint=source.fingerprint,
        id=UUID(receipt.result["id"]),
        expected_fingerprint=receipt.result["fingerprint"],
        target=target,
        expected_target_fingerprint=target_value.fingerprint,
        expected_implementation_ref="fixture-replay-b",
    )
    return surface, query, materials, base


def client_for(surface, settings):
    return TestClient(create_app(settings, SlackSettings(_env_file=None), policy_sources=surface))


def test_stored_case_replay_is_opt_in_exact_and_read_only(material_comparison, settings):
    surface, query, materials, base = material_comparison
    with client_for(base, settings) as client:
        assert (
            client.post(
                "/internal/policy/materials/compare",
                headers=HEADERS,
                json=query.model_dump(mode="json"),
            ).status_code
            == 404
        )
        assert client.post("/internal/policy/materials/report", headers=HEADERS).status_code == 404
    baseline = counts(materials)
    with client_for(surface, settings) as client:
        assert (
            client.post("/internal/policy/session", headers=HEADERS).json()[
                "material_comparison_implementation_ref"
            ]
            == "fixture-replay-b"
        )
        response = client.post(
            "/internal/policy/materials/compare",
            headers=HEADERS,
            json=query.model_dump(mode="json"),
        )
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        value = response.json()
        assert value["comparison_only"] and value["basis"] == "stored_caller_supplied_examples"
        assert (
            not value["current_dependencies_assessed"]
            and not value["approval_performed"]
            and not value["publication_performed"]
        )
        assert value["material"]["implementation_ref"] == "fixture-constraints-a"
        assert value["comparison_implementation_ref"] == "fixture-replay-b"
        first = value["cases"][0]
        assert (
            first["stored_replay_matches"] and first["recorded_result"]["decision"] == "WOULD_ALLOW"
        )
        assert (
            first["before_result"]["decision"] == "WOULD_ALLOW"
            and first["after_result"]["decision"] == "WOULD_DENY"
        )
        assert first["decision_changed"] and "reservation.concurrent" in first["changed_rules"]
        assert all(case["stored_replay_matches"] for case in value["cases"])
        assert first["scenario"]["as_of"] == scenario().model_dump(mode="json")["as_of"]
        assert str((1 << 44) + 1) in value["cases"][1]["raw_scenario"]
        assert value["cases"][1]["before_result"]["assumed_used_gpu_seconds"] == {
            "numerator": str((1 << 44) + 1),
            "denominator": "1000000",
        }
        expected = Fraction(((1 << 44) + 1) * 997, 1000000 * 991)
        assert expected.numerator > (1 << 53)
        assert value["cases"][1]["after_result"]["assumed_used_gpu_seconds"] == {
            "numerator": str(expected.numerator),
            "denominator": str(expected.denominator),
        }
        detail = client.post(
            "/internal/policy/materials/read",
            headers=HEADERS,
            json=query.model_dump(
                mode="json",
                exclude={"target", "expected_target_fingerprint", "expected_implementation_ref"},
            ),
        ).json()
        assert str((1 << 44) + 1) in detail["raw_scenarios"][1]
        report = client.post(
            "/internal/policy/materials/report", headers=HEADERS, json=query.model_dump(mode="json")
        )
        assert report.status_code == 200 and report.headers["cache-control"] == "no-store"
        assert report.headers["content-type"].startswith("text/html")
        assert report.headers["content-disposition"] == (
            'attachment; filename="aris-policy-case-comparison.html"'
        )
        assert "default-src 'none'" in report.headers["content-security-policy"]
        assert "script-src" not in report.headers["content-security-policy"]
        assert "&lt;script&gt;alert(&quot;" in report.text and "<script>" not in report.text
        assert "<script" not in report.text and "https://" not in report.text
        assert str(expected.numerator) in report.text and str(expected.denominator) in report.text
        assert TOKEN not in report.text and "Bearer " not in report.text
        assert value["material"]["fingerprint"] in report.text
        assert "fixture-constraints-a" in report.text and "fixture-replay-b" in report.text
        assert "保存结果重演一致 2/2" in report.text and "目标决策变化 1 个" in report.text
        assert "未评估当前真实依赖，未批准或发布" in report.text
        assert (
            client.post(
                "/internal/policy/materials/report?extra=1",
                headers=HEADERS,
                json=query.model_dump(mode="json"),
            ).status_code
            == 422
        )
    assert counts(materials) == baseline


def test_stored_case_replay_rejects_substituted_sources_and_identity_change(
    material_comparison, settings, monkeypatch
):
    surface, query, materials, _ = material_comparison
    value = query.model_dump(mode="json")
    with client_for(surface, settings) as client:
        for field in (
            "expected_source_fingerprint",
            "expected_target_fingerprint",
            "expected_fingerprint",
            "expected_implementation_ref",
        ):
            changed = {
                **value,
                field: "f" * 64 if field != "expected_implementation_ref" else "another-runtime",
            }
            assert (
                client.post(
                    "/internal/policy/materials/compare", headers=HEADERS, json=changed
                ).status_code
                == 409
            )
        assert (
            client.post(
                "/internal/policy/materials/compare",
                headers=HEADERS,
                json={**value, "scenarios": []},
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/policy/materials/compare",
                headers=HEADERS,
                json={
                    **value,
                    "source": {"name": "outside-policy", "version": 1},
                    "target": {"name": "outside-policy", "version": 2},
                },
            ).status_code
            == 403
        )
        original = surface.catalog.compare_sources_in_transaction

        def reconfigure(*args, **kwargs):
            result = original(*args, **kwargs)
            surface.material_comparison_implementation_ref = "changed-during-read"
            return result

        with monkeypatch.context() as patch:
            patch.setattr(surface.catalog, "compare_sources_in_transaction", reconfigure)
            assert (
                client.post(
                    "/internal/policy/materials/compare", headers=HEADERS, json=value
                ).status_code
                == 409
            )
        surface.material_comparison_implementation_ref = "fixture-replay-b"

        def revoke(*args, **kwargs):
            result = original(*args, **kwargs)
            with surface.catalog.sessions.begin() as session:
                session.execute(delete(ExternalIdentity))
            return result

        monkeypatch.setattr(surface.catalog, "compare_sources_in_transaction", revoke)
        response = client.post("/internal/policy/materials/compare", headers=HEADERS, json=value)
        assert response.status_code == 403 and "raw_scenario" not in response.text
        report = client.post("/internal/policy/materials/report", headers=HEADERS, json=value)
        assert report.status_code == 403 and "<html" not in report.text


def test_postgresql_stored_case_replay_read_only_and_lock_recovery(material_comparison):
    surface, query, materials, _ = material_comparison
    engine = surface.catalog.sessions.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL read-only and bounded material comparison lock")
    readonly = session_factory(engine.execution_options(postgresql_readonly=True))
    catalog = PolicyCatalogService(readonly)
    view = PolicyReadSurface(
        catalog,
        surface.authenticator,
        surface.policy_names,
        review_materials=PolicyReviewMaterialService(catalog),
        material_comparison_implementation_ref="fixture-replay-b",
    )
    before = counts(materials)
    first = view.materials_compare("Bearer " + TOKEN, "readonly-stored-replay", query)
    assert len(first.cases) == 2 and counts(materials) == before
    with surface.catalog.sessions() as blocker:
        blocker.execute(text("LOCK TABLE policy_versions IN ACCESS EXCLUSIVE MODE"))
        with pytest.raises(DomainError) as error:
            view.materials_compare("Bearer " + TOKEN, "locked-stored-replay", query)
        assert error.value.code in {"DEPENDENCY_UNAVAILABLE", "RETRYABLE_DATABASE_ERROR"}
        blocker.rollback()
    assert view.materials_compare("Bearer " + TOKEN, "recovered-stored-replay", query) == first
    assert counts(materials) == before
