from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError

from aris.core.access.models import RoleBinding
from aris.core.commands.schemas import CommandRequest, ConfirmCommand
from aris.core.commands.service import CommandService
from aris.core.contracts import DomainError
from aris.core.events import DomainEvent
from aris.modules.nova.models import PolicyReviewFeedbackRow
from aris.modules.nova.policy_catalog_service import GLOBAL_SCOPE
from aris.modules.nova.policy_feedback_commands import RecordPolicyFeedbackCommand
from aris.modules.nova.policy_feedback_schemas import (
    PolicyFeedbackListQuery,
    PolicyFeedbackMaterial,
    RecordPolicyFeedback,
)
from aris.modules.nova.policy_feedback_service import PolicyFeedbackService
from tests.test_commands import command_state, context
from tests.test_policy_catalog import KEY, actor, catalog_state, reference
from tests.test_policy_review_material import finish as material_finish
from tests.test_policy_review_material import materials
from tests.test_policy_review_material import pending as material_pending

__all__ = ["command_state", "catalog_state", "materials"]


@pytest.fixture
def feedback(materials):
    original = materials[2]
    service = PolicyFeedbackService(materials[1].catalog, writing_enabled=True)
    bus = CommandService(
        original[2],
        [RecordPolicyFeedbackCommand(service)],
        confirmation_key=KEY,
        clock=original[1].clock,
    )
    return bus, service, original, materials


def request(state, **changes):
    original = state[2]
    source = state[1].catalog.exact(actor(original[3]), reference())
    values = dict(
        source=reference(),
        expected_source_fingerprint=source.fingerprint,
        kind="comment",
        body="Please explain the boundary.\n<script>plain text only</script>",
        reason="Review the proposed fixture source",
    )
    values.update(changes)
    return CommandRequest(
        command="policy.record_feedback",
        scope=GLOBAL_SCOPE,
        parameters=RecordPolicyFeedback(**values).model_dump(mode="json"),
    )


def pending(state, *, key="feedback-fixture", parameters=None):
    return state[0].execute(context(state[2][3], key=key), parameters or request(state))


def finish(state, item):
    return state[0].confirm(
        actor(state[2][3]), ConfirmCommand(command_id=item.command_id, proof=item.proof)
    )


def query(state, **changes):
    return PolicyFeedbackListQuery(
        **{
            "source": reference(),
            "expected_source_fingerprint": request(state).parameters["expected_source_fingerprint"],
            **changes,
        }
    )


def stored_count(state):
    with state[2][2]() as session:
        return session.scalar(select(func.count()).select_from(PolicyReviewFeedbackRow))


def test_feedback_confirmation_retains_the_exact_author_source_body_and_original_result(feedback):
    state = feedback
    proposal = pending(state)
    assert proposal.state == "awaiting_confirmation" and stored_count(state) == 0
    saved = finish(state, proposal)
    assert saved.state == "succeeded" and saved.result["created"]
    assert saved.result["feedback_only"] and not saved.result["approval_performed"]
    assert not saved.result["publication_performed"]
    original = state[1].list(actor(state[2][3]), query(state)).items[0]
    assert original.created_by == UUID(state[2][3]["alice"])
    assert original.command_id == proposal.command_id
    assert original.request.body == request(state).parameters["body"]
    assert original.request.kind == "comment" and original.created_at == state[2][4]["now"]
    state[1].writing_enabled = False
    replay = pending(state)
    assert replay.duplicate and replay.result == saved.result and stored_count(state) == 1
    assert state[1].list(actor(state[2][3]), query(state)).items == (original,)
    with state[2][2]() as session:
        event = session.scalars(
            select(DomainEvent).where(DomainEvent.event_type == "policy.feedback_recorded")
        ).one()
        assert event.payload["feedback_only"] and "body" not in event.payload


def test_feedback_material_binding_requires_both_the_exact_source_and_material(feedback):
    state = feedback
    material = material_finish(state[3], material_pending(state[3]))
    target = PolicyFeedbackMaterial(
        id=UUID(material.result["id"]), fingerprint=material.result["fingerprint"]
    )
    saved = finish(
        state,
        pending(
            state,
            parameters=request(state, kind="changes_requested", material=target),
        ),
    )
    assert saved.state == "succeeded"
    note = state[1].list(actor(state[2][3]), query(state)).items[0]
    assert note.request.material == target and note.request.kind == "changes_requested"
    for index, changes in enumerate(
        (
            {"expected_source_fingerprint": "f" * 64},
            {"material": target.model_copy(update={"fingerprint": "f" * 64})},
            {"material": target.model_copy(update={"id": UUID(int=1)})},
        )
    ):
        proposal = pending(
            state, key="wrong-feedback-" + str(index), parameters=request(state, **changes)
        )
        with pytest.raises(DomainError) as error:
            finish(state, proposal)
        assert error.value.code == "CONFLICT" and stored_count(state) == 1


def test_feedback_directory_pages_are_source_bound_and_current_permission_is_required(feedback):
    state = feedback
    first = finish(state, pending(state))
    second = finish(
        state,
        pending(state, key="another-feedback", parameters=request(state, body="Second comment")),
    )
    one = state[1].list(actor(state[2][3]), query(state, limit=1))
    two = state[1].list(actor(state[2][3]), query(state, limit=1, before=one.next_cursor))
    assert {str(item.id) for item in one.items + two.items} == {
        first.result["id"],
        second.result["id"],
    }
    assert two.next_cursor is None
    with pytest.raises(DomainError) as error:
        state[1].list(actor(state[2][3]), query(state, expected_source_fingerprint="f" * 64))
    assert error.value.code == "CONFLICT"
    proposal = pending(state, key="revoked-feedback")
    original_query = query(state)
    with state[2][2].begin() as session:
        session.execute(delete(RoleBinding).where(RoleBinding.user_id == state[2][3]["alice"]))
    with pytest.raises(DomainError) as error:
        finish(state, proposal)
    assert error.value.code == "PERMISSION_DENIED"
    with pytest.raises(DomainError) as error:
        state[1].list(actor(state[2][3]), original_query)
    assert error.value.code == "PERMISSION_DENIED" and stored_count(state) == 2


def test_feedback_failure_after_event_is_atomic_and_the_original_confirmation_can_retry(
    feedback, monkeypatch
):
    state = feedback
    proposal = pending(state)
    append = state[1]._append_event

    def interrupted(session, execution, value):
        append(session, execution, value)
        raise DomainError("INTERNAL_ERROR", "fixture transaction interruption", 500)

    monkeypatch.setattr(state[1], "_append_event", interrupted)
    with pytest.raises(DomainError):
        finish(state, proposal)
    assert stored_count(state) == 0
    with state[2][2]() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(DomainEvent)
                .where(DomainEvent.event_type == "policy.feedback_recorded")
            )
            == 0
        )
    monkeypatch.setattr(state[1], "_append_event", append)
    assert finish(state, proposal).state == "succeeded" and stored_count(state) == 1


def test_feedback_is_immutable_and_nonempty_history_prevents_downgrade(feedback):
    state = feedback
    finish(state, pending(state))
    engine = state[2][2].kw["bind"]
    operations = [
        "UPDATE policy_review_feedback SET kind='changes_requested'",
        "DELETE FROM policy_review_feedback",
    ]
    if engine.dialect.name == "postgresql":
        operations.append("TRUNCATE policy_review_feedback")
    original = state[1].list(actor(state[2][3]), query(state))
    for sql in operations:
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(sql))
        assert state[1].list(actor(state[2][3]), query(state)) == original
    with pytest.raises(RuntimeError, match="feedback history"):
        command.downgrade(Config("alembic.ini"), "0029")
    assert state[1].list(actor(state[2][3]), query(state)) == original
