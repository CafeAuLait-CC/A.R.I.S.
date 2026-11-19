import json
from uuid import UUID

import pytest
from sqlalchemy import delete, func, select

from aris.apps.composition.policy import build_policy_draft_components
from aris.core.access.models import RoleBinding
from aris.core.commands.gateway_schemas import UserCommandInput
from aris.core.commands.models import CommandRecord
from aris.core.db import session_factory
from aris.interfaces.policy.materials import PolicyMaterialTextInput
from aris.interfaces.policy.service import PolicyReadSurface
from aris.modules.nova.models import PolicyReviewMaterialRow
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from aris.modules.nova.policy_material_schemas import RecordPolicyReviewMaterial
from aris.modules.nova.policy_material_service import PolicyReviewMaterialService
from tests.test_commands import command_state
from tests.test_policy_catalog import actor, catalog_state, reference
from tests.test_policy_draft_editor import client_for, draft_runtime, headers
from tests.test_policy_read_interface import TOKEN, policy_surface, seed
from tests.test_policy_simulation import scenario

__all__ = ["command_state", "catalog_state", "policy_surface"]


@pytest.fixture
def material_editor(policy_surface):
    read, state = policy_surface
    seed(state, 1, 2)
    config = draft_runtime(review_material_implementation_ref="fixture-save-a")
    graph = build_policy_draft_components(
        state[2], read.authenticator, read.policy_names, config, clock=read.catalog.clock
    )
    surface = PolicyReadSurface(
        read.catalog,
        read.authenticator,
        read.policy_names,
        draft_commands=graph.user_api,
        review_materials=PolicyReviewMaterialService(read.catalog),
    )
    return surface, graph, state


def parameters(editor):
    source = editor[0].catalog.exact(actor(editor[2][3]), reference())
    return RecordPolicyReviewMaterial(
        source=reference(),
        expected_source_fingerprint=source.fingerprint,
        expected_implementation_ref="fixture-save-a",
        scenarios=(scenario(include_usage=False),),
        reason="UI fixture save only",
    ).model_dump(mode="json")


def submit(client, value, key):
    return client.post(
        "/internal/commands",
        headers=headers(key),
        json={"command": "policy.record_review_material", "parameters": value},
    )


def count(editor):
    with editor[2][2]() as session:
        return session.scalar(select(func.count()).select_from(PolicyReviewMaterialRow))


def test_material_preview_and_confirmed_queue_preserve_original_receipt_across_runtime_change(
    material_editor, settings
):
    surface, graph, state = material_editor
    params = parameters(material_editor)
    params["scenarios"] = [
        scenario(include_usage=False, name="unknown-usage").model_dump(mode="json"),
        scenario(
            include_usage=False, name="concurrency-limit", projected_concurrent_gpus=100
        ).model_dump(mode="json"),
    ]
    with client_for(material_editor, settings) as client:
        info = client.post("/internal/policy/session", headers=headers()).json()
        assert info["review_material_implementation_ref"] == "fixture-save-a"
        with state[2]() as session:
            before = session.scalar(select(func.count()).select_from(CommandRecord))
        preview = client.post("/internal/policy/materials/validate", headers=headers(), json=params)
        assert preview.status_code == 200 and preview.json()["request"] == params
        assert preview.json()["results"][0]["decision"] == "NOT_EVALUATED"
        assert preview.json()["results"][1]["decision"] == "WOULD_DENY"
        with state[2]() as session:
            assert session.scalar(select(func.count()).select_from(CommandRecord)) == before
        pending = submit(client, params, "save-material").json()["outcome"]
        assert pending["state"] == "awaiting_confirmation" and count(material_editor) == 0
        path = f"/internal/commands/{pending['command_id']}"
        original = client.get(path + "/review", headers=headers()).json()
        assert original["request"]["parameters"] == params
        queued = client.post(
            path + "/confirm",
            headers=headers("confirm-material"),
            json={"proof": original["outcome"]["proof"]},
        )
        assert queued.json()["outcome"]["state"] == "queued" and count(material_editor) == 0
        assert graph.worker.run_once().state == "succeeded"
        stored = client.get(path + "/review", headers=headers()).json()["outcome"]
        assert stored["state"] == "succeeded" and stored["result"]["review_material_only"]
        assert count(material_editor) == 1
        receipt = stored["result"]
        material = surface.review_materials.read(actor(state[3]), UUID(receipt["id"]))
        assert material.request.model_dump(mode="json") == params
        assert material.results[0].assumed_used_gpu_seconds is None
        assert len(material.request.scenarios) == len(material.results) == 2
        assert [item.name for item in material.request.scenarios] == [
            "unknown-usage",
            "concurrency-limit",
        ]
        assert all(item.as_of == scenario().as_of for item in material.request.scenarios)
    changed = build_policy_draft_components(
        state[2],
        surface.authenticator,
        surface.policy_names,
        draft_runtime(review_material_implementation_ref="fixture-save-b"),
        clock=surface.catalog.clock,
    )
    replay = changed.user_api.submit(
        "Bearer " + TOKEN,
        "recovered-material",
        "save-material",
        UserCommandInput(command="policy.record_review_material", parameters=params),
    )
    assert (
        replay.outcome.state == "succeeded"
        and replay.outcome.result == receipt
        and count(material_editor) == 1
    )


def test_material_preview_scopes_default_off_withdraw_and_current_permissions(
    material_editor, settings
):
    surface, graph, state = material_editor
    params = parameters(material_editor)
    with client_for(material_editor, settings) as client:
        for change in (
            {"expected_implementation_ref": "changed"},
            {"expected_source_fingerprint": "f" * 64},
        ):
            assert (
                client.post(
                    "/internal/policy/materials/validate",
                    headers=headers(),
                    json={**params, **change},
                ).status_code
                == 409
            )
        foreign = {**params, "source": {"name": "outside-policy", "version": 1}}
        assert submit(client, foreign, "outside-material").status_code == 403
        assert (
            client.post(
                "/internal/policy/materials/validate", headers=headers(), json=foreign
            ).status_code
            == 403
        )
        pending = submit(client, params, "withdraw-material").json()["outcome"]
        path = f"/internal/commands/{pending['command_id']}"
        assert (
            client.post(path + "/withdraw", headers=headers("withdraw-original"), json={}).json()[
                "outcome"
            ]["state"]
            == "cancelled"
        )
        assert graph.worker.run_once() is None and count(material_editor) == 0
        pending = submit(client, params, "revoke-material").json()["outcome"]
        with state[2].begin() as session:
            session.execute(delete(RoleBinding).where(RoleBinding.user_id == state[3]["alice"]))
        assert (
            client.post(
                f"/internal/commands/{pending['command_id']}/confirm",
                headers=headers("revoked-original"),
                json={"proof": pending["proof"]},
            ).status_code
            == 403
        )
    defaults = build_policy_draft_components(
        state[2],
        surface.authenticator,
        surface.policy_names,
        draft_runtime(),
        clock=surface.catalog.clock,
    )
    assert defaults.user_api.gateway.allowed == frozenset({"policy.record_draft"})


def test_postgresql_material_preview_accepts_read_only_session(material_editor):
    surface, graph, state = material_editor
    engine = state[2].kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY contract")
    readonly = session_factory(engine.execution_options(postgresql_readonly=True))
    catalog = PolicyCatalogService(readonly)
    view = PolicyReadSurface(
        catalog,
        surface.authenticator,
        surface.policy_names,
        draft_commands=graph.user_api,
        review_materials=PolicyReviewMaterialService(catalog),
    )
    result = view.materials_validate(
        "Bearer " + TOKEN,
        "readonly-material",
        RecordPolicyReviewMaterial.model_validate(parameters(material_editor)),
    )
    assert (
        result.review_material_only
        and not result.approval_performed
        and count(material_editor) == 0
    )
    parameters_text = parameters(material_editor)
    parameters_text["scenarios_text"] = json.dumps(parameters_text.pop("scenarios"))
    replay = view.materials_validate_text(
        "Bearer " + TOKEN,
        "readonly-material-text",
        PolicyMaterialTextInput.model_validate(parameters_text),
    )
    assert replay == result


def test_complete_example_text_import_preserves_inputs_and_rejects_ambiguous_json(
    material_editor, settings
):
    surface, graph, state = material_editor
    params = parameters(material_editor)
    params["scenarios"] = [
        scenario(name="exact-full-case", used_microseconds=(1 << 44) + 1).model_dump(mode="json"),
        scenario(name="unknown-full-case", include_usage=False).model_dump(mode="json"),
    ]
    query = {key: value for key, value in params.items() if key != "scenarios"}
    raw = json.dumps(params["scenarios"])
    query["scenarios_text"] = raw
    path = "/internal/policy/materials/validate-text"
    readonly_surface = PolicyReadSurface(
        surface.catalog,
        surface.authenticator,
        surface.policy_names,
        review_materials=surface.review_materials,
    )
    with client_for((readonly_surface, graph, state), settings) as readonly_client:
        assert readonly_client.post(path, headers=headers(), json=query).status_code == 404
    with state[2]() as session:
        before = session.scalar(select(func.count()).select_from(CommandRecord))
    with client_for(material_editor, settings) as client:
        imported = client.post(path, headers=headers(), json=query)
        assert imported.status_code == 200 and imported.headers["cache-control"] == "no-store"
        value = imported.json()
        assert value["request"] == params and count(material_editor) == 0
        assert value["results"][0]["assumed_used_gpu_seconds"] == {
            "numerator": str((1 << 44) + 1),
            "denominator": "1000000",
        }
        assert value["results"][1]["assumed_used_gpu_seconds"] is None
        malformed = (
            raw[:-1],
            "{}",
            "[]",
            json.dumps([params["scenarios"][0]] * 2),
            json.dumps([{**params["scenarios"][0], "private-field": "private-marker"}]),
            raw.replace('"projected_concurrent_gpus": 1', '"projected_concurrent_gpus": 1.0'),
            raw.replace('"projected_concurrent_gpus": 1', '"projected_concurrent_gpus": NaN'),
            raw.replace('"projected_concurrent_gpus": 1', '"projected_concurrent_gpus": true'),
            raw.replace('"name": "exact-full-case"', '"name":"private-marker","name":"other"'),
            raw.replace('"quality": "complete"', '"quality":"unknown","quality":"complete"'),
            json.dumps([{**params["scenarios"][0], "name": "case-" + str(i)} for i in range(9)]),
            "[" * 1500 + "]" * 1500,
            raw + " " * 16000,
            raw + "　" * 6000,
        )
        for text in malformed:
            result = client.post(path, headers=headers(), json={**query, "scenarios_text": text})
            assert result.status_code == 422 and "private-marker" not in result.text
        for field in ("expected_source_fingerprint", "expected_implementation_ref"):
            changed = {**query, field: "f" * 64 if field.endswith("fingerprint") else "changed"}
            assert client.post(path, headers=headers(), json=changed).status_code == 409
        assert (
            client.post(
                path,
                headers=headers(),
                json={**query, "source": {"name": "outside-policy", "version": 1}},
            ).status_code
            == 403
        )
        assert client.post(path + "?extra=1", headers=headers(), json=query).status_code == 422
        assert client.post(path, json=query).status_code == 401
        with state[2]() as session:
            assert session.scalar(select(func.count()).select_from(CommandRecord)) == before
        assert count(material_editor) == 0
        with state[2].begin() as session:
            session.execute(delete(RoleBinding))
        denied = client.post(path, headers=headers(), json=query)
        assert denied.status_code == 403 and "exact-full-case" not in denied.text
