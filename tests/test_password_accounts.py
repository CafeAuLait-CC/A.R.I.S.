from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from aris.core.access.member_schemas import EditMember
from aris.core.access.password_models import LoginInvitation, PasswordAccount
from aris.core.access.password_schemas import ActivatePasswordInput, SetOwnPasswordInput
from aris.core.access.passwords import PasswordAccountService
from aris.core.access.session_models import LoginSession
from aris.core.access.sessions import secret_digest
from aris.core.contracts import DomainError
from tests.test_member_portal_api import login, perform, post
from tests.test_member_portal_api import portal as portal
from tests.test_member_portal_core import ADMIN_TOKEN, apply, register
from tests.test_member_portal_core import members as members

PASSWORD = "a🦉bcde"
SECOND_PASSWORD = "f☄ghij"


class Sender:
    def __init__(self):
        self.sent = []
        self.failure = False

    def send(self, subject, code, purpose):
        self.sent.append((subject, code.get_secret_value(), purpose))
        if self.failure:
            raise RuntimeError("a simulated uncertain delivery")


@pytest.fixture
def accounts(members):
    manager, admin, _, clock = members
    sender = Sender()
    service = PasswordAccountService(
        manager.sessions,
        manager.login,
        SecretStr("fixture-invitation-hmac-key-1234567890"),
        sender,
        clock=lambda: clock[0],
    )
    member = register(manager, admin)
    apply(manager, admin, [EditMember(user_id=member, login_username="member")])
    return service, sender, manager, admin, member, clock


def invitation(service, sender, username="member", purpose="activate"):
    receipt = service.request_invitation(
        username, purpose, peer="fixture-peer", request_id="invite"
    )
    return ActivatePasswordInput(
        username=username,
        password=SecretStr(PASSWORD),
        challenge_id=receipt.challenge_id,
        code=SecretStr(sender.sent[-1][1]),
    )


def test_activation_binds_slack_identity_once_and_password_login_keeps_permissions(accounts):
    service, sender, manager, _, member, _ = accounts
    data = invitation(service, sender)
    assert sender.sent[-1][0].external_id == "UMEMBER"
    assert sender.sent[-1][2] == "activate"
    grant = service.activate(data, "activate", peer="fixture-peer", request_id="activate")
    assert grant.principal.user_id == member and grant.max_age == 604800
    assert service.login.current(grant.token.get_secret_value()).principal.user_id == member
    authenticated = service.login_password(
        "member", SecretStr(PASSWORD), remember=False, peer="fixture-peer", request_id="sign-in"
    )
    assert authenticated.max_age is None
    assert manager.identity(authenticated.subject).role == "member"
    with manager.sessions() as session:
        account = session.get(PasswordAccount, str(member))
        stored = session.get(LoginInvitation, str(data.challenge_id))
        assert account.password_hash.startswith("$argon2id$")
        assert (
            PASSWORD not in account.password_hash and sender.sent[-1][1] not in stored.code_digest
        )
        assert stored.used_at is not None
    with pytest.raises(DomainError):
        service.activate(data, "activate", peer="fixture-peer", request_id="replay")
    with pytest.raises(DomainError):
        service.login_password(
            "member",
            SecretStr("incorrect password"),
            remember=True,
            peer="fixture-peer",
            request_id="bad-password",
        )


def test_invitation_attempts_expiration_resend_and_unknown_user(accounts):
    service, sender, manager, _, member, clock = accounts
    unknown = service.request_invitation(
        "unregistered", "activate", peer="peer", request_id="unknown"
    )
    assert not sender.sent and unknown.message.startswith("If this username")
    with pytest.raises(DomainError) as repeated_unknown:
        service.request_invitation(
            "unregistered", "activate", peer="peer", request_id="unknown-again"
        )
    assert repeated_unknown.value.status == 429
    data = invitation(service, sender)
    wrong = data.model_copy(
        update={
            "code": SecretStr("000000" if data.code.get_secret_value() != "000000" else "111111")
        }
    )
    for _ in range(5):
        with pytest.raises(DomainError):
            service.activate(wrong, "activate", peer="peer", request_id="wrong")
    with manager.sessions() as session:
        assert session.get(LoginInvitation, str(data.challenge_id)).attempts == 5
        assert session.get(PasswordAccount, str(member)).password_hash is None
    with pytest.raises(DomainError):
        service.activate(data, "activate", peer="peer", request_id="blocked")
    with pytest.raises(DomainError) as error:
        invitation(service, sender)
    assert error.value.status == 429
    clock[0] += timedelta(seconds=61)
    newer = invitation(service, sender)
    assert newer.challenge_id != data.challenge_id
    clock[0] += timedelta(minutes=10)
    with pytest.raises(DomainError):
        service.activate(newer, "activate", peer="peer", request_id="expired")


def test_uncertain_delivery_and_suspension_do_not_activate(accounts):
    service, sender, manager, admin, member, clock = accounts
    sender.failure = True
    with pytest.raises(DomainError) as error:
        service.request_invitation("member", "activate", peer="peer", request_id="uncertain")
    assert "could not be confirmed" in error.value.message
    with manager.sessions() as session:
        stored = session.scalars(select(LoginInvitation)).one()
        identifier = stored.id
        assert stored.delivered_at is None
    data = ActivatePasswordInput(
        username="member",
        password=SecretStr(PASSWORD),
        challenge_id=identifier,
        code=SecretStr(sender.sent[-1][1]),
    )
    with pytest.raises(DomainError):
        service.activate(data, "activate", peer="peer", request_id="not-delivered")
    sender.failure = False
    clock[0] += timedelta(seconds=61)
    data = invitation(service, sender)
    apply(manager, admin, [EditMember(user_id=member, active=False)])
    apply(manager, admin, [EditMember(user_id=member, active=True)])
    with pytest.raises(DomainError):
        service.activate(data, "activate", peer="peer", request_id="stale-after-restore")


def test_admin_password_setup_cuts_off_legacy_and_reset_revokes_old_sessions(accounts):
    service, sender, manager, _, _, clock = accounts
    legacy = manager.login.login(SecretStr(ADMIN_TOKEN), remember=True)
    setup = SetOwnPasswordInput(username="admin", password=SecretStr(PASSWORD))
    current = service.set_own_password(legacy.token.get_secret_value(), setup, request_id="setup")
    assert (
        service.login.current(current.token.get_secret_value()).principal.user_id
        == legacy.principal.user_id
    )
    with pytest.raises(DomainError):
        manager.login.current(legacy.token.get_secret_value())
    with pytest.raises(DomainError):
        manager.login.login(SecretStr(ADMIN_TOKEN), remember=True)
    with pytest.raises(DomainError):
        service.set_own_password(
            current.token.get_secret_value(), setup, request_id="missing-old-password"
        )
    reset = invitation(service, sender, "admin", "reset").model_copy(
        update={"password": SecretStr(SECOND_PASSWORD)}
    )
    restored = service.activate(reset, "reset", peer="peer", request_id="reset")
    assert manager.identity(restored.subject).role == "admin"
    with pytest.raises(DomainError):
        service.login.current(current.token.get_secret_value())
    logged_in = service.login_password(
        "admin", SecretStr(SECOND_PASSWORD), remember=True, peer="peer", request_id="new-login"
    )
    assert logged_in.principal.user_id == legacy.principal.user_id
    with manager.sessions() as session:
        old = session.get(LoginSession, secret_digest(current.token.get_secret_value()))
        assert old.revoked_at is not None


@pytest.mark.parametrize("remember", [False, True])
def test_password_setup_and_change_preserve_the_login_cookie_mode(portal, remember):
    from fastapi.testclient import TestClient

    app, components, _, _ = portal
    with TestClient(app) as client:
        login(client, remember=remember)
        first = post(
            client, "/internal/members/password", {"username": "admin", "password": PASSWORD}
        )
        assert first.status_code == 200, first.text
        assert ("Max-Age=" in first.headers["set-cookie"]) is remember
        current = components.portal.login.current(client.cookies.get("aris_member_session"))
        assert current.remember is remember
        # The password form cannot override a choice that belongs to sign-in.
        rejected = post(
            client,
            "/internal/members/password",
            {"username": "admin", "password": SECOND_PASSWORD, "remember": not remember},
        )
        assert rejected.status_code == 422
        changed = post(
            client,
            "/internal/members/password",
            {"username": "admin", "password": SECOND_PASSWORD, "current_password": PASSWORD},
        )
        assert changed.status_code == 200, changed.text
        assert ("Max-Age=" in changed.headers["set-cookie"]) is remember


def test_username_reservation_is_unique_revisioned_and_idempotent(accounts):
    service, _, manager, admin, member, _ = accounts
    before = manager.identity(
        manager.login.legacy[next(iter(manager.login.legacy))]
    ).directory_revision
    apply(manager, admin, [EditMember(user_id=member, login_username="member")])
    after = manager.identity(
        manager.login.legacy[next(iter(manager.login.legacy))]
    ).directory_revision
    assert after == before
    with pytest.raises(DomainError) as error:
        apply(manager, admin, [EditMember(user_id=admin, login_username="member")])
    assert error.value.code == "CONFLICT"
    apply(manager, admin, [EditMember(user_id=member, login_username="member-renamed")])
    assert service.summary(member).username == "member-renamed"
    assert (
        manager.identity(manager.login.legacy[next(iter(manager.login.legacy))]).directory_revision
        == before + 1
    )


def test_password_login_origin_csrf_profile_email_and_browser_migration(portal):
    from fastapi.testclient import TestClient

    app, components, _, _ = portal
    with TestClient(app) as client:
        login(client)
        too_short = post(
            client, "/internal/members/password", {"username": "admin", "password": "ab🦉de"}
        )
        assert too_short.status_code == 422
        response = post(
            client, "/internal/members/password", {"username": "admin", "password": PASSWORD}
        )
        assert response.status_code == 200, response.text
        assert "HttpOnly" in response.headers["set-cookie"]
        assert client.get("/internal/members/session").json()["account"] == {
            "username": "admin",
            "password_ready": True,
        }
        actor = client.get("/internal/members/session").json()["actor"]
        result = perform(
            client,
            components,
            "identity.edit_own_profile",
            {
                "expected_revision": actor["directory_revision"],
                "profile": {
                    "display_name": actor["display_name"],
                    "timezone": actor["timezone"],
                    "email": "alex@example.test",
                },
            },
            "email",
        )
        assert result["outcome"]["state"] == "succeeded"
        assert (
            client.get("/internal/members/session").json()["actor"]["email"] == "alex@example.test"
        )
        assert (
            client.post(
                "/internal/members/password",
                json={"username": "admin", "password": SECOND_PASSWORD},
            ).status_code
            == 403
        )
        post(client, "/internal/members/logout", {})
        assert (
            client.post(
                "/internal/members/login",
                json={"username": "ADMIN", "password": PASSWORD},
                headers={"Origin": "http://attacker.test"},
            ).status_code
            == 403
        )
        signed_in = client.post(
            "/internal/members/login",
            json={"username": " ADMIN ", "password": PASSWORD},
            headers={"Origin": "http://testserver"},
        )
        assert signed_in.status_code == 200
        assert PASSWORD not in signed_in.text


def test_same_invitation_can_only_activate_once_under_postgresql_concurrency(
    accounts, database_url
):
    if not database_url.startswith("postgresql"):
        pytest.skip("requires real row locking")
    service, sender, _, _, _, _ = accounts
    data = invitation(service, sender)

    def activate_one(index):
        try:
            return service.activate(data, "activate", peer="peer", request_id=f"concurrent-{index}")
        except DomainError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(activate_one, (1, 2)))
    assert sum(isinstance(result, str) for result in results) == 1
    assert "PERMISSION_DENIED" in results


def test_login_rate_limit_survives_a_new_service_instance(accounts):
    service, sender, manager, _, _, clock = accounts
    for _ in range(10):
        with pytest.raises(DomainError) as error:
            service.login_password(
                "unknown-user", SecretStr(PASSWORD), remember=False, peer="peer", request_id="guess"
            )
        assert error.value.status == 401
    restarted = PasswordAccountService(
        manager.sessions,
        manager.login,
        SecretStr("fixture-invitation-hmac-key-1234567890"),
        sender,
        clock=lambda: clock[0],
    )
    with pytest.raises(DomainError) as error:
        restarted.login_password(
            "unknown-user",
            SecretStr(PASSWORD),
            remember=False,
            peer="another-peer",
            request_id="restart-guess",
        )
    assert error.value.status == 429


def test_slack_invitation_transport_targets_only_the_bound_workspace_member():
    import json

    import httpx

    from aris.core.access.schemas import ExternalSubject
    from aris.interfaces.slack.client import SlackClient
    from aris.interfaces.slack.config import SlackSettings
    from aris.interfaces.slack.invitations import SlackInvitationSender

    sent = []

    def transport(request):
        sent.append(json.loads(request.content))
        assert request.url.path == "/api/chat.postMessage"
        return httpx.Response(200, json={"ok": True})

    settings = SlackSettings(
        _env_file=None,
        team_id="T111",
        signing_secret="fixture-slack-secret",
        bot_token="fixture-bot-token",
    )
    sender = SlackInvitationSender(SlackClient(settings, httpx.MockTransport(transport)), "T111")
    member = ExternalSubject(provider="slack", namespace="T111", external_id="U123456789")
    sender.send(member, SecretStr("123456"), "activate")
    assert len(sent) == 1 and sent[0]["channel"] == member.external_id
    assert "123456" in sent[0]["text"]
    with pytest.raises(ValueError):
        sender.send(
            member.model_copy(update={"namespace": "T999"}), SecretStr("123456"), "activate"
        )
    with pytest.raises(ValueError):
        sender.send(
            member.model_copy(update={"external_id": "C123456789"}), SecretStr("123456"), "activate"
        )
    assert len(sent) == 1
