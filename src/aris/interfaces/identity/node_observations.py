"""Input hints from public Vega facts; never infer a Slack identity or enrollment."""

from typing import Literal, Protocol

from pydantic import AwareDatetime, Field

from aris.core.access.schemas import AccessContract
from aris.modules.vega.resource_schemas import ResourceStatus


class ObservedNodeUsers(AccessContract):
    node_id: str
    state: Literal["current", "partial", "stale", "offline", "missing", "unavailable"]
    observed_at: AwareDatetime | None = None
    usernames: tuple[str, ...] = Field(default=(), max_length=256)
    unresolved_processes: int | None = Field(default=None, ge=0)


class NodeUserObservations(AccessContract):
    generated_at: AwareDatetime
    nodes: tuple[ObservedNodeUsers, ...] = Field(max_length=32)
    suggestions_only: Literal[True] = True


class ResourceStatusReader(Protocol):
    def resource_status(self) -> ResourceStatus: ...


class NodeUserObservationProvider(Protocol):
    def read(self, node_ids: tuple[str, ...]) -> NodeUserObservations: ...


class VegaNodeUserObservations:
    def __init__(self, reader: ResourceStatusReader) -> None:
        self.reader = reader

    def read(self, node_ids: tuple[str, ...]) -> NodeUserObservations:
        facts = self.reader.resource_status()
        by_id = {value.node.node_id: value for value in facts.nodes}
        nodes = []
        for node_id in node_ids:
            value = by_id.get(node_id)
            state: Literal["current", "partial", "stale", "offline", "missing", "unavailable"]
            state = "missing"
            usernames: tuple[str, ...] = ()
            unresolved = None
            observed_at = value.compute_observed_at if value is not None else None
            if value is not None:
                if value.node.status == "OFFLINE":
                    state = "offline"
                elif value.node.status == "STALE" or value.compute_freshness == "STALE":
                    state = "stale"
                elif value.compute_freshness != "CURRENT" or value.compute is None:
                    state = "missing"
                elif value.compute.quality != "ok":
                    state = "unavailable"
                elif value.compute.reported_processes == 0:
                    state, unresolved = "current", 0
                elif (
                    value.compute.ownership is None
                    or value.compute.ownership.quality == "unsupported"
                ):
                    state = "unavailable"
                else:
                    owners = value.compute.ownership
                    usernames = tuple(
                        sorted({user.username for user in owners.users if user.username})
                    )
                    unresolved = owners.unresolved_processes + sum(
                        user.reported_processes for user in owners.users if user.username is None
                    )
                    state = "current" if owners.quality == "ok" else "partial"
            nodes.append(
                ObservedNodeUsers(
                    node_id=node_id,
                    state=state,
                    observed_at=observed_at,
                    usernames=usernames,
                    unresolved_processes=unresolved,
                )
            )
        return NodeUserObservations(generated_at=facts.generated_at, nodes=tuple(nodes))
