"""Owner-selected native forms; all changes still require canonical command confirmation."""

import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import (
    ExternalCommandActor,
    ExternalCommandSubmit,
    UserCommandInput,
)
from aris.core.commands.schemas import CommandReview
from aris.core.contracts import DomainError
from aris.interfaces.reservations.schemas import OwnedReservationDetail
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.client import ModalPublisher
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.reservation_forms import PurposeInput, TimeInput, preparing_form
from aris.interfaces.slack.schemas import Interaction, ModalHandle, SlackPayload, SlackReply
from aris.interfaces.slack.user_commands import CommandReviewPresenter
from aris.modules.nova.reservation_schemas import (
    CancelReservation,
    ExtendReservation,
    ReleaseReservation,
)

logger = logging.getLogger(__name__)
CHANGE_FORM_CALLBACK = "aris.reservation.change.form"
CHANGE_ACTION_PREFIX = "aris.reservation.change."
CHANGE_OPERATIONS = frozenset({"cancel", "release", "extend"})


class ChangeSelection(SlackPayload):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    reservation_id: UUID
    operation: Literal["cancel", "release", "extend"]


class ChangeFormSource(SlackPayload):
    type: Literal["modal"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    callback_id: Literal["aris.reservation.change.form"]
    private_metadata: str = Field(min_length=1, max_length=1024, repr=False)
    state: dict[str, Any] | None = Field(default=None, repr=False)


def change_form(selection: ChangeSelection, detail: OwnedReservationDetail) -> dict[str, Any]:
    reservation, label = detail.reservation, detail.resource_label
    resource = (
        f"{label.node_id} · GPU {label.index} · {label.model}"
        if label
        else "GPU information unavailable"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": plain(resource)},
        {"type": "section", "text": plain("Purpose\n" + reservation.purpose)},
        {
            "type": "context",
            "elements": [
                markdown(
                    f"Scheduled: {slack_time(reservation.start_at)} → "
                    f"{slack_time(reservation.end_at)}"
                )
            ],
        },
        {
            "type": "context",
            "elements": [
                plain(
                    "The current plan and your permissions will be checked again "
                    "before confirmation."
                )
            ],
        },
    ]
    if selection.operation == "extend":
        blocks.append(
            {
                "type": "input",
                "block_id": "end",
                "label": plain("New scheduled end"),
                "element": {
                    "type": "datetimepicker",
                    "action_id": "aris.change.end",
                    "initial_date_time": int(reservation.end_at.timestamp()) + 3600,
                },
            }
        )
    blocks.append(
        {
            "type": "input",
            "block_id": "reason",
            "label": plain("Reason"),
            "element": {
                "type": "plain_text_input",
                "action_id": "aris.change.reason",
                "max_length": 256,
                "multiline": True,
            },
        }
    )
    return {
        "type": "modal",
        "callback_id": CHANGE_FORM_CALLBACK,
        "title": plain(selection.operation.capitalize() + " reservation"),
        "submit": plain("Review request"),
        "close": plain("Back"),
        "notify_on_close": True,
        "private_metadata": selection.model_dump_json(),
        "blocks": blocks,
    }


def submitted_change(
    source: ChangeFormSource, selection: ChangeSelection
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    values = source.state.get("values") if source.state else None
    fields = {"reason", "end"} if selection.operation == "extend" else {"reason"}
    if not isinstance(values, dict) or set(values) != fields:
        raise DomainError("VALIDATION_ERROR", "Reservation change form fields differ", 422)
    parsed: dict[str, Any] = {"reservation_id": str(selection.reservation_id)}
    errors = {}
    for field in fields:
        entry = values[field]
        if not isinstance(entry, dict) or set(entry) != {"aris.change." + field}:
            raise DomainError("VALIDATION_ERROR", "Reservation change actions differ", 422)
        try:
            if field == "reason":
                parsed["reason"] = PurposeInput.model_validate(entry["aris.change.reason"]).value
            else:
                stamp = TimeInput.model_validate(entry["aris.change.end"]).selected_date_time
                parsed["new_end_at"] = TypeAdapter(AwareDatetime).dump_python(
                    datetime.fromtimestamp(stamp, UTC), mode="json"
                )
        except (ValidationError, ValueError, OverflowError):
            errors[field] = (
                "Enter a reason (1–256 characters)."
                if field == "reason"
                else "Select a valid date and time."
            )
    return (None, errors) if errors else (parsed, {})


class SlackReservationChanges:
    def __init__(
        self,
        gateway: UserCommandGateway,
        publisher: ModalPublisher,
        presenter: CommandReviewPresenter,
        queries: PersonalReservationQueries,
    ) -> None:
        self.gateway, self.publisher, self.presenter, self.queries = (
            gateway,
            publisher,
            presenter,
            queries,
        )
        self.operations = frozenset(
            operation
            for operation in CHANGE_OPERATIONS
            if "reservation." + operation in gateway.allowed
        )
        if not self.operations or any(
            not gateway.definition("reservation." + op).requires_confirmation
            for op in self.operations
        ):
            raise ValueError("owner change forms require explicit confirmed commands")

    @staticmethod
    def handles(interaction: Interaction, payload: dict[str, Any]) -> bool:
        view = payload.get("view")
        return any(
            action.action_id.startswith(CHANGE_ACTION_PREFIX) for action in interaction.actions
        ) or (isinstance(view, dict) and view.get("callback_id") == CHANGE_FORM_CALLBACK)

    def interact(
        self,
        body: bytes,
        payload: dict[str, Any],
        interaction: Interaction,
        request_id: str,
        received: float,
    ) -> SlackReply:
        actor = ExternalCommandActor(
            subject={
                "provider": "slack",
                "namespace": interaction.team.id,
                "external_id": interaction.user.id,
            },
            request_id=request_id,
        )
        if interaction.type == "block_actions":
            if len(interaction.actions) != 1 or interaction.trigger_id is None:
                raise DomainError(
                    "VALIDATION_ERROR", "Owner action requires one button and trigger", 422
                )
            action = interaction.actions[0]
            operation = action.action_id.removeprefix(CHANGE_ACTION_PREFIX)
            if operation not in self.operations or action.value is None:
                raise DomainError("PERMISSION_DENIED", "Owner action is not exposed", 403)
            selection = ChangeSelection.model_validate(
                {"reservation_id": action.value, "operation": operation}
            )
            return SlackReply(
                {"ok": True},
                after_ack=partial(
                    self._open,
                    interaction.trigger_id.get_secret_value(),
                    actor,
                    selection,
                    received,
                ),
            )
        source = ChangeFormSource.model_validate(payload.get("view"))
        if interaction.type == "view_closed":
            return SlackReply({"ok": True})
        if interaction.type != "view_submission" or interaction.actions:
            raise DomainError("VALIDATION_ERROR", "Unsupported owner change interaction", 422)
        selection = ChangeSelection.model_validate_json(source.private_metadata)
        if selection.operation not in self.operations:
            raise DomainError("PERMISSION_DENIED", "Owner action is not exposed", 403)
        parameters, errors = submitted_change(source, selection)
        if errors:
            return SlackReply({"response_action": "errors", "errors": errors})
        assert parameters is not None
        key = hashlib.sha256(
            json.dumps([actor.subject.model_dump(), source.id], sort_keys=True).encode()
        ).hexdigest()
        context = ExternalCommandSubmit(
            **actor.model_dump(),
            receipt_key="slack.change.body:" + hashlib.sha256(body).hexdigest(),
            idempotency_key="slack.change.form:" + key,
        )
        return SlackReply(
            {
                "response_action": "update",
                "view": preparing_form(
                    "Preparing the current plan for review. The reservation has not changed."
                ),
            },
            after_ack=partial(
                self._prepare, ModalHandle(source.id), context, selection, parameters
            ),
        )

    def _open(
        self, trigger: str, actor: ExternalCommandActor, selection: ChangeSelection, received: float
    ) -> None:
        if time.monotonic() - received >= 2.5:
            return
        try:
            handle = self.publisher.open_modal(trigger, preparing_form("Loading your reservation…"))
        except Exception as error:
            self._log(actor.request_id, "open", error)
            return
        try:
            detail = self.queries.detail(actor, selection.reservation_id)
            view = change_form(selection, detail)
        except Exception as error:
            self._log(actor.request_id, "detail", error)
            view = preparing_form(
                "This reservation is not available for your current identity and permissions."
            )
        self._update(handle, view, actor.request_id)

    @staticmethod
    def _matching(
        review: CommandReview, selection: ChangeSelection, parameters: dict[str, Any]
    ) -> None:
        original = {
            key: value
            for key, value in review.request.parameters.items()
            if key != "expected_version"
        }
        if review.request.command != "reservation." + selection.operation or original != parameters:
            raise DomainError("CONFLICT", "The original form choices differ", 409)

    def _prepare(
        self,
        handle: ModalHandle,
        context: ExternalCommandSubmit,
        selection: ChangeSelection,
        parameters: dict[str, Any],
    ) -> None:
        actor = ExternalCommandActor(subject=context.subject, request_id=context.request_id)
        try:
            review = self.gateway.recover_by_key(context)
            if review is None:
                detail = self.queries.detail(actor, selection.reservation_id)
                if detail.reservation.version is None:
                    raise DomainError(
                        "DEPENDENCY_UNAVAILABLE", "Reservation history version is unavailable", 503
                    )
                schemas: dict[str, type[BaseModel]] = {
                    "cancel": CancelReservation,
                    "release": ReleaseReservation,
                    "extend": ExtendReservation,
                }
                schema = schemas[selection.operation]
                canonical = schema.model_validate(
                    {**parameters, "expected_version": detail.reservation.version}
                )
                result = self.gateway.submit(
                    context,
                    UserCommandInput(
                        command="reservation." + selection.operation,
                        parameters=canonical.model_dump(mode="json"),
                    ),
                )
                if result.outcome is None:
                    review = (
                        self.gateway.recover_by_key(context)
                        if result.error_code == "CONFLICT"
                        else None
                    )
                    if review is None:
                        raise DomainError(
                            result.error_code or "INTERNAL_ERROR",
                            "Owner change admission rejected",
                            result.error_status or 500,
                        )
                else:
                    review = self.gateway.review(actor, result.outcome.command_id)
            self._matching(review, selection, parameters)
            view = self.presenter.view(review, actor)
        except Exception as error:
            self._log(context.request_id, "prepare", error)
            view = preparing_form(
                "This change could not be verified or the form was already submitted "
                "with different choices. Check your reservations before starting another request."
            )
        self._update(handle, view, context.request_id)

    def _update(self, handle: ModalHandle, view: dict[str, Any], request_id: str) -> None:
        try:
            self.publisher.update_modal(handle, view)
        except Exception as error:
            self._log(request_id, "update", error)

    @staticmethod
    def _log(request_id: str, stage: str, error: Exception) -> None:
        logger.warning(
            "request_id=%s reservation_change_form_failed stage=%s failure_type=%s",
            request_id,
            stage,
            type(error).__name__,
        )
