import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.access.admin_schemas import IdentityNamespace
from aris.core.access.schemas import AccessContract, AccessRequest, AccessScope, ExternalSubject
from aris.core.access.service import AccessService
from aris.core.commands.budgets import transaction_budget
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.interfaces.reservations.schemas import ReservationQueryLimits
from aris.interfaces.slack.config import SlackSettings
from aris.modules.nova.occupancy import ReservationOccupancyService
from aris.modules.nova.occupancy_schemas import CurrentReservationQuery
from aris.modules.vega.resource_schemas import ResourceStatus
from aris.modules.vega.target_schemas import GpuLabelQuery
from aris.modules.vega.target_service import VegaTargetService

logger = logging.getLogger(__name__)


class HomeGpuReservation(AccessContract):
    node_id: str
    gpu_uuid: str
    state: Literal["unoccupied", "occupied", "unknown"]


class HomeReservationSnapshot(AccessContract):
    as_of: AwareDatetime
    gpus: tuple[HomeGpuReservation, ...] = Field(default=(), max_length=100)

    def state_for(self, node_id: str, gpu_uuid: str) -> str:
        return next(
            (
                item.state
                for item in self.gpus
                if (item.node_id, item.gpu_uuid) == (node_id, gpu_uuid)
            ),
            "unknown",
        )


class HomeReservationSource:
    """Current authorized aggregate facts; never user identity or booking approval."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        namespace: IdentityNamespace,
        targets: VegaTargetService,
        reservations: ReservationOccupancyService,
        limits: ReservationQueryLimits,
        clock: Callable[[], datetime],
    ) -> None:
        self.sessions, self.namespace, self.targets = sessions, namespace, targets
        self.reservations, self.limits, self.clock = reservations, limits, clock
        self.access = AccessService(sessions)

    def validate_configuration(self, config: SlackSettings) -> None:
        if (
            not config.configured
            or self.namespace.provider != "slack"
            or self.namespace.namespace != config.team_id
        ):
            raise ValueError("Home reservations require the configured workspace")

    def snapshot(
        self, subject: ExternalSubject, resources: ResourceStatus
    ) -> HomeReservationSnapshot:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Home reservation clock requires a timezone")
        unknown = HomeReservationSnapshot(as_of=utc(now))
        if (subject.provider, subject.namespace) != (
            self.namespace.provider,
            self.namespace.namespace,
        ):
            return unknown
        all_targets = [
            (entry.node.node_id, gpu.gpu_uuid)
            for entry in resources.nodes
            for gpu in entry.node.gpus
        ]
        counts = Counter(gpu for _, gpu in all_targets)
        selected = {gpu: node for node, gpu in all_targets if counts[gpu] == 1}
        selected = dict(list(selected.items())[:100])
        if not selected:
            return unknown
        try:
            with self.sessions() as session:
                transaction_budget(
                    session,
                    self.limits.lock_timeout_ms,
                    self.limits.statement_timeout_ms,
                    self.limits.transaction_timeout_ms,
                )
                principal = self.access.resolve_external_in_transaction(session, subject)
                if principal is None:
                    return unknown
                labels = self.targets.labels_in_transaction(
                    session, GpuLabelQuery(gpu_uuids=tuple(selected))
                )
                verified = {
                    label.gpu_uuid: label.node_id
                    for label in labels
                    if label.gpu_uuid in selected and label.node_id == selected[label.gpu_uuid]
                }
                allowed = self._allowed_nodes(session, principal.user_id, set(verified.values()))
                gpu_ids = tuple(gpu for gpu, node in verified.items() if node in allowed)
                if not gpu_ids:
                    return unknown
                facts = self.reservations.current_in_transaction(
                    session, CurrentReservationQuery(gpu_uuids=gpu_ids)
                )
                current = self.access.resolve_external_in_transaction(session, subject)
                if current is None or current.user_id != principal.user_id:
                    return unknown
                final = self.targets.labels_in_transaction(
                    session, GpuLabelQuery(gpu_uuids=gpu_ids)
                )
                stable = {
                    label.gpu_uuid
                    for label in final
                    if label.gpu_uuid in verified and label.node_id == verified[label.gpu_uuid]
                }
                allowed = self._allowed_nodes(session, principal.user_id, allowed)
                return HomeReservationSnapshot(
                    as_of=facts.as_of,
                    gpus=tuple(
                        HomeGpuReservation(
                            node_id=verified[item.gpu_uuid],
                            gpu_uuid=item.gpu_uuid,
                            state=item.state,
                        )
                        for item in facts.gpus
                        if item.gpu_uuid in stable and verified[item.gpu_uuid] in allowed
                    ),
                )
        except (DBAPIError, DomainError) as error:
            logger.warning(
                "home_reservation_query_unavailable failure_type=%s", type(error).__name__
            )
            return unknown

    def _allowed_nodes(self, session: Session, user_id: UUID, nodes: set[str]) -> set[str]:
        allowed = set()
        for node in sorted(nodes):
            try:
                self.access.require_in_transaction(
                    session,
                    AccessRequest(
                        user_id=user_id,
                        permission="reservation.view_current",
                        scope=AccessScope(type="node", id=node),
                    ),
                )
                allowed.add(node)
            except DomainError as error:
                if error.code != "PERMISSION_DENIED":
                    raise
        return allowed
