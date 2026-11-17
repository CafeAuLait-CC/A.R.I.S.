from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import sessionmaker

from aris.apps.api.main import create_app
from aris.apps.composition.m2 import build_m2_components
from aris.core.access.admin_repo import AccessAdministrationRepository
from aris.core.access.models import (
    AccessDirectoryState,
    ExternalIdentity,
    NodeUserMapping,
    RoleBinding,
    User,
)
from aris.core.commands.models import CommandRecord
from aris.interfaces.commands.auth import UserTokenBinding
from aris.interfaces.identity.slack_directory import SlackRosterMember, SlackRosterPage
from aris.interfaces.slack.config import SlackSettings
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_administration import NAMESPACE, NOW, initialized, register, subject
from tests.test_administration import admin_state as admin_state
from tests.test_m2_composition import config

ADMIN_TOKEN = "fixture-identity-admin-user-token-123456789"
MEMBER_TOKEN = "fixture-identity-member-user-token-123456789"
DIRECTORY = "/internal/identity/directory/query"
ROSTER = "/internal/identity/slack-members/query"


def headers(token=ADMIN_TOKEN, key="fixture-mapping-ui"):
    return {"Authorization": "Bearer " + token, "Idempotency-Key": key}


class FixtureDirectory:
    def __init__(self, sessions):
        self.sessions, self.calls, self.side_effect = sessions, [], None
        self.team_id = NAMESPACE.namespace

    def page(self, query):
        # External I/O starts only after the authorization session has closed.
        pool = self.sessions.kw["bind"].pool
        if hasattr(pool, "checkedout"):
            assert pool.checkedout() == 0
        with self.sessions.begin() as session:
            session.execute(select(AccessDirectoryState).with_for_update(nowait=True)).all()
        self.calls.append(query)
        if self.side_effect:
            self.side_effect()
        return SlackRosterPage(
            team_id=self.team_id,
            fetched_at=NOW,
            members=(
                SlackRosterMember(
                    external_id="UADMIN",
                    display_name="Directory operator",
                    timezone="UTC",
                    deleted=False,
                    is_bot=False,
                ),
                SlackRosterMember(
                    external_id="UMEMBER",
                    display_name="Morgan <unsafe>",
                    timezone="Europe/London",
                    deleted=False,
                    is_bot=False,
                ),
            ),
            next_cursor="fixture-next-cursor",
        )


@pytest.fixture
def identity_api(admin_state, settings):
    _, sessions, _ = admin_state
    admin = initialized(admin_state)
    slack = SlackSettings(
        _env_file=None,
        bot_token="fixture-directory-bot",
        signing_secret="fixture-signing",
        team_id=NAMESPACE.namespace,
    )
    bindings = tuple(
        UserTokenBinding(subject=subject(member), token=SecretStr(token))
        for member, token in (("UADMIN", ADMIN_TOKEN), ("UMEMBER", MEMBER_TOKEN))
    )
    cfg = config(
        namespace=NAMESPACE,
        commands=("identity.map_users",),
        rules=None,
        maintenance=(),
        user_tokens=bindings,
        identity_admin=True,
        identity_nodes=("node-a", "node-b"),
    )
    provider = FixtureDirectory(sessions)
    graph = build_m2_components(
        sessions, settings, slack, cfg, clock=lambda: NOW, identity_directory=provider
    )
    app = create_app(
        settings, slack, user_commands=graph.user_api, identity_admin=graph.identity_admin
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, graph, provider, admin_state, admin


def count(sessions, model):
    with sessions() as session:
        return session.scalar(select(func.count()).select_from(model))


def test_identity_ui_is_available_only_with_explicit_module_and_has_private_static_assets(
    identity_api, client
):
    assert client.get("/internal/identity/ui").status_code == 404
    app_client = identity_api[0]
    for path in ("/ui", "/ui/assets/admin.css", "/ui/assets/admin.js"):
        response = app_client.get("/internal/identity" + path)
        assert response.status_code == 200 and response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert "Referrer-Policy" in response.headers and ADMIN_TOKEN not in response.text
    script = app_client.get("/internal/identity/ui/assets/admin.js").text
    assert all(
        unsafe not in script for unsafe in ("localStorage", "sessionStorage", ".innerHTML", "eval(")
    )
    assert identity_api[2].calls == []


def test_roster_is_read_only_and_distinguishes_slack_candidates_from_registered_users(identity_api):
    client, _, provider, state, _ = identity_api
    response = client.post(ROSTER, json={}, headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["revision"] == 1 and data["node_ids"] == ["node-a", "node-b"]
    assert data["members"][0]["registered"]["external_id"] == "UADMIN"
    assert data["members"][1]["registered"] is None
    assert data["members"][1]["slack"]["display_name"] == "Morgan <unsafe>"
    assert data["next_cursor"] == "fixture-next-cursor" and len(provider.calls) == 1
    assert count(state[1], User) == 1 and count(state[1], NodeUserMapping) == 0
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("token", [READ_TOKEN, AGENT_TOKEN, MEMBER_TOKEN, "unknown-credential"])
def test_only_current_explicit_identity_administrators_can_read_a_roster(identity_api, token):
    client, _, provider, state, admin = identity_api
    register(state[0], admin)
    response = client.post(ROSTER, json={}, headers=headers(token))
    assert response.status_code == (403 if token == MEMBER_TOKEN else 401)
    assert response.headers["Cache-Control"] == "no-store" and provider.calls == []
    assert "Morgan" not in response.text


@pytest.mark.parametrize("change", ["revoke", "remap", "workspace", "provider-error"])
def test_external_roster_read_rechecks_current_identity_and_permissions_before_returning(
    identity_api, change
):
    client, _, provider, state, admin = identity_api
    service, sessions, _ = state
    member = register(service, admin).outcomes[0].entity_id
    if change == "revoke":

        def changed():
            with sessions.begin() as session:
                session.execute(delete(RoleBinding))

        provider.side_effect = changed
    elif change == "remap":
        with sessions.begin() as session:
            role = session.scalar(select(RoleBinding.role_id))
            session.add(
                RoleBinding(
                    id=str(uuid4()), user_id=member, role_id=role, scope_type="global", scope_id="*"
                )
            )

        def changed():
            with sessions.begin() as session:
                identity = session.scalar(
                    select(ExternalIdentity).where(ExternalIdentity.external_id == "UADMIN")
                )
                identity.user_id = member

        provider.side_effect = changed
    elif change == "workspace":
        provider.team_id = "TOTHER"
    else:

        def changed():
            raise RuntimeError("private upstream payload must not escape")

        provider.side_effect = changed
    response = client.post(ROSTER, json={}, headers=headers())
    assert response.status_code == (403 if change in {"revoke", "remap"} else 503)
    assert "Morgan" not in response.text and "private upstream" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_identity_frontend_contract_reaches_confirmation_worker_and_current_mapping_read(
    identity_api,
):
    client, graph, _, state, _ = identity_api
    payload = {
        "command": "identity.map_users",
        "parameters": {
            "expected_revision": 1,
            "reason": "Manual two-node attribution",
            "changes": [
                {
                    "type": "register_identity",
                    "subject": subject("UMEMBER").model_dump(),
                    "profile": {"display_name": "Morgan", "timezone": "Europe/London"},
                    "mappings": [
                        {"node_id": "node-a", "os_username": "morgan-a"},
                        {"node_id": "node-b", "os_username": "different-b"},
                    ],
                }
            ],
        },
    }
    submitted = client.post("/internal/commands", json=payload, headers=headers())
    assert submitted.status_code == 202, submitted.text
    pending = submitted.json()["outcome"]
    assert count(state[1], User) == 1 and count(state[1], NodeUserMapping) == 0
    review = client.get(
        "/internal/commands/" + pending["command_id"] + "/review", headers=headers()
    )
    assert (
        review.status_code == 200
        and review.json()["request"]["parameters"]["expected_revision"] == 1
    )
    confirmed = client.post(
        "/internal/commands/" + pending["command_id"] + "/confirm",
        json={"proof": pending["proof"]},
        headers=headers(key="fixture-ui-confirm"),
    )
    assert confirmed.status_code == 202 and confirmed.json()["outcome"]["state"] == "queued"
    assert count(state[1], NodeUserMapping) == 0
    assert graph.run_once("commands")["commands"]["state"] == "succeeded"
    result = client.get("/internal/commands/" + pending["command_id"], headers=headers())
    assert result.status_code == 200 and result.json()["state"] == "succeeded"
    current = client.post(ROSTER, json={}, headers=headers()).json()
    member = current["members"][1]["registered"]
    assert current["revision"] == 2 and [
        (mapping["node_id"], mapping["os_username"]) for mapping in member["mappings"]
    ] == [("node-a", "morgan-a"), ("node-b", "different-b")]
    assert count(state[1], CommandRecord) == 1
    with state[1]() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(RoleBinding)
                .where(RoleBinding.user_id == member["user_id"])
            )
            == 0
        )


@pytest.mark.parametrize(
    "path,payload",
    [
        (DIRECTORY, {"actor_id": str(uuid4())}),
        (DIRECTORY, {"node_ids": ["injected-node"]}),
        (ROSTER, {"namespace": "TOTHER"}),
        (ROSTER, {"cursor": "x" * 2049}),
    ],
)
def test_identity_query_context_cannot_be_overridden_by_body_or_url(identity_api, path, payload):
    response = identity_api[0].post(path, json=payload, headers=headers())
    assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"
    assert (
        identity_api[0].post(path + "?owner=other", json={}, headers=headers()).status_code == 422
    )
    assert identity_api[2].calls == []


def test_postgres_identity_query_is_read_only_and_metadata_timeouts_do_not_fake_an_empty_page(
    identity_api, monkeypatch
):
    client, graph, _, state, _ = identity_api
    if state[2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL identity query readonly and budget")
    administration = graph.identity_admin.directory.administration
    readonly = sessionmaker(bind=state[2].execution_options(postgresql_readonly=True))
    monkeypatch.setattr(administration, "sessions", readonly)
    assert client.post(DIRECTORY, json={}, headers=headers()).status_code == 200

    def slow(repo, *args):
        repo.session.execute(text("SELECT pg_sleep(2)"))

    monkeypatch.setattr(AccessAdministrationRepository, "directory_members", slow)
    response = client.post(DIRECTORY, json={}, headers=headers())
    assert response.status_code == 503 and response.json()["error"]["code"] == "QUERY_UNAVAILABLE"
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "change",
    [
        {"identity_nodes": ()},
        {"user_tokens": ()},
        {"commands": ("reservation.cancel",)},
        {"identity_nodes": ("node-a", "node-a")},
    ],
)
def test_identity_ui_requires_explicit_nodes_authentication_and_mapping_allowlist(change):
    values = {
        "namespace": NAMESPACE,
        "commands": ("identity.map_users",),
        "rules": None,
        "maintenance": (),
        "identity_admin": True,
        "identity_nodes": ("node-a",),
        "user_tokens": (UserTokenBinding(subject=subject(), token=SecretStr(ADMIN_TOKEN)),),
    }
    with pytest.raises(ValidationError):
        config(**{**values, **change})
