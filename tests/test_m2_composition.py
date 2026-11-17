import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import select, update

from aris.apps.api.main import create_app
from aris.apps.composition.m2 import M2RuntimeConfig, build_m2_components, load_m2_config
from aris.core.access.models import ExternalIdentity
from aris.core.notifications.models import NotificationOutbox
from aris.interfaces.commands.auth import UserTokenBinding
from aris.interfaces.slack.config import SlackSettings
from aris.modules.vega.models import Node
from tests.test_availability import RULES, calendar
from tests.test_command_intake import LIMITS as INTAKE_LIMITS
from tests.test_command_intake import NAMESPACE, WORKER_LIMITS, subject
from tests.test_event_relay import LIMITS as RELAY_LIMITS
from tests.test_notifications import LIMITS as NOTIFICATION_LIMITS
from tests.test_notifications import FixtureTransport
from tests.test_reservation_service import booking as booking
from tests.test_user_commands import ALICE_TOKEN, LIMITS, headers

KEY = SecretStr("fixture-independent-m2-confirmation-key-123456789")


def config(**changes):
    now = datetime.now(UTC)
    values = dict(
        namespace=NAMESPACE,
        commands=("reservation.create",),
        confirmation_key=KEY,
        confirmation_key_id="fixture-key",
        confirmation_ttl_seconds=300,
        gateway_limits=LIMITS,
        intake_limits=INTAKE_LIMITS,
        command_worker_limits=WORKER_LIMITS,
        relay_limits=RELAY_LIMITS,
        notification_limits=NOTIFICATION_LIMITS,
        rules=RULES,
        maintenance=(calendar(start=now),),
    )
    return M2RuntimeConfig(**{**values, **changes})


def private_file(tmp_path, cfg):
    data = cfg.model_dump(mode="json")
    data["confirmation_key"] = cfg.confirmation_key.get_secret_value()
    for index, binding in enumerate(cfg.user_tokens):
        data["user_tokens"][index]["token"] = binding.token.get_secret_value()
    path = tmp_path / "m2-private.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def slack():
    return SlackSettings(
        _env_file=None,
        bot_token="fixture-bot-token",
        signing_secret="fixture-signing-secret",
        team_id=NAMESPACE.namespace,
    )


def test_composed_api_workers_process_reservation_and_preserve_disabled_delivery(booking, settings):
    _, _, sessions, ids, _, _ = booking
    now = datetime.now(UTC)
    with sessions.begin() as session:
        session.add(
            ExternalIdentity(id=str(uuid4()), user_id=ids["alice"], **subject().model_dump())
        )
        session.execute(
            update(Node).values(
                observed_at=now,
                received_at=now,
                telemetry_at=now,
                telemetry_received_at=now,
                collector_health_at=now,
            )
        )
    cfg = config(user_tokens=(UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),))
    with TestClient(
        create_app(settings, slack(), m2_config=cfg), raise_server_exceptions=False
    ) as client:
        assert client.get("/health/ready").status_code == 200
        payload = {
            "command": "reservation.create",
            "parameters": {
                "gpu_uuid": "GPU-target",
                "start_at": (now + timedelta(hours=1)).isoformat(),
                "end_at": (now + timedelta(hours=2)).isoformat(),
                "purpose": "Private composed fixture",
            },
        }
        submitted = client.post("/internal/commands", headers=headers(), json=payload)
        assert submitted.status_code == 202, submitted.text
        pending = submitted.json()["outcome"]
        confirmed = client.post(
            f"/internal/commands/{pending['command_id']}/confirm",
            headers=headers(),
            json={"proof": pending["proof"]},
        )
        assert confirmed.status_code == 202
        graph = client.app.state.m2
        report = graph.run_once("all")
        assert report["commands"]["state"] == "succeeded", report
        assert (
            report["event_delivery"]["state"] == "processed"
            and not report["notification_delivery_enabled"]
        )
        with sessions() as session:
            assert session.scalar(select(NotificationOutbox.state)) == "queued"
        with pytest.raises(ValueError, match="disabled"):
            graph.run_once("notifications")
        assert KEY.get_secret_value() not in repr(
            report
        ) and "Private composed fixture" not in repr(report)
    transport = FixtureTransport(sessions)
    enabled = build_m2_components(
        sessions,
        settings,
        slack(),
        cfg.model_copy(update={"notification_delivery": True}),
        notification_transport=transport,
    )
    assert enabled.run_once("notifications")["notification"]["state"] == "delivered"
    assert len(transport.sent) == 1 and transport.sent[0].target == subject()


def test_default_and_explicit_but_no_ingress_config_do_not_mount_user_routes(booking, settings):
    cfg = config(user_tokens=(), slack_callbacks=False)
    with TestClient(create_app(settings, slack(), m2_config=cfg)) as client:
        assert client.app.state.m2 is not None
        assert client.app.state.m2.user_api is None and client.app.state.m2.slack_callbacks is None
        assert client.post("/internal/commands", json={}).status_code == 404
    with TestClient(create_app(settings, SlackSettings(_env_file=None))) as client:
        assert client.app.state.m2 is None


@pytest.mark.parametrize("missing", ["rules", "maintenance"])
def test_create_and_extend_require_explicit_rules_and_maintenance(missing):
    for name in ("reservation.create", "reservation.extend"):
        with pytest.raises(ValidationError):
            config(commands=(name,), **{missing: None if missing == "rules" else ()})
    reducing = config(
        commands=("reservation.cancel", "reservation.release"), rules=None, maintenance=()
    )
    assert reducing.rules is None and not reducing.maintenance


def test_private_config_roundtrip_is_explicit_and_hides_keys(tmp_path):
    cfg = config(user_tokens=(UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),))
    path = private_file(tmp_path, cfg)
    assert load_m2_config(path) == cfg
    assert KEY.get_secret_value() not in repr(cfg) and ALICE_TOKEN not in repr(cfg)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private regular file"):
        load_m2_config(path)
    path.chmod(0o600)
    link = tmp_path / "m2-link.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="private regular file"):
        load_m2_config(link)


@pytest.mark.parametrize("bad", ["unknown_command", "duplicate_command", "wrong_namespace"])
def test_composition_never_accepts_arbitrary_actions_or_cross_workspace_user_bindings(bad):
    changes = (
        {"commands": ("events.replay",)}
        if bad == "unknown_command"
        else (
            {"commands": ("reservation.create", "reservation.create")}
            if bad == "duplicate_command"
            else {
                "user_tokens": (
                    UserTokenBinding(
                        subject=subject().model_copy(update={"namespace": "T_OTHER"}),
                        token=SecretStr(ALICE_TOKEN),
                    ),
                )
            }
        )
    )
    with pytest.raises(ValidationError):
        config(**changes)


@pytest.mark.parametrize("reuse", ["confirmation", "user_token", "workspace", "production"])
def test_composition_refuses_conflicting_credentials_and_channels_before_activation(
    booking, settings, reuse
):
    _, _, sessions, _, _, _ = booking
    cfg = config()
    target = settings
    channel = slack()
    if reuse == "confirmation":
        cfg = cfg.model_copy(update={"confirmation_key": settings.read_token})
    elif reuse == "user_token":
        cfg = config(user_tokens=(UserTokenBinding(subject=subject(), token=KEY),))
    elif reuse == "workspace":
        cfg = config(notification_delivery=True)
        channel = channel.model_copy(update={"team_id": "T_OTHER"})
    else:
        cfg = config(
            user_tokens=(UserTokenBinding(subject=subject(), token=SecretStr(ALICE_TOKEN)),)
        )
        target = settings.model_copy(update={"environment": "production"})
    with pytest.raises(ValueError):
        build_m2_components(sessions, target, channel, cfg)


def test_explicit_m2_api_refuses_old_schema_instead_of_partially_enabling_features(
    booking, settings
):
    migration = Config("alembic.ini")
    command.downgrade(migration, "0012")
    try:
        with pytest.raises(RuntimeError, match="migration required"):
            create_app(settings, slack(), m2_config=config())
    finally:
        command.upgrade(migration, "head")


def test_worker_private_config_once_runs_without_external_io_or_secret_output(
    booking, settings, tmp_path
):
    cfg = config()
    path = private_file(tmp_path, cfg)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ARIS_", "PG"))}
    env.update(ARIS_DATABASE_URL=settings.database_url, ARIS_ENVIRONMENT="test")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "aris.apps.worker.main",
            "--m2-config",
            str(path),
            "--m2-mode",
            "commands",
            "--once",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"mode": "m2-commands", "commands": None}
    assert KEY.get_secret_value() not in result.stdout + result.stderr
    help_result = subprocess.run(
        [sys.executable, "-m", "aris.apps.api.main", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert help_result.returncode == 0 and "--m2-config" in help_result.stdout


def test_worker_incomplete_mode_arguments_fail_without_starting_services():
    result = subprocess.run(
        [sys.executable, "-m", "aris.apps.worker.main", "--m2-mode", "commands", "--once"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "must be provided together" in result.stderr
