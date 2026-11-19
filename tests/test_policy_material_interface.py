import json

from fastapi.testclient import TestClient
from sqlalchemy import delete, event

from aris.apps.api.main import create_app
from aris.apps.ops import policy_ui
from aris.core.access.models import ExternalIdentity, RoleBinding
from aris.interfaces.policy.materials import PolicyExampleResultView
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_material_repo import PolicyMaterialRepository
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from aris.modules.nova.policy_simulation import ExactGpuSeconds, simulate_policy_constraints
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, reference
from tests.test_policy_read_interface import TOKEN, policy_surface, seed
from tests.test_policy_review_material import counts, finish, materials, pending
from tests.test_policy_simulation import compiled, scenario

__all__ = ["command_state", "catalog_state", "policy_surface", "materials"]
HEADERS = {"Authorization": "Bearer " + TOKEN}


def test_optional_material_routes_page_metadata_and_bound_details_without_writes(
    policy_surface, materials, settings
):
    surface, state = policy_surface
    saved = [finish(materials, pending(materials, key=f"material-{index}")) for index in range(2)]
    surface.review_materials = PolicyReviewMaterialService(surface.catalog)
    source = surface.catalog.exact(actor(state[3]), reference())
    query = {
        "source": reference().model_dump(),
        "expected_source_fingerprint": source.fingerprint,
        "limit": 1,
    }
    baseline = counts(materials)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        assert client.post("/internal/policy/session", headers=HEADERS).json()[
            "review_materials_available"
        ]
        statements = []
        engine = state[2].kw["bind"]

        def observe(conn, cursor, sql, parameters, context, many):
            statements.append(sql)

        event.listen(engine, "before_cursor_execute", observe)
        try:
            first = client.post("/internal/policy/materials/query", headers=HEADERS, json=query)
            second = client.post(
                "/internal/policy/materials/query",
                headers=HEADERS,
                json={**query, "before": first.json()["next_cursor"]},
            )
        finally:
            event.remove(engine, "before_cursor_execute", observe)
        assert first.status_code == second.status_code == 200
        rows = first.json()["items"] + second.json()["items"]
        assert {row["id"] for row in rows} == {item.result["id"] for item in saved}
        assert not any("canonical_material" in sql for sql in statements)
        assert second.json()["next_cursor"] is None
        detail = {**query, "id": rows[0]["id"], "expected_fingerprint": rows[0]["fingerprint"]}
        detail.pop("limit")
        result = client.post("/internal/policy/materials/read", headers=HEADERS, json=detail)
        body = result.json()
        assert result.status_code == 200 and "no-store" in result.headers["cache-control"]
        assert body["review_material_only"] and body["basis"] == "caller_supplied_examples"
        assert not any(
            body[key]
            for key in (
                "approval_performed",
                "publication_performed",
                "current_dependencies_assessed",
            )
        )
        assert [item["decision"] for item in body["results"]] == ["NOT_EVALUATED", "WOULD_ALLOW"]
        assert body["results"][1]["assumed_used_gpu_seconds"] == {
            "numerator": "60",
            "denominator": "1",
        }
        for field in ("expected_source_fingerprint", "expected_fingerprint"):
            assert (
                client.post(
                    "/internal/policy/materials/read",
                    headers=HEADERS,
                    json={**detail, field: "f" * 64},
                ).status_code
                == 409
            )
        assert (
            client.post(
                "/internal/policy/materials/query?unexpected=yes", headers=HEADERS, json=query
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/internal/policy/materials/read",
                headers=HEADERS,
                json={**detail, "reverify": True},
            ).status_code
            == 422
        )
        assert client.post("/internal/commands", headers=HEADERS, json={}).status_code == 404
        assert client.get("/internal/policy/ui/assets/policy-materials.js").status_code == 200
    assert counts(materials) == baseline and surface.review_materials.implementation_ref is None


def test_material_selection_scope_and_current_identity_are_checked_before_return(
    policy_surface, materials, settings, monkeypatch
):
    surface, state = policy_surface
    saved = finish(materials, pending(materials))
    surface.review_materials = PolicyReviewMaterialService(surface.catalog)
    seed(state, 2, 2)
    source = surface.catalog.exact(actor(state[3]), reference())
    source2 = surface.catalog.exact(actor(state[3]), reference().model_copy(update={"version": 2}))
    detail = {
        "source": reference().model_dump(),
        "expected_source_fingerprint": source.fingerprint,
        "id": saved.result["id"],
        "expected_fingerprint": saved.result["fingerprint"],
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("wrong source or unauthorized full body queried")

    original = PolicyMaterialRepository.get
    monkeypatch.setattr(PolicyMaterialRepository, "get", forbidden)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), policy_sources=surface)
    ) as client:
        assert (
            client.post(
                "/internal/policy/materials/read",
                headers=HEADERS,
                json={**detail, "source": {"name": "outside-scope", "version": 1}},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/policy/materials/read",
                headers=HEADERS,
                json={
                    **detail,
                    "source": source2.source.metadata.model_dump(),
                    "expected_source_fingerprint": source2.fingerprint,
                },
            ).status_code
            == 404
        )
        monkeypatch.setattr(PolicyMaterialRepository, "get", original)
        read = surface.review_materials.read

        def revoke(*args, **kwargs):
            value = read(*args, **kwargs)
            with state[2].begin() as session:
                session.execute(
                    delete(ExternalIdentity).where(ExternalIdentity.user_id == state[3]["alice"])
                )
            return value

        monkeypatch.setattr(surface.review_materials, "read", revoke)
        assert (
            client.post("/internal/policy/materials/read", headers=HEADERS, json=detail).status_code
            == 403
        )
        monkeypatch.setattr(PolicyMaterialRepository, "get", forbidden)
        with state[2].begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == state[3]["alice"]))
        assert (
            client.post("/internal/policy/materials/read", headers=HEADERS, json=detail).status_code
            == 403
        )
    assert counts(materials)[0] == 1


def test_material_cli_opt_in_validation_is_strict_and_never_accesses_database(
    tmp_path, monkeypatch, capsys
):
    def forbidden(*args, **kwargs):
        raise AssertionError("pure configuration validation accessed database")

    monkeypatch.setattr(policy_ui, "build_engine", forbidden)
    path = tmp_path / "policy.json"
    raw = {
        "environment": "test",
        "database_url": "sqlite+pysqlite:///not-created.db",
        "read_token": "fixture-reserved-token-123456789123456",
        "user_tokens": [
            {
                "subject": {"provider": "slack", "namespace": "TFIXTURE", "external_id": "UALICE"},
                "token": TOKEN,
            }
        ],
        "policy_names": ["fixture-only"],
        "review_materials_read": True,
    }
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    assert policy_ui.main(["--config", str(path), "--validate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert (
        result["review_materials_read"]
        and not result["database_accessed"]
        and "draft_commands" not in result
    )
    raw["review_materials_read"] = "true"
    path.write_text(json.dumps(raw))
    assert policy_ui.main(["--config", str(path), "--validate"]) == 2
    assert "CONFIGURATION_ERROR" in capsys.readouterr().err


def test_material_result_projection_preserves_large_fraction_as_decimal_strings():
    value = simulate_policy_constraints(compiled(), scenario())
    exact = ExactGpuSeconds(numerator=2**61 + 1, denominator=999983)
    view = PolicyExampleResultView.of(value.model_copy(update={"assumed_used_gpu_seconds": exact}))
    assert view.model_dump(mode="json")["assumed_used_gpu_seconds"] == {
        "numerator": str(2**61 + 1),
        "denominator": "999983",
    }
