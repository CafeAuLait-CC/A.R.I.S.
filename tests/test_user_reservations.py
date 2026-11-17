import time
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, event, text, update

from aris.apps.api.main import create_app
from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.access.schemas import ExternalSubject
from aris.interfaces.commands.auth import BoundSubjectTokens, UserTokenBinding
from aris.interfaces.reservations.service import UserReservationSurface
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.reservation_schemas import MineQuery
from aris.modules.vega.models import Node
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_command_intake import NAMESPACE, subject
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import counts, create
from tests.test_user_commands import ALICE_TOKEN, BOB_TOKEN, LIMITS, headers

PATH = "/internal/reservations/mine"


@pytest.fixture
def mine_api(booking, settings):
    reservations, _, sessions, ids, _, _ = booking
    with sessions.begin() as session:
        for name in ("alice", "bob"):
            session.add(
                ExternalIdentity(id=str(uuid4()), user_id=ids[name], **subject(name).model_dump())
            )
    auth = BoundSubjectTokens(
        [
            UserTokenBinding(subject=subject(name), token=SecretStr(token))
            for name, token in (("alice", ALICE_TOKEN), ("bob", BOB_TOKEN))
        ],
        reserved_tokens=[settings.read_token, SecretStr(AGENT_TOKEN)],
    )
    surface = UserReservationSurface(sessions, reservations, NAMESPACE, auth, LIMITS)
    with TestClient(
        create_app(settings, SlackSettings(_env_file=None), user_reservations=surface),
        raise_server_exceptions=False,
    ) as client:
        yield client, surface, booking


def seed_personal_reservations(state):
    results = []
    for index, user in enumerate(("alice", "bob", "alice")):
        results.append(
            create(
                state,
                user=user,
                key=f"personal-{index}",
                start=state[4]["now"] + timedelta(hours=index + 1),
            ).result["reservation"]
        )
    return results


def test_default_application_has_no_personal_reservation_route(client):
    assert client.get(PATH, headers=headers()).status_code == 404


def test_mine_returns_only_current_owner_and_stable_tied_timestamp_pages(mine_api):
    client, _, state = mine_api
    records = seed_personal_reservations(state)
    before = counts(state)
    first = client.get(PATH, headers=headers(), params={"limit": "1"})
    assert first.status_code == 200, first.text
    data = first.json()
    cursor = data["next_cursor"]
    assert cursor is not None and len(data["reservations"]) == 1
    second = client.get(
        PATH,
        headers=headers(),
        params={
            "limit": "1",
            "before_created_at": cursor["created_at"],
            "before_id": cursor["reservation_id"],
        },
    )
    assert second.status_code == 200 and second.json()["next_cursor"] is None
    own_ids = {item["id"] for item in data["reservations"] + second.json()["reservations"]}
    assert own_ids == {records[0]["id"], records[2]["id"]}
    assert records[1]["id"] not in first.text + second.text
    bob = client.get(PATH, headers=headers(token=BOB_TOKEN))
    assert [item["id"] for item in bob.json()["reservations"]] == [records[1]["id"]]
    assert all(response.headers["Cache-Control"] == "no-store" for response in (first, second, bob))
    assert counts(state) == before


def test_mine_empty_is_real_and_offline_gpu_does_not_hide_personal_history(mine_api):
    client, _, state = mine_api
    empty = client.get(PATH, headers=headers())
    assert empty.status_code == 200 and empty.json() == {"reservations": [], "next_cursor": None}
    created = create(state).result["reservation"]
    with state[2].begin() as session:
        old = state[4]["now"] - timedelta(days=1)
        session.execute(
            update(Node).values(observed_at=old, received_at=old, collector_status="error")
        )
    response = client.get(PATH, headers=headers())
    assert response.status_code == 200
    assert response.json()["reservations"][0]["id"] == created["id"]


@pytest.mark.parametrize("token", [READ_TOKEN, AGENT_TOKEN, "wrong-user-token"])
def test_observation_credentials_cannot_read_user_reservation_purposes(mine_api, token):
    client, _, state = mine_api
    create(state)
    before = counts(state)
    response = client.get(PATH, headers=headers(token=token))
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"
    assert "Private fixture purpose" not in response.text and counts(state) == before


@pytest.mark.parametrize("change", ["unmapped", "suspended", "permission", "wrong_scope"])
def test_each_query_rechecks_current_identity_and_explicit_mine_permission(mine_api, change):
    client, _, state = mine_api
    create(state)
    assert client.get(PATH, headers=headers()).status_code == 200
    with state[2].begin() as session:
        if change == "unmapped":
            session.execute(
                delete(ExternalIdentity).where(ExternalIdentity.user_id == state[3]["alice"])
            )
        elif change == "suspended":
            session.execute(
                update(User).where(User.id == state[3]["alice"]).values(status="suspended")
            )
        elif change == "permission":
            session.execute(
                delete(RoleBinding).where(
                    RoleBinding.user_id == state[3]["alice"],
                    RoleBinding.role_id == state[3]["reader"],
                )
            )
        else:
            session.execute(
                update(RoleBinding)
                .where(
                    RoleBinding.user_id == state[3]["alice"],
                    RoleBinding.role_id == state[3]["reader"],
                )
                .values(scope_type="node", scope_id="node-a")
            )
    response = client.get(PATH, headers=headers())
    assert response.status_code == 403 and "Private fixture purpose" not in response.text


def test_remapped_subject_does_not_keep_previous_owners_history(mine_api):
    client, _, state = mine_api
    records = seed_personal_reservations(state)
    first = client.get(PATH, headers=headers(), params={"limit": "1"})
    old_cursor = first.json()["next_cursor"]
    with state[2].begin() as session:
        session.execute(
            update(ExternalIdentity)
            .where(ExternalIdentity.external_id == subject().external_id)
            .values(user_id=state[3]["bob"])
        )
    current = client.get(PATH, headers=headers())
    assert [item["id"] for item in current.json()["reservations"]] == [records[1]["id"]]
    continued = client.get(
        PATH,
        headers=headers(),
        params={
            "before_created_at": old_cursor["created_at"],
            "before_id": old_cursor["reservation_id"],
        },
    )
    assert continued.status_code == 200
    assert all(item["id"] == records[1]["id"] for item in continued.json()["reservations"])


@pytest.mark.parametrize(
    "params",
    [
        {"owner_id": "somebody"},
        {"actor_id": "somebody"},
        {"scope": "global"},
        {"limit": "0"},
        {"limit": "51"},
        {"limit": "1.0"},
        {"limit": "99999999999999"},
        {"before_id": str(uuid4())},
        {"before_created_at": "2026-11-17T02:00:00Z"},
        {"before_id": str(uuid4()), "before_created_at": "2026-11-17T02:00:00"},
        {"before_id": "not-uuid", "before_created_at": "2026-11-17T02:00:00Z"},
        [("limit", "1"), ("limit", "2")],
    ],
)
def test_query_cannot_inject_owner_or_ambiguous_and_unbounded_pagination(mine_api, params):
    client, _, state = mine_api
    before = counts(state)
    response = client.get(PATH, headers=headers(), params=params)
    assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"
    assert counts(state) == before


def test_duplicate_authorization_and_cross_workspace_subject_fail_closed(mine_api):
    client, surface, _ = mine_api
    response = client.get(
        PATH,
        headers=[
            ("Authorization", "Bearer " + ALICE_TOKEN),
            ("Authorization", "Bearer " + BOB_TOKEN),
        ],
    )
    assert response.status_code == 401
    surface.authentication = BoundSubjectTokens(
        [
            UserTokenBinding(
                subject=ExternalSubject(
                    provider="slack", namespace="T_OTHER", external_id="U_FIXTURE"
                ),
                token=SecretStr(ALICE_TOKEN),
            )
        ],
        reserved_tokens=[],
    )
    response = client.get(PATH, headers=headers())
    assert response.status_code == 403


def test_personal_query_cannot_activate_production_or_reserved_credentials(mine_api, settings):
    _, surface, _ = mine_api
    with pytest.raises(ValueError, match="production"):
        create_app(
            settings.model_copy(update={"environment": "production"}), user_reservations=surface
        )
    with pytest.raises(ValueError, match="reserved"):
        create_app(
            settings.model_copy(update={"read_token": SecretStr(ALICE_TOKEN)}),
            user_reservations=surface,
        )


def test_personal_query_uses_a_read_only_database_transaction(mine_api):
    client, _, state = mine_api
    create(state)
    before = counts(state)
    engine = state[5]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL READ ONLY contract")

    def read_only(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(engine, "begin", read_only)
    try:
        response = client.get(PATH, headers=headers())
    finally:
        event.remove(engine, "begin", read_only)
    assert response.status_code == 200 and len(response.json()["reservations"]) == 1
    assert counts(state) == before


def test_personal_identity_lookup_has_a_database_time_budget(mine_api, monkeypatch):
    client, surface, state = mine_api
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL query budget")

    def slow_lookup(session, subject):
        session.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("database budget did not stop the lookup")

    monkeypatch.setattr(surface.access, "resolve_external_in_transaction", slow_lookup)
    started = time.monotonic()
    response = client.get(PATH, headers=headers())
    assert response.status_code == 503 and response.json()["error"]["code"] == "QUERY_UNAVAILABLE"
    assert time.monotonic() - started < 1.5


def test_query_does_not_invoke_gpu_or_notification_dependencies(mine_api, monkeypatch):
    client, surface, state = mine_api
    create(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("personal query must not fetch GPU facts")

    monkeypatch.setattr(surface.reservations.availability.targets, "gpu_in_transaction", forbidden)
    assert client.get(PATH, headers=headers()).status_code == 200
    assert surface.mine("Bearer " + ALICE_TOKEN, "fixture-query", MineQuery()).reservations
