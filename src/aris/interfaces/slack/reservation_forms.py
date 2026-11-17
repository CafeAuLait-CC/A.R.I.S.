import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator

from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import (
    ExternalCommandActor,
    ExternalCommandSubmit,
    UserCommandInput,
)
from aris.core.contracts import DomainError
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.client import ModalPublisher
from aris.interfaces.slack.presentation import plain
from aris.interfaces.slack.schemas import (
    Interaction,
    ModalHandle,
    SlackPayload,
    SlackReply,
    SlashCommand,
)
from aris.interfaces.slack.user_commands import CommandReviewPresenter
from aris.modules.nova.reservation_schemas import CreateReservation
from aris.modules.vega.target_schemas import GpuCatalogPage

logger = logging.getLogger(__name__)
FORM_CALLBACK = "aris.reservation.create.form"
FIELDS = ("gpu", "start", "end", "purpose")


def preparing_form(message: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": "aris.reservation.preparing",
        "title": plain("Reservation review"),
        "close": plain("Close"),
        "blocks": [{"type": "section", "text": plain(message)}],
    }


def create_form(page: GpuCatalogPage, now: datetime) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("reservation form clock requires a timezone")
    if not page.labels:
        return preparing_form(
            "No authorized GPU choices are available in this inventory page. "
            "Use /gpu reserve GPU_UUID START_AT END_AT PURPOSE for another known target."
        )
    start = (int(now.timestamp()) // 60 + 15) * 60
    options = []
    for label in page.labels:
        name = f"{label.node_id} · GPU {label.index} · {label.model}"
        options.append(
            {"text": plain(name if len(name) <= 75 else name[:74] + "…"), "value": label.gpu_uuid}
        )
    blocks: list[dict[str, Any]] = [
        {
            "type": "context",
            "elements": [
                plain(
                    "Resource labels use last-known inventory. Selecting a GPU does not reserve it."
                )
            ],
        },
        {
            "type": "input",
            "block_id": "gpu",
            "label": plain("GPU"),
            "element": {
                "type": "static_select",
                "action_id": "aris.reserve.gpu",
                "placeholder": plain("Select a GPU"),
                "options": options,
            },
        },
    ]
    for field, title, initial in (
        ("start", "Scheduled start", start),
        ("end", "Scheduled end", start + 3600),
    ):
        blocks.append(
            {
                "type": "input",
                "block_id": field,
                "label": plain(title),
                "element": {
                    "type": "datetimepicker",
                    "action_id": "aris.reserve." + field,
                    "initial_date_time": initial,
                },
            }
        )
    blocks.append(
        {
            "type": "input",
            "block_id": "purpose",
            "label": plain("Purpose"),
            "element": {
                "type": "plain_text_input",
                "action_id": "aris.reserve.purpose",
                "max_length": 256,
                "multiline": True,
            },
        }
    )
    if page.truncated:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    plain(
                        "Only the first 100 inventory targets were considered. "
                        "Use the full /gpu reserve command for a known target outside this list."
                    )
                ],
            }
        )
    return {
        "type": "modal",
        "callback_id": FORM_CALLBACK,
        "title": plain("Reserve a GPU"),
        "submit": plain("Review request"),
        "close": plain("Cancel"),
        "notify_on_close": True,
        "blocks": blocks,
    }


class FormSource(SlackPayload):
    type: Literal["modal"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    callback_id: Literal["aris.reservation.create.form"]
    state: dict[str, Any] | None = Field(default=None, repr=False)


class SelectedOption(SlackPayload):
    value: str = Field(min_length=1, max_length=128)


class GpuInput(SlackPayload):
    type: Literal["static_select"]
    selected_option: SelectedOption


class TimeInput(SlackPayload):
    type: Literal["datetimepicker"]
    selected_date_time: int = Field(ge=0, le=9999999999, strict=True)


class PurposeInput(SlackPayload):
    type: Literal["plain_text_input"]
    value: str = Field(min_length=1, max_length=256, repr=False)

    @field_validator("value")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("purpose must not be blank")
        return value.strip()


def submitted_request(source: FormSource) -> tuple[CreateReservation | None, dict[str, str]]:
    values = source.state.get("values") if source.state else None
    if not isinstance(values, dict) or set(values) != set(FIELDS):
        raise DomainError("VALIDATION_ERROR", "Reservation form fields differ", 422)
    inputs = {}
    for field in FIELDS:
        entry = values[field]
        action = "aris.reserve." + field
        if not isinstance(entry, dict) or set(entry) != {action}:
            raise DomainError("VALIDATION_ERROR", "Reservation form actions differ", 422)
        inputs[field] = entry[action]
    parsed: dict[str, Any] = {}
    errors = {}
    for field in FIELDS:
        try:
            if field == "gpu":
                parsed["gpu_uuid"] = GpuInput.model_validate(inputs[field]).selected_option.value
            elif field == "purpose":
                parsed["purpose"] = PurposeInput.model_validate(inputs[field]).value
            else:
                stamp = TimeInput.model_validate(inputs[field]).selected_date_time
                parsed["start_at" if field == "start" else "end_at"] = datetime.fromtimestamp(
                    stamp, UTC
                )
        except (ValidationError, ValueError, OverflowError):
            errors[field] = {
                "gpu": "Select a GPU.",
                "purpose": "Enter a purpose (1–256 characters).",
            }.get(field, "Select a valid date and time.")
    if errors:
        return None, errors
    try:
        return CreateReservation.model_validate(parsed), {}
    except ValidationError:
        return None, {"end": "The end must be after the start."}


class SlackReservationForms:
    def __init__(
        self,
        gateway: UserCommandGateway,
        publisher: ModalPublisher,
        presenter: CommandReviewPresenter,
        queries: PersonalReservationQueries,
        *,
        clock: Callable[[], datetime],
    ) -> None:
        self.gateway, self.publisher, self.presenter = gateway, publisher, presenter
        self.queries, self.clock = queries, clock
        if not gateway.definition("reservation.create").requires_confirmation:
            raise ValueError("reservation forms require confirmed creation")

    def start(self, command: SlashCommand, request_id: str, received: float) -> SlackReply:
        if command.trigger_id is None:
            raise DomainError("VALIDATION_ERROR", "Reservation form requires a Slack trigger", 422)
        actor = ExternalCommandActor(
            subject={
                "provider": "slack",
                "namespace": command.team_id,
                "external_id": command.user_id,
            },
            request_id=request_id,
        )
        return SlackReply(
            {"response_type": "ephemeral", "text": "Opening your reservation form."},
            after_ack=partial(self._open, command.trigger_id.get_secret_value(), actor, received),
        )

    def _open(self, trigger: str, actor: ExternalCommandActor, received: float) -> None:
        if time.monotonic() - received >= 2.5:
            return
        try:
            handle = self.publisher.open_modal(
                trigger, preparing_form("Loading authorized GPU choices…")
            )
        except Exception as error:
            self._log(actor.request_id, "open", error)
            return
        try:
            page = self.queries.create_choices(actor)
            view = create_form(page, self.clock())
        except Exception as error:
            self._log(actor.request_id, "choices", error)
            view = preparing_form(
                "GPU choices could not be verified for your current identity and permissions."
            )
        self._update(handle, view, actor.request_id)

    def interact(
        self, body: bytes, payload: dict[str, Any], interaction: Interaction, request_id: str
    ) -> SlackReply:
        source = FormSource.model_validate(payload.get("view"))
        if interaction.type == "view_closed":
            return SlackReply({"ok": True})
        if interaction.type != "view_submission" or interaction.actions:
            raise DomainError("VALIDATION_ERROR", "Unsupported reservation form interaction", 422)
        request, errors = submitted_request(source)
        if errors:
            return SlackReply({"response_action": "errors", "errors": errors})
        assert request is not None
        subject = {
            "provider": "slack",
            "namespace": interaction.team.id,
            "external_id": interaction.user.id,
        }
        key = hashlib.sha256(json.dumps([subject, source.id], sort_keys=True).encode()).hexdigest()
        context = ExternalCommandSubmit(
            subject=subject,
            request_id=request_id,
            receipt_key="slack.form.body:" + hashlib.sha256(body).hexdigest(),
            idempotency_key="slack.form:" + key,
        )
        return SlackReply(
            {
                "response_action": "update",
                "view": preparing_form(
                    "Preparing your request for review. No reservation has been made."
                ),
            },
            # The ACK replaces the form, so its old hash is no longer current.
            after_ack=partial(self._prepare, ModalHandle(source.id), context, request),
        )

    def _prepare(
        self, handle: ModalHandle, context: ExternalCommandSubmit, request: CreateReservation
    ) -> None:
        try:
            result = self.gateway.submit(
                context,
                UserCommandInput(
                    command="reservation.create", parameters=request.model_dump(mode="json")
                ),
            )
            if result.outcome is None:
                raise DomainError(
                    result.error_code or "INTERNAL_ERROR",
                    "Form admission rejected",
                    result.error_status or 500,
                )
            actor = ExternalCommandActor(subject=context.subject, request_id=context.request_id)
            review = self.gateway.review(actor, result.outcome.command_id)
            view = self.presenter.view(review, actor)
        except Exception as error:
            self._log(context.request_id, "prepare", error)
            view = preparing_form(
                "This form could not be verified or was already submitted with different choices. "
                "Check your reservations before opening a new request."
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
            "request_id=%s reservation_form_failed stage=%s failure_type=%s",
            request_id,
            stage,
            type(error).__name__,
        )
