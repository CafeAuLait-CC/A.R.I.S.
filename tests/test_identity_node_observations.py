from sqlalchemy import delete, select

from aris.core.access.models import NodeUserMapping, RoleBinding
from aris.core.db import session_factory
from aris.interfaces.identity.node_observations import (
    NodeUserObservations,
    ObservedNodeUsers,
    VegaNodeUserObservations,
)
from aris.modules.vega.resource_schemas import ResourceStatus
from aris.modules.vega.service import VegaService
from tests.conftest import READ_TOKEN
from tests.test_administration import NOW, admin_state
from tests.test_historical_usage_facts import owner, ownership
from tests.test_identity_admin_api import headers, identity_api
from tests.test_resource_telemetry import put, resource_batch

__all__ = ["admin_state", "identity_api"]
PATH = "/internal/identity/node-users/query"


def test_vega_observations_offer_only_current_named_compute_users(client, settings):
    reader = VegaService(session_factory(client.app.state.engine), settings)
    provider = VegaNodeUserObservations(reader)
    assert (
        put(client, ownership(resource_batch(), [owner(username="fixture-linux-user")])).status_code
        == 200
    )
    result = provider.read(("node-a", "node-b"))
    assert result.suggestions_only
    assert result.nodes[0].state == "current"
    assert result.nodes[0].usernames == ("fixture-linux-user",)
    assert result.nodes[1].state == "missing" and result.nodes[1].usernames == ()
    assert (
        put(
            client, ownership(resource_batch(seq=3), [owner(username=None)], quality="partial")
        ).status_code
        == 200
    )
    unresolved = provider.read(("node-a",)).nodes[0]
    assert unresolved.state == "partial" and unresolved.usernames == ()
    assert unresolved.unresolved_processes == 1


def test_stale_offline_and_failed_observations_never_offer_old_names(client, settings):
    assert (
        put(client, ownership(resource_batch(), [owner(username="fixture-linux-user")])).status_code
        == 200
    )
    facts = VegaService(session_factory(client.app.state.engine), settings).resource_status()

    class FrozenReader:
        def resource_status(self):
            return current

    for state, node_status, freshness in (
        ("offline", "OFFLINE", "CURRENT"),
        ("stale", "STALE", "CURRENT"),
        ("stale", "ONLINE", "STALE"),
    ):
        node = facts.nodes[0].model_copy(
            update={
                "node": facts.nodes[0].node.model_copy(update={"status": node_status}),
                "compute_freshness": freshness,
            }
        )
        current = ResourceStatus(generated_at=facts.generated_at, nodes=[node])
        result = VegaNodeUserObservations(FrozenReader()).read(("node-a",)).nodes[0]
        assert (
            result.state == state and result.usernames == () and result.unresolved_processes is None
        )


class FixtureObservations:
    def __init__(self):
        self.calls = []
        self.side_effect = None

    def read(self, node_ids):
        self.calls.append(node_ids)
        if self.side_effect:
            self.side_effect()
        return NodeUserObservations(
            generated_at=NOW,
            nodes=tuple(
                ObservedNodeUsers(
                    node_id=node,
                    state="current",
                    observed_at=NOW,
                    usernames=("fixture-linux-user",),
                    unresolved_processes=0,
                )
                for node in node_ids
            ),
        )


def test_identity_username_hints_require_admin_access_and_do_not_enroll_anyone(identity_api):
    client, graph, _, state, _ = identity_api
    provider = FixtureObservations()
    graph.identity_admin.observations = provider
    denied = client.post(PATH, json={}, headers=headers(READ_TOKEN))
    assert denied.status_code == 401 and provider.calls == []
    result = client.post(PATH, json={}, headers=headers())
    assert result.status_code == 200
    assert provider.calls == [("node-a", "node-b")]
    assert result.json()["suggestions_only"] and result.json()["nodes"][0]["usernames"] == [
        "fixture-linux-user"
    ]
    with state[1]() as session:
        assert list(session.scalars(select(NodeUserMapping))) == []
    assert client.post(PATH, json={"node_ids": ["outside"]}, headers=headers()).status_code == 422


def test_observation_result_rechecks_current_identity_permission(identity_api):
    client, graph, _, state, _ = identity_api
    provider = FixtureObservations()

    def revoked():
        with state[1].begin() as session:
            session.execute(delete(RoleBinding))

    provider.side_effect = revoked
    graph.identity_admin.observations = provider
    result = client.post(PATH, json={}, headers=headers())
    assert result.status_code == 403 and "fixture-linux-user" not in result.text


def test_m2_composes_read_only_observations_without_touching_slack_or_agent(identity_api):
    client, graph, directory, _, _ = identity_api
    assert isinstance(graph.identity_admin.observations, VegaNodeUserObservations)
    result = client.post(PATH, json={}, headers=headers())
    assert result.status_code == 200
    assert [node["node_id"] for node in result.json()["nodes"]] == ["node-a", "node-b"]
    assert all(
        node["state"] in ("stale", "offline", "missing") and not node["usernames"]
        for node in result.json()["nodes"]
    )
    assert directory.calls == []
