from datetime import timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from aris.apps.api.main import create_app
from aris.apps.composition.members import MemberRuntimeConfig, build_member_components
from aris.core.access.admin_schemas import (
    AdministrationBatch,
    AdministrationContext,
    BindRole,
    DefineRole,
)
from aris.core.access.member_repo import MemberRepository
from aris.core.access.member_schemas import EditMember
from aris.core.access.schemas import AccessRequest, AccessScope
from aris.core.access.sessions import secret_digest
from aris.core.contracts import DomainError
from aris.interfaces.commands.auth import UserTokenBinding
from aris.interfaces.members.api import COOKIE
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.usage.activity_service import ActivityPreviewConfig
from aris.modules.lyra.ledger_schemas import UsageLedgerHead
from aris.modules.vega.models import Node
from tests.conftest import AGENT_TOKEN
from tests.test_historical_usage_facts import owner, ownership
from tests.test_member_portal_core import (
    ADMIN_TOKEN,
    GLOBAL,
    MEMBER_TOKEN,
    NOW,
    apply,
    issue,
    register,
)
from tests.test_member_portal_core import members as members
from tests.test_recorded_usage import measured
from tests.test_resource_telemetry import put, resource_batch

ORIGIN = "http://testserver"


@pytest.fixture
def portal(members, settings):
    manager, admin, subject, clock = members
    with manager.sessions.begin() as session:
        for node, agent in (("node-a", "agent-a"), ("node-b", "agent-b")):
            session.add(
                Node(
                    id=node,
                    agent_id=agent,
                    hostname="fixture.local",
                    agent_version="fixture",
                    last_seq=0,
                    observed_at=NOW,
                    received_at=NOW,
                )
            )
    config = MemberRuntimeConfig(
        namespace=manager.administration.namespace,
        node_ids=("node-a", "node-b"),
        origin=ORIGIN,
        user_tokens=(UserTokenBinding(subject=subject, token=SecretStr(ADMIN_TOKEN)),),
        confirmation_key=SecretStr("fixture-member-confirmation-key-123456789"),
        activity_preview=ActivityPreviewConfig(
            sources=(
                {"node_id": "node-a", "agent_id": "agent-a"},
                {"node_id": "node-b", "agent_id": "agent-b"},
            ),
            rule={"source_ref": "fixture-member-diagnostic", "max_gap_seconds": 45},
            hardware_metrics=True,
        ),
    )
    slack = SlackSettings(_env_file=None, team_id="", signing_secret="", bot_token="")
    components = build_member_components(
        manager.sessions, settings, slack, config, clock=lambda: clock[0]
    )
    app = create_app(
        settings,
        slack,
        user_commands=components.portal.commands,
        usage_history=components.portal.usage,
        member_portal=components.portal,
    )
    return app, components, manager, admin


def login(client, token=ADMIN_TOKEN, remember=True):
    response = client.post(
        "/internal/members/login",
        json={"credential": token, "remember": remember},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200, response.text
    view = client.get("/internal/members/session")
    assert view.status_code == 200, view.text
    return response, view.json()


def post(client, path, body, key=None):
    csrf = client.get("/internal/members/session").json()["csrf_token"]
    return client.post(
        path,
        json=body,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            **({"Idempotency-Key": key} if key else {}),
        },
    )


def perform(client, components, command, parameters, key):
    submitted = post(
        client, "/internal/commands", {"command": command, "parameters": parameters}, key
    )
    assert submitted.status_code == 202, submitted.text
    outcome = submitted.json()["outcome"]
    cid = outcome["command_id"]
    confirmed = post(
        client,
        "/internal/commands/" + cid + "/confirm",
        {"proof": outcome["proof"]},
        key + "-confirm",
    )
    assert confirmed.status_code == 202, confirmed.text
    result = components.worker.run_once()
    assert result.state == "succeeded", result
    return client.get("/internal/commands/" + cid + "/review").json()


def window():
    return {
        "kind": "utc",
        "start_at": (NOW - timedelta(days=7)).isoformat(),
        "end_at": NOW.isoformat(),
        "timezone": "UTC",
    }


def test_browser_cookie_restores_without_reentering_credential_and_logout_revokes(portal):
    app, components, _, _ = portal
    with TestClient(app) as client:
        response, view = login(client)
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=604800" in cookie
        token = client.cookies[COOKIE]
        assert token not in response.text and ADMIN_TOKEN not in view.__repr__()
        assert client.get("/internal/members/ui").status_code == 200
        assert client.get("/internal/members/session").json()["actor"]["role"] == "admin"
        assert client.get("/internal/usage/ui").status_code == 200
        assert 'data-member-sessions="true"' in client.get("/internal/usage/ui").text
        assert (
            post(client, "/internal/usage/session", None).json()["credential_mode"]
            == "member_session"
        )
        assert post(client, "/internal/members/logout", {}).status_code == 200
        assert client.get("/internal/members/session").status_code == 401
        with pytest.raises(DomainError):
            components.portal.login.current(token)


def test_cookie_requests_require_exact_origin_csrf_and_one_credential(portal):
    app, _, _, _ = portal
    with TestClient(app) as client:
        rejected = client.post(
            "/internal/members/login", json={"credential": ADMIN_TOKEN, "remember": True}
        )
        assert rejected.status_code == 403
        _, view = login(client)
        assert (
            client.post(
                "/internal/members/directory", json={}, headers={"Origin": ORIGIN}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/members/directory",
                json={},
                headers={"Origin": "http://attacker.test", "X-CSRF-Token": view["csrf_token"]},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/internal/members/directory",
                json={},
                headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
            ).status_code
            == 403
        )
        assert post(client, "/internal/members/directory", {}).status_code == 200
        assert (
            client.get(
                "/internal/members/session", headers={"Authorization": "Bearer " + ADMIN_TOKEN}
            ).status_code
            == 401
        )
        raw = client.cookies[COOKIE]
        assert (
            client.get(
                "/internal/members/session",
                headers={"Cookie": COOKIE + "=" + raw + "; " + COOKIE + "=" + raw},
            ).status_code
            == 401
        )


def test_multiple_new_members_register_in_one_confirmed_batch_with_default_member_role(portal):
    app, components, manager, admin = portal
    with TestClient(app) as client:
        _, view = login(client)
        changes = [
            {
                "type": "register_identity",
                "subject": {"provider": "slack", "namespace": "TDEMO", "external_id": name},
                "profile": {"display_name": name, "timezone": "UTC"},
                "mappings": [{"node_id": "node-a", "os_username": name.lower()}],
            }
            for name in ("UONE", "UTWO")
        ]
        result = perform(
            client,
            components,
            "identity.manage_members",
            {
                "expected_revision": view["actor"]["directory_revision"],
                "reason": "Register two lab members",
                "changes": changes,
            },
            "batch-register",
        )
        assert result["outcome"]["state"] == "succeeded"
        page = post(client, "/internal/members/directory", {}).json()
        assert len(page["directory"]["members"]) == 3
        assert [access["role"] for access in page["access"]].count("member") == 2
        target = next(
            row["user_id"] for row in page["directory"]["members"] if row["external_id"] == "UONE"
        )
        rejected = post(
            client,
            "/internal/commands",
            {
                "command": "identity.manage_members",
                "parameters": {
                    "expected_revision": page["directory"]["revision"],
                    "reason": "Reject a node credential reused as a personal credential",
                    "changes": [
                        {
                            "type": "issue_login_credential",
                            "user_id": target,
                            "token_digest": secret_digest(AGENT_TOKEN),
                            "expected_generation": None,
                        }
                    ],
                },
            },
            "reserved-node-credential",
        )
        assert (
            rejected.status_code == 422 and rejected.json()["error"]["code"] == "VALIDATION_ERROR"
        )


def test_member_can_edit_only_their_profile_through_the_confirmed_command(portal):
    app, components, manager, admin = portal
    user = register(manager, admin)
    issue(manager, admin, user)
    with TestClient(app) as client:
        _, view = login(client, MEMBER_TOKEN)
        assert view["actor"]["role"] == "member"
        for injected in ({"user_id": str(admin)}, {"role": "admin"}, {"status": "active"}):
            request = {
                "expected_revision": view["actor"]["directory_revision"],
                "profile": {"display_name": "Member renamed", "timezone": "Europe/London"},
                **injected,
            }
            assert (
                post(
                    client,
                    "/internal/commands",
                    {"command": "identity.edit_own_profile", "parameters": request},
                    "invalid-" + next(iter(injected)),
                ).status_code
                == 422
            )
        result = perform(
            client,
            components,
            "identity.edit_own_profile",
            {
                "expected_revision": view["actor"]["directory_revision"],
                "profile": {"display_name": "Member renamed", "timezone": "Europe/London"},
            },
            "self-profile",
        )
        assert result["outcome"]["state"] == "succeeded"
        current = client.get("/internal/members/session").json()["actor"]
        assert (
            current["user_id"] == str(user)
            and current["display_name"] == "Member renamed"
            and current["role"] == "member"
        )
        assert post(client, "/internal/members/directory", {}).status_code == 403


def test_general_response_is_anonymous_while_own_queries_exclude_other_users(portal):
    app, components, manager, admin = portal
    user = register(manager, admin)
    issue(manager, admin, user)
    first = measured(owner_id=user)
    second = measured(previous=first, owner_id=admin, start=40, end=100)
    head = UsageLedgerHead(node_id="node-a", revision=2, fingerprint=second.fingerprint)
    with TestClient(app) as client:
        assert (
            put(
                client, ownership(resource_batch(), [owner(username="other-private-linux-user")])
            ).status_code
            == 200
        )
        login(client, MEMBER_TOKEN)
        with (
            patch("aris.modules.lyra.ledger_repo.UsageLedgerRepository.head", return_value=head),
            patch(
                "aris.modules.lyra.ledger_repo.UsageLedgerRepository.prefix",
                return_value=(first, second),
            ),
        ):
            result = post(
                client, "/internal/members/general", {"node_id": "node-a", "window": window()}
            )
            assert result.status_code == 200, result.text
            assert result.json()["recorded_usage"]["recorded_microseconds"] == 90000000
            assert not any(
                value in result.text
                for value in (
                    "other-private-linux-user",
                    "owner_id",
                    "username",
                    "identity_id",
                    str(admin),
                    str(user),
                )
            )
            own = post(
                client,
                "/internal/usage/query",
                {"node_id": "node-a", "mode": "mine", "window": window()},
            )
            assert own.status_code == 200, own.text
            assert own.json()["report"]["recorded_microseconds"] == 30000000
            assert str(admin) not in own.text


def test_member_cannot_use_legacy_read_all_binding_for_any_detail_endpoint(portal):
    app, components, manager, admin = portal
    user = register(manager, admin)
    issue(manager, admin, user)
    manager.administration.apply(
        AdministrationContext(
            actor_id=admin, request_id="legacy-all", reason="Fixture legacy permission"
        ),
        AdministrationBatch(
            changes=(
                DefineRole(name="legacy-all", permissions=("usage.read_all",)),
                BindRole(user_id=user, role_name="legacy-all", scope=GLOBAL, present=True),
            )
        ),
    )
    with TestClient(app) as client:
        login(client, MEMBER_TOKEN)
        session = post(client, "/internal/usage/session", None).json()
        assert all(node["mine"] and not node["node"] for node in session["nodes"])
        query = {"node_id": "node-a", "mode": "node", "window": window()}
        selections = [
            ("/query", query),
            ("/export", {**query, "snapshot": {"node_id": "node-a", "revision": 0}}),
            ("/report", {**query, "snapshot": {"node_id": "node-a", "revision": 0}}),
            (
                "/groups/query",
                {"node_ids": ["node-a", "node-b"], "mode": "node", "window": window()},
            ),
            ("/snapshots/query", query),
            ("/weekly/query", query),
            ("/weekly/plans", {"node_id": "node-a", "mode": "node"}),
            ("/activity/query", {"node_id": "node-a", "max_messages": 128}),
        ]
        for path, payload in selections:
            denied = post(client, "/internal/usage" + path, payload)
            assert denied.status_code == 403, (path, denied.text)


def test_role_downgrade_retains_unrelated_business_role_and_removes_full_statistics(portal):
    _, _, manager, admin = portal
    user = register(manager, admin)
    manager.administration.catalog = manager.administration.catalog | {"reservation.create_own"}
    manager.administration.apply(
        AdministrationContext(
            actor_id=admin,
            request_id="fixture-extra",
            reason="Keep the existing reservation access",
        ),
        AdministrationBatch(
            changes=(
                DefineRole(name="booking", permissions=("reservation.create_own",)),
                BindRole(
                    user_id=user,
                    role_name="booking",
                    scope=AccessScope(type="node", id="node-a"),
                    present=True,
                ),
                DefineRole(name="legacy-all", permissions=("usage.read_all",)),
                BindRole(user_id=user, role_name="legacy-all", scope=GLOBAL, present=True),
            )
        ),
    )
    with manager.sessions() as session:
        bindings = MemberRepository(session).bindings(str(user))
    apply(manager, admin, [EditMember(user_id=user, role="member", previous_bindings=bindings)])
    manager.access.require_permission(
        AccessRequest(
            user_id=user,
            permission="reservation.create_own",
            scope=AccessScope(type="node", id="node-a"),
        )
    )
    with pytest.raises(DomainError):
        manager.access.require_permission(
            AccessRequest(user_id=user, permission="usage.read_all", scope=GLOBAL)
        )
    assert {binding.role_name for binding in manager.summaries(admin, (user,))[0].bindings} == {
        "member",
        "booking",
    }
