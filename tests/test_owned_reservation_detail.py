import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, event, text, update

from aris.core.access.models import ExternalIdentity, RoleBinding, User
from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.commands.schemas import ActorContext
from aris.core.contracts import DomainError
from aris.interfaces.reservations.schemas import LabeledMinePage, OwnedReservationDetail
from aris.interfaces.slack.reservations import personal_reservations
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import MineQuery
from aris.modules.vega.models import GpuDevice, Node
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_command_intake import subject
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import counts, create
from tests.test_user_commands import ALICE_TOKEN, BOB_TOKEN, headers
from tests.test_user_reservations import mine_api as mine_api


@pytest.fixture
def detail_api(mine_api):
    client, surface, state = mine_api
    surface.queries.labels = surface.queries.reservations.availability.targets
    alice = create(state, key="alice-detail", purpose="Alice private detail").result["reservation"]
    bob = create(
        state,
        user="bob",
        key="bob-detail",
        start=state[4]["now"] + timedelta(hours=2),
        purpose="Bob private detail",
    ).result["reservation"]
    return client, surface, state, alice, bob


def path(reservation_id):
    return "/internal/reservations/" + str(reservation_id)


def context():
    return ExternalCommandActor(subject=subject(), request_id="fixture-detail")


def test_current_owner_receives_private_detail_with_coherent_history_version_and_label(detail_api):
    client, surface, state, alice, _ = detail_api
    before = counts(state)
    response = client.get(path(alice["id"]), headers=headers())
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["reservation"] == alice and data["reservation"]["version"] == 1
    assert data["resource_label"] == {
        "gpu_uuid": "GPU-target",
        "node_id": "node-a",
        "index": 0,
        "model": "Fixture",
    }
    assert response.headers["Cache-Control"] == "no-store" and counts(state) == before
    assert ALICE_TOKEN not in response.text
    assert "Alice private detail" not in repr(surface.queries.detail(context(), UUID(alice["id"])))


def test_foreign_and_missing_id_have_identical_not_found_without_private_fields(detail_api):
    client, _, _, _, bob = detail_api
    foreign = client.get(path(bob["id"]), headers=headers())
    missing = client.get(path(uuid4()), headers=headers())
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert "Bob private detail" not in foreign.text and bob["id"] not in foreign.text


@pytest.mark.parametrize("token", [READ_TOKEN, AGENT_TOKEN, "wrong-token"])
def test_observation_credentials_cannot_read_details(detail_api, token):
    client, _, _, alice, _ = detail_api
    response = client.get(path(alice["id"]), headers=headers(token=token))
    assert response.status_code == 401 and "Alice private detail" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_only_current_owner_may_read_even_when_another_user_knows_the_id(detail_api):
    client, _, _, alice, bob = detail_api
    assert client.get(path(alice["id"]), headers=headers(token=BOB_TOKEN)).status_code == 404
    own = client.get(path(bob["id"]), headers=headers(token=BOB_TOKEN))
    assert own.status_code == 200 and own.json()["reservation"]["purpose"] == "Bob private detail"


@pytest.mark.parametrize("change", ["suspend", "permission", "remap"])
def test_detail_uses_current_identity_and_read_grant(detail_api, change):
    client, _, state, alice, _ = detail_api
    with state[2].begin() as session:
        if change == "suspend":
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
                update(ExternalIdentity)
                .where(ExternalIdentity.user_id == state[3]["alice"])
                .values(user_id=state[3]["bob"])
            )
    response = client.get(path(alice["id"]), headers=headers())
    assert response.status_code == (404 if change == "remap" else 403)
    assert "Alice private detail" not in response.text


@pytest.mark.parametrize("change", ["identity", "permission"])
@pytest.mark.parametrize("phase", ["detail", "page"])
def test_late_identity_or_permission_change_blocks_already_read_detail_and_page(
    detail_api, monkeypatch, change, phase
):
    client, surface, state, alice, _ = detail_api
    original = surface.queries.labels.labels_in_transaction

    def changed(session, query):
        labels = original(session, query)
        with state[2].begin() as other:
            if change == "identity":
                other.execute(
                    update(ExternalIdentity)
                    .where(ExternalIdentity.user_id == state[3]["alice"])
                    .values(user_id=state[3]["bob"])
                )
            else:
                other.execute(
                    delete(RoleBinding).where(
                        RoleBinding.user_id == state[3]["alice"],
                        RoleBinding.role_id == state[3]["reader"],
                    )
                )
        return labels

    monkeypatch.setattr(surface.queries.labels, "labels_in_transaction", changed)
    if phase == "detail":
        response = client.get(path(alice["id"]), headers=headers())
        assert response.status_code == 403 and "Alice private detail" not in response.text
    else:
        with pytest.raises(DomainError) as error:
            surface.queries.mine_labeled(context(), MineQuery())
        assert error.value.code == "PERMISSION_DENIED"


def test_missing_hardware_and_missing_history_remain_explicit_without_erasing_personal_record(
    detail_api,
):
    client, _, state, alice, _ = detail_api
    with state[2].begin() as session:
        session.execute(delete(GpuDevice))
        session.execute(
            delete(ReservationEvent).where(ReservationEvent.reservation_id == alice["id"])
        )
        session.execute(update(Node).values(collector_status="error"))
    response = client.get(path(alice["id"]), headers=headers())
    assert response.status_code == 200
    assert response.json()["resource_label"] is None
    assert response.json()["reservation"]["version"] is None
    assert response.json()["reservation"]["purpose"] == "Alice private detail"


def test_detail_and_version_are_selected_in_one_statement_without_flushing_caller_work(detail_api):
    _, surface, state, alice, _ = detail_api
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        statements.append(statement)

    event.listen(state[5], "before_cursor_execute", capture)
    try:
        with state[2]() as session:
            row = session.get(Reservation, alice["id"])
            row.purpose = "Uncommitted private edit"
            statements.clear()
            result = surface.queries.reservations.owned_in_transaction(
                session,
                ActorContext(actor_id=state[3]["alice"], request_id="fixture-domain-detail"),
                UUID(alice["id"]),
            )
            assert result.purpose == "Alice private detail" and result.version == 1
            assert row in session.dirty and session.in_transaction()
            snapshots = [sql for sql in statements if "reservation_events" in sql]
            assert len(snapshots) == 1 and "reservations.owner_id" in snapshots[0]
            assert not any(
                sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))
                for sql in statements
            )
            session.rollback()
    finally:
        event.remove(state[5], "before_cursor_execute", capture)


def test_friendly_mine_labels_are_last_known_and_do_not_show_hardware_uuid(detail_api):
    _, surface, state, alice, _ = detail_api
    page = surface.queries.mine_labeled(context(), MineQuery())
    view = personal_reservations(page.page, page.labels)
    encoded = json.dumps(view)
    assert "node-a" in encoded and "GPU 0" in encoded and "Fixture" in encoded
    assert "GPU-target" not in encoded and "last-known inventory" in encoded
    assert "Alice private detail" in encoded and "Bob private detail" not in encoded
    with state[2].begin() as session:
        session.execute(delete(GpuDevice))
    page = surface.queries.mine_labeled(context(), MineQuery())
    encoded = json.dumps(personal_reservations(page.page, page.labels))
    assert "GPU information unavailable" in encoded and alice["id"] in encoded


def test_return_dtos_reject_labels_for_unrelated_resources(detail_api):
    _, surface, _, alice, _ = detail_api
    result = surface.queries.detail(context(), UUID(alice["id"]))
    wrong = result.resource_label.model_copy(update={"gpu_uuid": "GPU-other"})
    with pytest.raises(ValueError):
        OwnedReservationDetail(reservation=result.reservation, resource_label=wrong)
    page = surface.queries.mine_labeled(context(), MineQuery())
    with pytest.raises(ValueError):
        LabeledMinePage(page=page.page, labels=(wrong,))


def test_ambiguous_headers_and_authority_queries_are_rejected(detail_api):
    client, _, _, alice, _ = detail_api
    assert (
        client.get(
            path(alice["id"]), headers=headers(), params={"owner_id": "somebody"}
        ).status_code
        == 422
    )
    response = client.get(
        path(alice["id"]),
        headers=[
            ("Authorization", "Bearer " + ALICE_TOKEN),
            ("Authorization", "Bearer " + BOB_TOKEN),
        ],
    )
    assert response.status_code == 401
    assert client.get("/internal/reservations/mine", headers=headers()).status_code == 200


def test_detail_and_friendly_page_work_with_pg_read_only_transactions(detail_api):
    client, surface, state, alice, _ = detail_api
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL READ ONLY detail")

    def readonly(conn):
        conn.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(state[5], "begin", readonly)
    try:
        assert client.get(path(alice["id"]), headers=headers()).status_code == 200
        assert surface.queries.mine_labeled(context(), MineQuery()).page.reservations
    finally:
        event.remove(state[5], "begin", readonly)


def test_label_query_timeout_is_unavailable_without_leaking_already_read_purpose(
    detail_api, monkeypatch
):
    client, surface, state, alice, _ = detail_api
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL label budget")

    def slow(session, query):
        session.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("budget failed")

    monkeypatch.setattr(surface.queries.labels, "labels_in_transaction", slow)
    response = client.get(path(alice["id"]), headers=headers())
    assert response.status_code == 503 and response.json()["error"]["code"] == "QUERY_UNAVAILABLE"
    assert "Alice private detail" not in response.text
