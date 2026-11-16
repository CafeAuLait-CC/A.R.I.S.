from datetime import timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, update

from aris.core.access.admin_schemas import (
    BindRole,
    DefineRole,
    RegisterIdentity,
    RegistrationMapping,
)
from aris.core.access.administration import AccessAdministrationService
from aris.core.access.schemas import AccessScope, NodeUserLookup
from aris.core.access.service import AccessService
from aris.core.commands.models import CommandRecord
from aris.core.commands.schemas import ActorContext, CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent, OutboxEntry
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.availability_schemas import (
    AvailabilityWindow,
    BasicReservationRules,
    MaintenanceCoverage,
)
from aris.modules.nova.commands import (
    CancelReservationCommand,
    CreateReservationCommand,
    ExtendReservationCommand,
    ReleaseReservationCommand,
)
from aris.modules.nova.extension import ReservationExtensionService
from aris.modules.nova.models import Reservation, ReservationEvent
from aris.modules.nova.reservation_schemas import MineQuery
from aris.modules.nova.reservation_service import ReservationService
from aris.modules.vega.models import GpuDevice, Node
from aris.modules.vega.scope_service import VegaNodeScopes
from aris.modules.vega.target_service import VegaTargetService
from tests.test_administration import (
    KEY,
    NAMESPACE,
    NOW,
    PROFILE,
    batch,
    bootstrap_request,
    context,
    subject,
)
from tests.test_administration import admin_state as admin_state


def test_identity_grants_confirmed_reservations_and_revocation_form_one_internal_workflow(
    admin_state, settings
):
    _, sessions, _ = admin_state
    clock = {"now": NOW}
    permissions = (
        "reservation.create",
        "reservation.extend",
        "reservation.mine",
        "reservation.cancel_own",
        "reservation.release_own",
        "reservation.extend_own",
    )
    admin = AccessAdministrationService(
        sessions,
        NAMESPACE,
        operator_key=KEY,
        permission_catalog=permissions,
        scopes=VegaNodeScopes(sessions),
        clock=lambda: clock["now"],
    )
    operator = admin.bootstrap(bootstrap_request()).user_id
    registered = admin.apply(
        context(operator),
        batch(
            RegisterIdentity(
                subject=subject("UMEMBER"),
                profile=PROFILE,
                mappings=(
                    RegistrationMapping(node_id="node-a", os_username="fixture_a"),
                    RegistrationMapping(node_id="node-b", os_username="different_b"),
                ),
            )
        ),
    )
    member = registered.outcomes[0].entity_id
    node_a, global_scope = AccessScope(type="node", id="node-a"), AccessScope(type="global", id="*")
    personal = permissions[2:]
    admin.apply(
        context(operator),
        batch(
            DefineRole(name="resource_member", permissions=permissions[:2]),
            DefineRole(name="personal_member", permissions=personal),
            BindRole(user_id=member, role_name="resource_member", scope=node_a, present=True),
            BindRole(user_id=member, role_name="personal_member", scope=global_scope, present=True),
        ),
    )
    # Hardware reports are synthetic fixtures; there are no collector or Slack requests.
    with sessions.begin() as session:
        session.execute(
            update(Node).values(
                observed_at=NOW,
                received_at=NOW,
                last_seq=2,
                telemetry_at=NOW,
                telemetry_received_at=NOW,
                telemetry_seq=1,
                collector_status="ok",
                collector_health_at=NOW,
            )
        )
        for node in ("node-a", "node-b"):
            session.add(
                GpuDevice(
                    gpu_uuid="GPU-workflow-" + node[-1],
                    node_id=node,
                    index=0,
                    model="Fixture GPU",
                    memory_total_mib=49152,
                    memory_used_mib=30000,
                    utilization_percent=99,
                    temperature_c=70,
                    present=True,
                )
            )
    targets = VegaTargetService(sessions, settings, clock=lambda: clock["now"])
    rules = BasicReservationRules(
        max_duration_seconds=14400, min_notice_seconds=60, max_advance_seconds=604800
    )
    calendars = tuple(
        MaintenanceCoverage(
            gpu_uuid="GPU-workflow-" + suffix,
            coverage=AvailabilityWindow(start_at=NOW, end_at=NOW + timedelta(days=8)),
            windows=(),
        )
        for suffix in ("a", "b")
    )
    available = AvailabilityService(
        sessions, targets, rules=rules, maintenance=calendars, clock=lambda: clock["now"]
    )
    reservations = ReservationService(sessions, available)
    extensions = ReservationExtensionService(sessions, available)
    commands = CommandService(
        sessions,
        [
            CreateReservationCommand(reservations),
            CancelReservationCommand(reservations),
            ReleaseReservationCommand(reservations),
            ExtendReservationCommand(extensions),
        ],
        confirmation_key=SecretStr("fixture-internal-workflow-confirmation-123456789"),
        clock=lambda: clock["now"],
    )
    access = AccessService(sessions)
    assert str(access.resolve_external(subject("UMEMBER")).user_id) == member
    assert (
        str(
            access.resolve_node_user(
                NodeUserLookup(node_id="node-b", os_username="different_b")
            ).user_id
        )
        == member
    )

    def intent(actor, key, name, parameters, scope):
        return commands.execute(
            CommandContext(actor_id=actor, request_id="submit-" + key, idempotency_key=key),
            CommandRequest(command=name, parameters=parameters, scope=scope),
        )

    def confirmation(actor, key, pending):
        return commands.confirm(
            ActorContext(actor_id=actor, request_id="confirm-" + key),
            ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
        )

    def perform(key, name, parameters, scope):
        pending = intent(member, key, name, parameters, scope)
        assert pending.state == "awaiting_confirmation"
        return confirmation(member, key, pending)

    create_input = {
        "gpu_uuid": "GPU-workflow-a",
        "start_at": (NOW + timedelta(hours=1)).isoformat(),
        "end_at": (NOW + timedelta(hours=2)).isoformat(),
        "purpose": "Fixture workflow",
    }
    # Identity administrators gain no reservation rights by role name; mappings
    # likewise identify the member on node-b without granting node-b access.
    for actor, parameters, scope in (
        (operator, create_input, node_a),
        (
            member,
            {**create_input, "gpu_uuid": "GPU-workflow-b"},
            AccessScope(type="node", id="node-b"),
        ),
    ):
        with pytest.raises(DomainError) as denied:
            intent(
                actor, "denied-" + str(actor) + scope.id, "reservation.create", parameters, scope
            )
        assert denied.value.code == "PERMISSION_DENIED"
    first = perform("create-first", "reservation.create", create_input, node_a)
    first_id = first.result["reservation"]["id"]
    extended = perform(
        "extend-first",
        "reservation.extend",
        {
            "reservation_id": first_id,
            "expected_version": 1,
            "new_end_at": (NOW + timedelta(hours=3)).isoformat(),
            "reason": "More fixture time",
        },
        global_scope,
    )
    assert extended.result["reservation"]["version"] == 2
    cancelled = perform(
        "cancel-first",
        "reservation.cancel",
        {"reservation_id": first_id, "expected_version": 2, "reason": "Fixture plan changed"},
        global_scope,
    )
    assert cancelled.result["status"] == "cancelled" and cancelled.result["version"] == 3
    second = perform("create-second", "reservation.create", create_input, node_a)
    second_id = second.result["reservation"]["id"]
    clock["now"] = NOW + timedelta(hours=1)
    release_input = {
        "reservation_id": second_id,
        "expected_version": 1,
        "reason": "Fixture allocation finished",
    }
    waiting = intent(member, "release-second", "reservation.release", release_input, global_scope)
    admin.apply(
        context(operator),
        batch(
            BindRole(user_id=member, role_name="personal_member", scope=global_scope, present=False)
        ),
    )
    assert access.resolve_external(subject("UMEMBER")) is not None
    with pytest.raises(DomainError) as denied:
        confirmation(member, "release-second", waiting)
    assert denied.value.code == "PERMISSION_DENIED"
    admin.apply(
        context(operator),
        batch(
            BindRole(user_id=member, role_name="personal_member", scope=global_scope, present=True)
        ),
    )
    released = confirmation(member, "release-second", waiting)
    assert released.result["reservation"]["status"] == "completed"
    replay = intent(member, "release-second", "reservation.release", release_input, global_scope)
    assert replay.duplicate and replay.result == released.result
    assert (
        len(
            reservations.mine(
                ActorContext(actor_id=member, request_id="mine-final"), MineQuery()
            ).reservations
        )
        == 2
    )
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(CommandRecord)) == 5
        assert session.scalar(select(func.count()).select_from(Reservation)) == 2
        assert session.scalar(select(func.count()).select_from(ReservationEvent)) == 5
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.aggregate_type == "reservation")
            )
            == 5
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(OutboxEntry)
                .join(DomainEvent)
                .where(DomainEvent.aggregate_type == "reservation")
            )
            == 5
        )
        assert set(session.scalars(select(GpuDevice.utilization_percent))) == {99}
        assert set(session.scalars(select(Reservation.status))) == {"cancelled", "completed"}
