import json

import pytest
from pydantic import SecretStr

from aris.apps.ops import identity_ui
from aris.interfaces.commands.auth import UserTokenBinding
from tests.test_administration import NAMESPACE, subject
from tests.test_identity_bootstrap_cli import private
from tests.test_m2_composition import config

USER_TOKEN = "fixture-local-runtime-user-credential-123456789"


def files(tmp_path, **changes):
    settings = private(
        tmp_path / "settings.json",
        {
            "environment": "development",
            "database_url": "sqlite+pysqlite:///" + str(tmp_path / "uncreated-fixture.db"),
            "read_token": "fixture-local-runtime-read-credential-123456789",
            "agent_credentials": {},
        },
    )
    slack = private(
        tmp_path / "slack.json",
        {
            "team_id": NAMESPACE.namespace,
            "bot_token": "fixture-local-runtime-bot",
            "signing_secret": "fixture-local-runtime-signing",
        },
    )
    cfg = config(
        namespace=NAMESPACE,
        commands=("identity.map_users",),
        rules=None,
        maintenance=(),
        identity_admin=True,
        identity_nodes=("node-a",),
        user_tokens=(UserTokenBinding(subject=subject(), token=SecretStr(USER_TOKEN)),),
        **changes,
    )
    raw = cfg.model_dump(mode="json")
    raw["confirmation_key"] = cfg.confirmation_key.get_secret_value()
    raw["user_tokens"][0]["token"] = USER_TOKEN
    runtime = private(tmp_path / "runtime.json", raw)
    return settings, slack, runtime


def args(paths, *extra):
    return [
        "--settings",
        str(paths[0]),
        "--slack-settings",
        str(paths[1]),
        "--m2-config",
        str(paths[2]),
        *extra,
    ]


def test_local_validation_is_pure_and_hides_credentials(tmp_path, monkeypatch, capsys):
    paths = files(tmp_path)
    monkeypatch.setattr(
        identity_ui, "create_app", lambda *a, **kw: pytest.fail("pure validation built an app")
    )
    monkeypatch.setattr(
        identity_ui, "run_m2_worker", lambda *a, **kw: pytest.fail("pure validation ran a worker")
    )
    assert identity_ui.main(args(paths, "--validate")) == 0
    data = capsys.readouterr().out
    assert (
        json.loads(data)["host"] == "127.0.0.1" and json.loads(data)["database_accessed"] is False
    )
    assert USER_TOKEN not in data and "fixture-local-runtime-bot" not in data
    assert not (tmp_path / "uncreated-fixture.db").exists()


@pytest.mark.parametrize(
    "change",
    [
        "production",
        "collectors",
        "slack_callbacks",
        "notification_delivery",
        "reservation_home",
        "other_command",
    ],
)
def test_loopback_launcher_cannot_enable_production_dev_auth_or_other_workloads(
    tmp_path, monkeypatch, capsys, change
):
    paths = files(tmp_path)
    target = paths[0] if change in {"production", "collectors"} else paths[2]
    raw = json.loads(target.read_text())
    if change == "production":
        raw["environment"] = "production"
    elif change == "collectors":
        raw["agent_credentials"] = {
            "injected": {"node_id": "node-a", "token": "credential-for-other-endpoint"}
        }
    elif change == "other_command":
        raw["commands"].append("reservation.cancel")
    else:
        raw[change] = True
    private(target, raw)
    monkeypatch.setattr(
        identity_ui, "create_app", lambda *a, **kw: pytest.fail("unsafe profile built an app")
    )
    assert identity_ui.main(args(paths, "--validate")) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "CONFIGURATION_ERROR"


def test_settings_do_not_fall_back_to_process_or_dotenv_database_credentials(tmp_path, monkeypatch):
    path = files(tmp_path)[0]
    monkeypatch.setenv("ARIS_ENVIRONMENT", "production")
    monkeypatch.setenv("ARIS_DATABASE_URL", "postgresql+psycopg://must-not-use.invalid/other")
    settings = identity_ui.read_private(path, identity_ui.LocalIdentitySettings).settings()
    assert settings.environment == "development"
    assert settings.database_url == "sqlite+pysqlite:///" + str(tmp_path / "uncreated-fixture.db")
    assert settings.agent_credentials == {}


def test_server_host_is_fixed_loopback_and_worker_is_commands_only(tmp_path, monkeypatch):
    paths = files(tmp_path)
    database = tmp_path / "migrated-placeholder.db"
    database.touch()
    raw = json.loads(paths[0].read_text())
    raw["database_url"] = "sqlite+pysqlite:///" + str(database)
    private(paths[0], raw)
    calls = []
    app = object()
    monkeypatch.setattr(identity_ui, "create_app", lambda *a, **kw: app)
    monkeypatch.setattr(identity_ui.uvicorn, "run", lambda built, **kw: calls.append((built, kw)))
    assert identity_ui.main(args(paths, "--port", "53412")) == 0
    assert (
        calls[0][0] is app and calls[0][1]["host"] == "127.0.0.1" and calls[0][1]["port"] == 53412
    )
    worker = []
    monkeypatch.setattr(identity_ui, "run_m2_worker", lambda *a: worker.append(a))
    assert identity_ui.main(args(paths, "--worker", "--once")) == 0
    assert worker[0][-2:] == ("commands", True) and len(calls) == 1


@pytest.mark.parametrize(
    "extra", [("--once",), ("--host", "0.0.0.0"), ("--port", "80"), ("--port", "65536")]
)
def test_invalid_runtime_modes_or_public_host_are_not_accepted(tmp_path, extra):
    with pytest.raises(SystemExit):
        identity_ui.main(args(files(tmp_path), *extra))
