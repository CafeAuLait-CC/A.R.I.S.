import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from aris.apps.api.main import create_app
from aris.apps.composition.policy import (
    PolicyDraftRuntimeConfig,
    build_policy_draft_components,
    validate_policy_draft_configuration,
)
from aris.core.access.models import RoleBinding
from aris.core.commands.gateway_schemas import ExternalCommandSubmit, UserCommandInput
from aris.core.commands.models import CommandRecord
from aris.core.db import session_factory
from aris.interfaces.policy.drafts import PolicyDraftInput
from aris.interfaces.policy.service import PolicyReadSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.policy_catalog_service import PolicyCatalogService
from tests.test_commands import command_state
from tests.test_policy_catalog import KEY, catalog_state, source_counts
from tests.test_policy_read_interface import TOKEN, policy_surface, seed
from tests.test_policy_source import policy

__all__ = ["command_state", "catalog_state", "policy_surface"]


def draft_runtime(**changes):
    return PolicyDraftRuntimeConfig.model_validate(
        {
            "namespace": {"provider": "slack", "namespace": "TFIXTURE"},
            "confirmation_key": KEY.get_secret_value(),
            "confirmation_key_id": "fixture-policy-drafts",
            "confirmation_ttl_seconds": 300,
            "gateway_limits": {
                "lock_timeout_ms": 50,
                "statement_timeout_ms": 150,
                "transaction_timeout_ms": 240,
            },
            "intake_limits": {
                "lock_timeout_ms": 100,
                "statement_timeout_ms": 500,
                "transaction_timeout_ms": 1000,
                "max_queue_delay_seconds": 120,
                "max_pending_commands": 20,
            },
            "worker_limits": {
                "lock_timeout_ms": 100,
                "statement_timeout_ms": 500,
                "transaction_timeout_ms": 2000,
                "lease_seconds": 5,
                "max_attempts": 3,
                "retry_base_seconds": 1,
            },
            **changes,
        }
    )


@pytest.fixture
def draft_editor(policy_surface, settings):
    read, state = policy_surface
    config = draft_runtime()
    validate_policy_draft_configuration(config, read.authenticator, settings.read_token)
    graph = build_policy_draft_components(
        state[2], read.authenticator, read.policy_names, config, clock=lambda: state[4]["now"]
    )
    surface = PolicyReadSurface(
        read.catalog, read.authenticator, read.policy_names, draft_commands=graph.user_api
    )
    return surface, graph, state


def client_for(editor, settings):
    surface, graph, _ = editor
    return TestClient(
        create_app(
            settings,
            SlackSettings(_env_file=None),
            policy_sources=surface,
            user_commands=graph.user_api,
        )
    )


def new_source(version=2):
    raw = policy()
    raw["metadata"]["version"] = version
    return raw


def headers(key=None):
    return {"Authorization": "Bearer " + TOKEN, **({"Idempotency-Key": key} if key else {})}


def submit(client, source, key):
    return client.post(
        "/internal/commands",
        headers=headers(key),
        json={
            "command": "policy.record_draft",
            "parameters": {"source": source, "reason": "Editor fixture draft"},
        },
    )


def test_editor_validation_is_read_only_and_binds_strict_text_base_version_and_inline_budget(
    draft_editor, settings
):
    surface, _, state = draft_editor
    seed(state, 1, 1)
    before = source_counts(state[2])
    with state[2]() as db:
        commands_before = db.scalar(select(func.count()).select_from(CommandRecord))
    with client_for(draft_editor, settings) as client:
        assert client.post("/internal/policy/session", headers=headers()).json()[
            "draft_editing_available"
        ]
        base = client.post(
            "/internal/policy/sources", headers=headers(), json={"name": "fixture-only"}
        ).json()["versions"][0]
        body = {
            "source_text": json.dumps(new_source()),
            "reason": "  Editor fixture draft  ",
            "base": base["reference"],
            "expected_base_fingerprint": base["fingerprint"],
        }
        preview = client.post("/internal/policy/drafts/validate", headers=headers(), json=body)
        assert preview.status_code == 200
        data = preview.json()
        assert (
            data["validated_draft_only"]
            and not data["publication_performed"]
            and not data["activation_performed"]
        )
        assert data["draft"]["reason"] == "Editor fixture draft" and data["inline_bytes"] < 16384
        assert not data["existing_equivalent"] and data["difference"]["changes"]
        large = new_source()
        large["spec"]["gpu_time_weights"]["model_weights"] = [
            {"model": f"{index:02d}-" + "x" * 200, "weight": {"numerator": 1, "denominator": 1}}
            for index in range(64)
        ]
        oversized = client.post(
            "/internal/policy/drafts/validate",
            headers=headers(),
            json={**body, "source_text": json.dumps(large)},
        )
        assert oversized.status_code == 422
        wrong = {**body, "expected_base_fingerprint": "0" * 64}
        assert (
            client.post(
                "/internal/policy/drafts/validate", headers=headers(), json=wrong
            ).status_code
            == 409
        )
        duplicate = body["source_text"].replace('"spec":', '"spec": {}, "spec":')
        rejected = client.post(
            "/internal/policy/drafts/validate",
            headers=headers(),
            json={**body, "source_text": duplicate},
        )
        assert rejected.status_code == 422 and duplicate not in rejected.text
        assert (
            client.post(
                "/internal/policy/drafts/validate",
                headers=headers(),
                json={**body, "source_text": json.dumps(new_source(1))},
            ).status_code
            == 409
        )
        existing = policy()
        existing["spec"]["reservation"]["max_concurrent_gpus"] = 1
        equivalent = client.post(
            "/internal/policy/drafts/validate",
            headers=headers(),
            json={**body, "source_text": json.dumps(existing)},
        )
        assert equivalent.status_code == 200 and equivalent.json()["existing_equivalent"]
    assert source_counts(state[2]) == before
    with state[2]() as db:
        assert db.scalar(select(func.count()).select_from(CommandRecord)) == commands_before


def test_confirmed_editor_intent_is_queued_then_worker_stores_once_and_withdraw_does_not_store(
    draft_editor, settings
):
    _, graph, state = draft_editor
    seed(state, 1, 1)
    before = source_counts(state[2])
    with pytest.raises(ValueError, match="same authenticated command surface"):
        create_app(settings, SlackSettings(_env_file=None), policy_sources=draft_editor[0])
    with client_for(draft_editor, settings) as client:
        pending = submit(client, new_source(), "editor-save")
        assert pending.status_code == 202
        outcome = pending.json()["outcome"]
        command = outcome["command_id"]
        assert outcome["state"] == "awaiting_confirmation" and source_counts(state[2]) == before
        review = client.get(f"/internal/commands/{command}/review", headers=headers()).json()
        assert review["request"]["parameters"]["source"]["metadata"]["version"] == 2
        assert review["request"]["parameters"]["reason"] == "Editor fixture draft"
        queued = client.post(
            f"/internal/commands/{command}/confirm",
            headers=headers("editor-confirm"),
            json={"proof": review["outcome"]["proof"]},
        )
        assert queued.status_code == 202 and queued.json()["outcome"]["state"] == "queued"
        assert source_counts(state[2]) == before
        finished = graph.worker.run_once()
        assert finished is not None and finished.state == "succeeded"
        saved = client.get(f"/internal/commands/{command}", headers=headers()).json()
        assert (
            saved["state"] == "succeeded"
            and saved["result"]["catalog_entry_only"]
            and not saved["result"]["publication_performed"]
        )
        replay = submit(client, new_source(), "editor-save")
        assert replay.json()["duplicate"] and replay.json()["outcome"]["command_id"] == command
        pending = submit(client, new_source(3), "editor-withdraw").json()["outcome"]
        withdrawn = client.post(
            f"/internal/commands/{pending['command_id']}/withdraw",
            headers=headers("editor-withdraw-confirm"),
            json={},
        )
        assert withdrawn.status_code == 200 and withdrawn.json()["outcome"]["state"] == "cancelled"
        assert graph.worker.run_once() is None
    assert source_counts(state[2]) == tuple(value + 1 for value in before)


def test_editor_name_boundary_covers_submit_review_and_confirmation_and_current_rbac(
    draft_editor, settings
):
    surface, graph, state = draft_editor
    foreign = new_source()
    foreign["metadata"]["name"] = "outside-policy"
    with client_for(draft_editor, settings) as client:
        assert submit(client, foreign, "not-exposed").status_code == 403
        assert (
            client.post(
                "/internal/policy/drafts/validate",
                headers=headers(),
                json={"source_text": json.dumps(foreign), "reason": "Other policy fixture"},
            ).status_code
            == 403
        )
        # A different trusted interface may admit this actor's global draft command.
        subject = surface.authenticator.authenticate("Bearer " + TOKEN)
        other = graph.user_api.gateway.submit(
            ExternalCommandSubmit(
                subject=subject,
                request_id="other-interface",
                receipt_key="other-interface-save",
                idempotency_key="other-interface-save",
            ),
            UserCommandInput(
                command="policy.record_draft",
                parameters={"source": foreign, "reason": "Other interface fixture"},
            ),
        ).outcome
        assert other is not None and other.state == "awaiting_confirmation"
        assert (
            client.get(
                f"/internal/commands/{other.command_id}/review", headers=headers()
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/internal/commands/{other.command_id}/confirm",
                headers=headers("not-exposed-confirm"),
                json={"proof": other.proof.get_secret_value()},
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/internal/commands/{other.command_id}/withdraw",
                headers=headers("not-exposed-withdraw"),
                json={},
            ).status_code
            == 403
        )
        pending = submit(client, new_source(), "current-rbac").json()["outcome"]
        with state[2].begin() as db:
            db.execute(delete(RoleBinding).where(RoleBinding.user_id == state[3]["alice"]))
        assert (
            client.post(
                f"/internal/commands/{pending['command_id']}/confirm",
                headers=headers("revoked-confirm"),
                json={"proof": pending["proof"]},
            ).status_code
            == 403
        )
    assert source_counts(state[2]) == (0, 0, 0)


def test_draft_preview_uses_postgresql_read_only_transactions(draft_editor, settings):
    surface, graph, state = draft_editor
    engine = state[2].kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("requires PostgreSQL READ ONLY contract")
    readonly = session_factory(engine.execution_options(postgresql_readonly=True))
    view = PolicyReadSurface(
        PolicyCatalogService(readonly),
        surface.authenticator,
        surface.policy_names,
        draft_commands=graph.user_api,
    )
    result = view.validate_draft(
        "Bearer " + TOKEN,
        "readonly-editor",
        PolicyDraftInput(source_text=json.dumps(new_source()), reason="Read-only fixture"),
    )
    assert result.validated_draft_only and source_counts(state[2]) == (0, 0, 0)


def test_editor_runtime_validation_is_pure_and_checks_workspace_keys_and_explicit_worker(
    tmp_path, monkeypatch, capsys
):
    from aris.apps.ops import policy_ui
    from tests.test_usage_ui_runtime import private_config

    path, database = private_config(tmp_path)
    config = json.loads(path.read_text())
    del config["node_ids"]
    config["policy_names"] = ["fixture-policy"]
    draft = draft_runtime().model_dump(mode="json")
    draft["confirmation_key"] = KEY.get_secret_value()
    config["draft_commands"] = draft
    path.write_text(json.dumps(config))

    def forbidden(*args, **kwargs):
        raise AssertionError("configuration validation cannot open a database")

    monkeypatch.setattr(policy_ui, "build_engine", forbidden)
    assert policy_ui.main(["--config", str(path), "--worker", "--once", "--validate"]) == 0
    checked = json.loads(capsys.readouterr().out)
    assert not checked["database_accessed"] and checked["draft_commands"] == ["policy.record_draft"]
    for change in ("workspace", "key"):
        bad = json.loads(json.dumps(config))
        if change == "workspace":
            bad["draft_commands"]["namespace"]["namespace"] = "TOTHER"
        else:
            bad["draft_commands"]["confirmation_key"] = config["user_tokens"][0]["token"]
        path.write_text(json.dumps(bad))
        assert policy_ui.main(["--config", str(path), "--validate"]) == 2
        output = capsys.readouterr().err
        assert "CONFIGURATION_ERROR" in output and KEY.get_secret_value() not in output
    del config["draft_commands"]
    path.write_text(json.dumps(config))
    assert policy_ui.main(["--config", str(path), "--worker", "--once"]) == 2
    assert "CONFIGURATION_ERROR" in capsys.readouterr().err
    assert not database.exists()
