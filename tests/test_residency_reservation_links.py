import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import event, select, text

from aris.core.access.admin_schemas import BindRole, DefineRole
from aris.core.access.schemas import AccessScope
from aris.core.commands.schemas import ActorContext, CommandContext, CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.modules.lyra.reservation_links import (
    ExplicitReservationReference,
    ReservationLinkRequest,
    ResidencyReservationAnalysis,
    ResidencyReservationService,
)
from aris.modules.nova.availability import AvailabilityService
from aris.modules.nova.commands import CreateReservationCommand
from aris.modules.nova.models import ReservationEvent
from aris.modules.nova.reservation_service import ReservationService
from aris.modules.nova.usage_link_service import ReservationUsageLinkService
from aris.modules.vega.models import GpuDevice, Node
from aris.modules.vega.target_service import VegaTargetService
from tests.test_administration import NOW, admin_state, batch, context
from tests.test_availability import RULES, calendar
from tests.test_mapping_history import assign, counts, history_state, remove
from tests.test_residency_attribution import analyze, source

__all__ = ["admin_state", "history_state"]


@pytest.fixture
def linked_state(history_state, settings):
    admin, sessions, engine, actor, users, clock, _ = history_state
    assign(history_state, 0)
    admin.apply(
        context(actor),
        batch(
            DefineRole(name="fixture-residency-creator", permissions=("reservation.create",)),
            BindRole(
                user_id=users[0],
                role_name="fixture-residency-creator",
                scope=AccessScope(type="node", id="node-a"),
                present=True,
            ),
        ),
    )
    with sessions.begin() as session:
        node = session.get(Node, "node-a")
        for field in (
            "observed_at",
            "received_at",
            "telemetry_at",
            "telemetry_received_at",
            "collector_health_at",
        ):
            setattr(node, field, NOW)
        node.telemetry_seq, node.collector_status = 1, "ok"
        session.add(
            GpuDevice(
                gpu_uuid="GPU-a",
                node_id="node-a",
                index=0,
                model="Fixture GPU",
                memory_total_mib=49152,
                memory_used_mib=0,
                utilization_percent=0,
                temperature_c=35,
                present=True,
            )
        )
    targets = VegaTargetService(sessions, settings, clock=lambda: clock["now"])
    availability = AvailabilityService(
        sessions,
        targets,
        rules=RULES,
        maintenance=(calendar(gpu="GPU-a", start=NOW, end=NOW + timedelta(days=8)),),
        clock=lambda: clock["now"],
    )
    commands = CommandService(
        sessions,
        [CreateReservationCommand(ReservationService(sessions, availability))],
        confirmation_key=SecretStr("fixture-residency-reservation-confirmation-key"),
        clock=lambda: clock["now"],
    )
    pending = commands.execute(
        CommandContext(
            actor_id=users[0],
            request_id="fixture-residency-create",
            idempotency_key="fixture-residency-create",
        ),
        CommandRequest(
            command="reservation.create",
            scope={"type": "node", "id": "node-a"},
            parameters=dict(
                gpu_uuid="GPU-a",
                start_at=(NOW + timedelta(seconds=60)).isoformat(),
                end_at=(NOW + timedelta(seconds=180)).isoformat(),
                purpose="Private residency link fixture",
            ),
        ),
    )
    created = commands.confirm(
        ActorContext(actor_id=users[0], request_id="fixture-residency-confirm"),
        ConfirmCommand(command_id=pending.command_id, proof=pending.proof),
    )
    reservation_id = created.result["reservation"]["id"]
    attribution = analyze(history_state, [source(90), source(105, 3), source(120, 5)])
    reader = ReservationUsageLinkService(sessions, clock=lambda: clock["now"])
    return history_state, reservation_id, attribution, reader


def request(state, *references, attribution=None):
    return ReservationLinkRequest(attribution=attribution or state[2], references=references)


def reference(state, index=0, **changes):
    fields = dict(session_index=index, reservation_id=state[1], expected_version=1)
    fields.update(changes)
    return ExplicitReservationReference(**fields)


class NoReservationReads:
    def resolve_in_transaction(self, session, query):
        raise AssertionError("unexpected reservation lookup")


def service(state, reader=None):
    return ResidencyReservationService(state[0][1], reader or state[3])


def test_real_identity_residency_and_confirmed_reservation_keep_independent_evidence(linked_state):
    state = linked_state
    before = counts(state[0][1])
    result = service(state).link(request(state, reference(state)))
    row = result.links[0]
    assert row.state == "linked_candidate" and row.association.state == "matched"
    assert row.association.query.owner_id == state[2].attributions[0].identity.user_id
    assert row.association.query.start_at == NOW + timedelta(seconds=90)
    assert result.request.attribution.residency.sessions[0].observed_microseconds == 30000000
    assert counts(state[0][1]) == before and not result.formal_usage_available
    assert "Private residency link fixture" not in result.model_dump_json()


def test_no_explicit_reference_is_unlinked_without_claiming_unreserved(linked_state):
    result = service(linked_state, NoReservationReads()).link(request(linked_state))
    assert result.links[0].state == "not_provided" and result.links[0].association is None
    assert result.links[0].reason == "NO_REFERENCE"


def test_multiple_explicit_references_require_review_without_guessing_or_splitting(linked_state):
    state = linked_state
    result = service(state, NoReservationReads()).link(
        request(state, reference(state), reference(state, reservation_id=uuid4()))
    )
    assert (
        result.links[0].state == "needs_review" and result.links[0].reason == "MULTIPLE_REFERENCES"
    )
    assert result.request.attribution == state[2]


@pytest.mark.parametrize("kind", ["duplicate", "outside"])
def test_invalid_reference_targets_are_rejected_before_database_queries(linked_state, kind):
    state = linked_state
    with pytest.raises(ValidationError):
        request(
            state,
            *(
                (reference(state), reference(state))
                if kind == "duplicate"
                else (reference(state, index=1),)
            ),
        )


def test_short_residency_is_not_promoted_by_a_reservation(linked_state):
    state = linked_state
    attribution = analyze(state[0], [source(90), source(105, 3)])
    result = service(state, NoReservationReads()).link(
        request(state, reference(state), attribution=attribution)
    )
    assert (
        result.links[0].state == "not_evaluated"
        and result.links[0].reason == "INSUFFICIENT_EVIDENCE"
    )


def test_missing_identity_is_not_inferred_from_the_reservation_owner(linked_state):
    state = linked_state
    remove(state[0], 80)
    attribution = analyze(state[0], [source(90), source(105, 3), source(120, 5)])
    result = service(state, NoReservationReads()).link(
        request(state, reference(state), attribution=attribution)
    )
    assert (
        result.links[0].state == "not_evaluated" and result.links[0].reason == "IDENTITY_UNRESOLVED"
    )
    assert result.request.attribution.attributions[0].identity.user_id is None


def test_other_owner_does_not_change_attribution_or_hide_observed_use(linked_state):
    state = linked_state
    assign(state[0], 50, 1, expected=state[0][4][0])
    attribution = analyze(state[0], [source(90), source(105, 3), source(120, 5)])
    result = service(state).link(request(state, reference(state), attribution=attribution))
    assert result.links[0].reason == "RESERVATION_NOT_MATCHED"
    assert result.links[0].association.reason == "OWNER_OR_RESOURCE_DIFFERS"
    assert result.request.attribution == attribution


def test_unknown_reservation_stays_unknown_and_outdated_version_conflicts(linked_state):
    state = linked_state
    result = service(state).link(request(state, reference(state, reservation_id=uuid4())))
    assert result.links[0].reason == "RESERVATION_UNKNOWN"
    with pytest.raises(DomainError) as error:
        service(state).link(request(state, reference(state, expected_version=2)))
    assert error.value.code == "CONFLICT"


def test_response_cannot_omit_a_source_or_upgrade_an_unlinked_candidate(linked_state):
    result = service(linked_state, NoReservationReads()).link(request(linked_state))
    body = result.model_dump(mode="json")
    body["links"] = []
    with pytest.raises(ValidationError):
        ResidencyReservationAnalysis.model_validate(body)
    body = result.model_dump(mode="json")
    body["links"][0].update(state="linked_candidate", reason="EXPLICIT_RESERVATION_MATCH")
    with pytest.raises(ValidationError):
        ResidencyReservationAnalysis.model_validate(body)


def test_wrong_provider_query_cannot_be_attached_to_the_original_residency(linked_state):
    state = linked_state

    class WrongQuery:
        def resolve_in_transaction(self, session, query):
            result = state[3].resolve_in_transaction(session, query)
            return result.model_copy(
                update={"query": query.model_copy(update={"gpu_uuid": "GPU-other"})}
            )

    with pytest.raises(ValidationError, match="original source window"):
        service(state, WrongQuery()).link(request(state, reference(state)))


def test_postgresql_real_link_is_read_only_and_has_a_bounded_query_budget(linked_state):
    state = linked_state
    if state[0][2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL cross-domain reservation link READ ONLY and budget")
    statements = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    with state[0][1].begin() as session:
        session.execute(text("SET TRANSACTION READ ONLY"))
        event.listen(state[0][2], "before_cursor_execute", record)
        try:
            result = service(state).link_in_transaction(session, request(state, reference(state)))
        finally:
            event.remove(state[0][2], "before_cursor_execute", record)
    assert result.links[0].state == "linked_candidate" and len(statements) == 4


def test_postgresql_change_after_first_result_prevents_partial_batch_success(linked_state):
    state = linked_state
    if state[0][2].dialect.name != "postgresql":
        pytest.skip("PostgreSQL final reservation evidence recheck after concurrent change")
    calls = []

    class ChangedSource:
        def resolve_in_transaction(self, session, query):
            result = state[3].resolve_in_transaction(session, query)
            if not calls:
                calls.append(True)
                with state[0][1].begin() as other:
                    history = other.scalars(select(ReservationEvent)).one()
                    body = copy.deepcopy(history.payload)
                    body["request_id"] = "changed-between-batch-reads"
                    history.payload = body
            return result

    with pytest.raises(DomainError) as error:
        service(state, ChangedSource()).link(request(state, reference(state)))
    assert error.value.code == "CONFLICT"
