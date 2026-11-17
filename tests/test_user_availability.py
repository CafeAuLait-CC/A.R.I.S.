from datetime import timedelta, timezone

import pytest
from sqlalchemy import delete, event, text, update

from aris.core.access.models import RoleBinding
from aris.modules.nova.availability import AvailabilityService
from aris.modules.vega.models import GpuDevice
from tests.conftest import AGENT_TOKEN, READ_TOKEN
from tests.test_availability import RULES, query
from tests.test_reservation_service import booking as booking
from tests.test_reservation_service import counts, create
from tests.test_user_commands import headers
from tests.test_user_reservations import mine_api as mine_api

PATH = "/internal/reservations/availability"


def parameters(**changes):
    return {**query().model_dump(mode="json"), **changes}


def test_authorized_future_window_can_be_available_while_gpu_currently_busy(mine_api):
    client, _, state = mine_api
    before = counts(state)
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["code"] == "AVAILABLE" and result["available"] is True
    assert result["assessment_scope"] == "resource_window"
    assert result["resource"]["node_id"] == "node-a"
    assert result["rules_fingerprint"] == RULES.fingerprint
    assert result["reservation_conflicts"] == {"total": 0, "windows": [], "truncated": False}
    assert response.headers["Cache-Control"] == "no-store"
    assert counts(state) == before


def test_other_users_reservation_conflict_is_visible_without_private_fields(mine_api):
    client, _, state = mine_api
    created = create(state, user="bob", purpose="Other private purpose").result["reservation"]
    before = counts(state)
    conflict = client.get(PATH, headers=headers(), params=parameters())
    assert conflict.status_code == 200 and conflict.json()["code"] == "CONFLICT"
    assert conflict.json()["reservation_conflicts"]["total"] == 1
    assert conflict.json()["available"] is False
    assert created["id"] not in conflict.text and state[3]["bob"] not in conflict.text
    assert "Other private purpose" not in conflict.text
    adjacent = query(start=query().end_at)
    allowed = client.get(PATH, headers=headers(), params=adjacent.model_dump(mode="json"))
    assert allowed.status_code == 200 and allowed.json()["code"] == "AVAILABLE"
    assert counts(state) == before


def test_idle_gpu_does_not_override_a_booked_window(mine_api):
    client, _, state = mine_api
    create(state, user="bob")
    with state[2].begin() as session:
        session.execute(update(GpuDevice).values(memory_used_mib=0, utilization_percent=0))
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 200 and response.json()["code"] == "CONFLICT"


@pytest.mark.parametrize("missing", ["rules", "maintenance"])
def test_unconfigured_rules_and_maintenance_stay_unknown(mine_api, missing):
    client, surface, state = mine_api
    previous = surface.queries.reservations.availability
    surface.queries.reservations.availability = AvailabilityService(
        state[2],
        previous.targets,
        rules=None if missing == "rules" else RULES,
        maintenance=()
        if missing == "maintenance"
        else (previous.maintenance_snapshot("GPU-target"),),
        clock=previous.clock,
    )
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 200
    assert response.json()["code"] == "NOT_EVALUATED" and response.json()["available"] is None
    assert response.json()["reservation_conflicts"] is None
    if missing == "rules":
        assert response.json()["rules_fingerprint"] is None


@pytest.mark.parametrize("age", [61, 181])
def test_stale_and_offline_facts_are_unknown_even_for_an_empty_window(mine_api, age):
    client, _, state = mine_api
    state[4]["now"] += timedelta(seconds=age)
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 200
    assert response.json()["code"] == "STALE_DATA" and response.json()["available"] is None
    assert response.json()["reservation_conflicts"] is None


def test_unknown_or_absent_target_is_never_reported_as_an_available_gpu(mine_api):
    client, _, state = mine_api
    unknown = client.get(PATH, headers=headers(), params=parameters(gpu_uuid="GPU-unknown"))
    assert unknown.status_code == 404
    with state[2].begin() as session:
        session.execute(update(GpuDevice).values(present=False))
    absent = client.get(PATH, headers=headers(), params=parameters())
    assert absent.status_code == 200 and absent.json()["code"] == "NOT_FOUND"
    assert absent.json()["available"] is False


def test_basic_rule_rejection_is_explicit_without_full_policy_approval(mine_api):
    client, _, _ = mine_api
    target = query()
    response = client.get(
        PATH,
        headers=headers(),
        params=parameters(end_at=(target.start_at + timedelta(hours=5)).isoformat()),
    )
    assert response.status_code == 200 and response.json()["code"] == "POLICY_REJECTED"
    assert response.json()["reason"] == "WINDOW_TOO_LONG"
    assert "quota" not in response.json() and "policy_version" not in response.json()


@pytest.mark.parametrize("scope", ["no_create", "other_node"])
def test_mine_permission_and_other_node_grants_do_not_authorize_window_query(mine_api, scope):
    client, _, state = mine_api
    with state[2].begin() as session:
        criteria = (
            RoleBinding.user_id == state[3]["alice"],
            RoleBinding.role_id == state[3]["creator"],
        )
        if scope == "no_create":
            session.execute(delete(RoleBinding).where(*criteria))
        else:
            session.execute(update(RoleBinding).where(*criteria).values(scope_id="node-b"))
    assert client.get("/internal/reservations/mine", headers=headers()).status_code == 200
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 403 and "GPU-target" not in response.text


@pytest.mark.parametrize("token", [READ_TOKEN, AGENT_TOKEN, "wrong-user-token"])
def test_observation_credentials_do_not_authorize_reservation_window_query(mine_api, token):
    client, _, _ = mine_api
    response = client.get(PATH, headers=headers(token=token), params=parameters())
    assert response.status_code == 401 and response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "extra",
    [
        {"actor_id": "other"},
        {"owner_id": "other"},
        {"scope": "node-b"},
        {"exclude_reservation_id": "ignore-a-conflict"},
        {"gpu_uuid": ""},
        {"start_at": "2026-11-17T01:00:00"},
        {"end_at": query().start_at.isoformat()},
    ],
)
def test_window_query_rejects_authority_fields_exclusions_and_invalid_times(mine_api, extra):
    client, _, state = mine_api
    before = counts(state)
    response = client.get(PATH, headers=headers(), params=parameters(**extra))
    assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"
    assert counts(state) == before


def test_ambiguous_target_parameter_is_rejected_and_equivalent_timezones_are_normalized(mine_api):
    client, _, _ = mine_api
    duplicate = list(parameters().items()) + [("gpu_uuid", "GPU-other")]
    assert client.get(PATH, headers=headers(), params=duplicate).status_code == 422
    other_zone = timezone(timedelta(hours=9))
    target = query()
    response = client.get(
        PATH,
        headers=headers(),
        params=parameters(
            start_at=target.start_at.astimezone(other_zone).isoformat(),
            end_at=target.end_at.astimezone(other_zone).isoformat(),
        ),
    )
    assert response.status_code == 200 and response.json()["code"] == "AVAILABLE"
    assert response.json()["query"] == target.model_dump(mode="json")


def test_result_resource_scope_is_rechecked_after_assessment(mine_api, monkeypatch):
    client, surface, _ = mine_api
    availability = surface.queries.reservations.availability
    original = availability.assess_in_transaction

    def changed_resource(session, target):
        result = original(session, target)
        return result.model_copy(
            update={"resource": result.resource.model_copy(update={"node_id": "node-b"})}
        )

    monkeypatch.setattr(availability, "assess_in_transaction", changed_resource)
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 403 and "AVAILABLE" not in response.text


def test_window_query_is_read_only_and_does_not_preallocate_capacity(mine_api):
    client, _, state = mine_api
    engine = state[5]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL READ ONLY window contract")
    before = counts(state)

    def read_only(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    event.listen(engine, "begin", read_only)
    try:
        response = client.get(PATH, headers=headers(), params=parameters())
    finally:
        event.remove(engine, "begin", read_only)
    assert response.status_code == 200 and response.json()["code"] == "AVAILABLE"
    assert counts(state) == before
    # Querying first does not prevent another owner taking the slot.
    create(state, user="bob")
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.json()["code"] == "CONFLICT"


def test_window_query_database_timeout_is_unavailable_instead_of_available(mine_api, monkeypatch):
    client, surface, state = mine_api
    if state[5].dialect.name != "postgresql":
        pytest.skip("PostgreSQL window query budget")

    def slow_assessment(session, target):
        session.execute(text("SELECT pg_sleep(2)"))
        raise AssertionError("query timeout did not abort assessment")

    monkeypatch.setattr(
        surface.queries.reservations.availability, "assess_in_transaction", slow_assessment
    )
    response = client.get(PATH, headers=headers(), params=parameters())
    assert response.status_code == 503 and response.json()["error"]["code"] == "QUERY_UNAVAILABLE"
    assert "AVAILABLE" not in response.json()
