import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, ValidationError, field_validator, model_validator
from sqlalchemy.orm import Session, sessionmaker

from aris.core.commands.budgets import transaction_budget
from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import (
    ExternalCommandActor,
    ExternalCommandSubmit,
    GatewayLimits,
    UserCommandInput,
)
from aris.core.commands.schemas import CommandReview, PendingConfirmation
from aris.core.contracts import DomainError
from aris.core.time import utc
from aris.interfaces.reservations.service import PersonalReservationQueries
from aris.interfaces.slack.client import ModalPublisher
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.reservation_forms import SlackReservationForms
from aris.interfaces.slack.schemas import ModalHandle, SlackPayload, SlackReply, SlashCommand
from aris.interfaces.slack.user_commands import (
    CONFIRM_ACTION,
    STATUS_ACTION,
    WITHDRAW_ACTION,
    status_view,
)
from aris.modules.nova.reservation_schemas import (
    CancelReservation,
    CreateReservation,
    ExtendReservation,
    ReleaseReservation,
)
from aris.modules.vega.target_schemas import GpuTargetQuery
from aris.modules.vega.target_service import VegaTargetService

logger = logging.getLogger(__name__)
PREPARING_CALLBACK = "aris.reservation.preparing"
USAGE = (
    "Use /gpu reserve GPU_UUID START_AT END_AT PURPOSE. "
    "Both times must include a timezone, for example 2026-12-01T10:00:00-08:00."
)
OWN_COMMANDS = frozenset(
    "reservation." + operation for operation in ("create", "cancel", "release", "extend")
)
CHANGE_USAGE = (
    "Use /gpu cancel RESERVATION_ID REASON, /gpu release RESERVATION_ID REASON, "
    "or /gpu extend RESERVATION_ID NEW_END_AT REASON. Include a timezone in NEW_END_AT."
)


class ReservationChangeDraft(SlackPayload):
    operation: Literal["cancel", "release", "extend"]
    reservation_id: UUID
    reason: str = Field(min_length=1, max_length=256, repr=False)
    new_end_at: AwareDatetime | None = None

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("change reason must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def extension_end(self) -> "ReservationChangeDraft":
        if (self.operation == "extend") != (self.new_end_at is not None):
            raise ValueError("only extension requires a new end")
        if self.new_end_at is not None:
            self.new_end_at = utc(self.new_end_at)
        return self


def information_view(message: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": PREPARING_CALLBACK,
        "title": plain("Reservation review"),
        "close": plain("Close"),
        "blocks": [{"type": "section", "text": plain(message)}],
    }


class ReservationReviewPresenter:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        targets: VegaTargetService,
        limits: GatewayLimits,
        queries: PersonalReservationQueries,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.sessions, self.targets, self.limits = sessions, targets, limits
        self.queries, self.clock = queries, clock

    def view(self, review: CommandReview, context: ExternalCommandActor) -> dict[str, Any]:
        outcome = review.outcome
        if not isinstance(outcome, PendingConfirmation):
            return status_view(review.command_id, outcome)
        if review.request.command in OWN_COMMANDS - {"reservation.create"}:
            return self._change_view(review, context)
        if review.request.command != "reservation.create":
            return status_view(review.command_id, outcome)
        request = CreateReservation.model_validate(review.request.parameters)
        with self.sessions() as session:
            transaction_budget(
                session,
                self.limits.lock_timeout_ms,
                self.limits.statement_timeout_ms,
                self.limits.transaction_timeout_ms,
            )
            label = self.targets.label_in_transaction(
                session, GpuTargetQuery(gpu_uuid=request.gpu_uuid)
            )
        if (
            label is None
            or label.gpu_uuid != request.gpu_uuid
            or review.request.scope.type != "node"
            or label.node_id != review.request.scope.id
        ):
            view = status_view(review.command_id, outcome)
            view["blocks"][0]["text"] = plain(
                "The selected GPU cannot be identified. Refresh or withdraw this request."
            )
            return view
        selection = {"command_id": str(review.command_id)}
        return {
            "type": "modal",
            "callback_id": "aris.command.confirmation",
            "title": plain("Confirm reservation"),
            "close": plain("Close"),
            "blocks": [
                {
                    "type": "section",
                    "fields": [
                        plain("Node\n" + label.node_id),
                        plain(f"GPU {label.index}\n{label.model}"),
                    ],
                },
                {
                    "type": "section",
                    "text": markdown(
                        f"*Scheduled start*\n{slack_time(request.start_at)}\n"
                        f"*Scheduled end*\n{slack_time(request.end_at)}"
                    ),
                },
                {"type": "section", "text": plain("Purpose\n" + request.purpose)},
                {
                    "type": "context",
                    "elements": [
                        markdown(
                            f"Confirm by {slack_time(outcome.expires_at)}"
                            + (
                                f" · Must run by {slack_time(outcome.execution_not_after)}"
                                if outcome.execution_not_after is not None
                                else ""
                            )
                        )
                    ],
                },
                {
                    "type": "section",
                    "text": plain(
                        "Confirmation queues this request. Availability, permissions and rules "
                        "will be checked when it runs; a reservation is not guaranteed."
                    ),
                },
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "action_id": CONFIRM_ACTION,
                            "text": plain("Confirm request"),
                            "style": "primary",
                            "value": json.dumps(
                                {**selection, "proof": outcome.proof.get_secret_value()},
                                separators=(",", ":"),
                            ),
                        },
                        {
                            "type": "button",
                            "action_id": WITHDRAW_ACTION,
                            "text": plain("Withdraw request"),
                            "value": json.dumps(selection, separators=(",", ":")),
                        },
                        {
                            "type": "button",
                            "action_id": STATUS_ACTION,
                            "text": plain("Refresh"),
                            "value": json.dumps(selection, separators=(",", ":")),
                        },
                    ],
                },
            ],
        }

    def _change_view(self, review: CommandReview, context: ExternalCommandActor) -> dict[str, Any]:
        operation = review.request.command.removeprefix("reservation.")
        change: CancelReservation | ReleaseReservation | ExtendReservation
        change = (
            ExtendReservation.model_validate(review.request.parameters)
            if operation == "extend"
            else CancelReservation.model_validate(review.request.parameters)
        )
        detail = self.queries.detail(context, change.reservation_id)
        row, label = detail.reservation, detail.resource_label
        now = utc(self.clock())
        valid_state = (
            row.status == "confirmed" and now < utc(row.start_at)
            if operation == "cancel"
            else row.status in {"confirmed", "active"} and now >= utc(row.start_at)
            if operation == "release"
            else row.status in {"confirmed", "active"}
            and now < utc(row.end_at)
            and isinstance(change, ExtendReservation)
            and change.new_end_at > utc(row.end_at)
        )
        if row.version != change.expected_version or not valid_state:
            view = status_view(review.command_id, review.outcome)
            view["blocks"][0]["text"] = plain(
                "This reservation has changed or cannot accept this action. "
                "Withdraw this request and start a new one."
            )
            return view
        if operation == "extend" and label is None:
            view = status_view(review.command_id, review.outcome)
            view["blocks"][0]["text"] = plain(
                "The reservation resource cannot be identified. Refresh or withdraw this request."
            )
            return view
        # Resource observations are not needed to reduce an existing booking.
        # Never guess a label when a device has disappeared from inventory.
        resource = (
            f"{label.node_id} · GPU {label.index} · {label.model}"
            if label is not None
            else "Unavailable (no last-known resource label)"
        )
        blocks: list[dict[str, Any]] = [
            {"type": "section", "text": plain("Resource\n" + resource)},
            {
                "type": "section",
                "text": plain(
                    f"Reservation\n{row.id}\nStatus: {row.status} · Version: {row.version}"
                ),
            },
            {
                "type": "section",
                "text": markdown(
                    f"*Scheduled start*\n{slack_time(row.start_at)}\n"
                    f"*Scheduled end*\n{slack_time(row.end_at)}"
                ),
            },
            {"type": "section", "text": plain("Purpose\n" + row.purpose)},
            {"type": "section", "text": plain("Reason\n" + change.reason)},
        ]
        if isinstance(change, ExtendReservation):
            blocks.append(
                {"type": "section", "text": markdown("*New end*\n" + slack_time(change.new_end_at))}
            )
        blocks.append(
            {
                "type": "context",
                "elements": [plain("Resource labels use last-known inventory.")],
            }
        )
        if operation == "release":
            blocks.append(
                {
                    "type": "section",
                    "text": plain("This ends the reservation only. GPU processes keep running."),
                }
            )
        assert isinstance(review.outcome, PendingConfirmation)
        # Reuse the native command modal actions and its original signed proof.
        view = status_view(review.command_id, review.outcome)
        selection = {"command_id": str(review.command_id)}
        view["title"] = plain("Confirm " + operation)
        view["blocks"] = blocks + [
            {
                "type": "context",
                "elements": [
                    markdown(
                        f"Confirm by {slack_time(review.outcome.expires_at)}"
                        + (
                            f" · Must run by {slack_time(review.outcome.execution_not_after)}"
                            if review.outcome.execution_not_after is not None
                            else ""
                        )
                    )
                ],
            },
            {
                "type": "section",
                "text": plain(
                    "Confirmation queues the original request. Current permissions, "
                    "reservation version and applicable rules are checked when it runs."
                ),
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": CONFIRM_ACTION,
                        "text": plain("Confirm " + operation),
                        "style": "primary",
                        "value": json.dumps(
                            {**selection, "proof": review.outcome.proof.get_secret_value()},
                            separators=(",", ":"),
                        ),
                    },
                    {
                        "type": "button",
                        "action_id": WITHDRAW_ACTION,
                        "text": plain("Withdraw request"),
                        "value": json.dumps(selection, separators=(",", ":")),
                    },
                    {
                        "type": "button",
                        "action_id": STATUS_ACTION,
                        "text": plain("Refresh"),
                        "value": json.dumps(selection, separators=(",", ":")),
                    },
                ],
            },
        ]
        return view


class SlackReservationDrafts:
    """After-ACK UI preparation; the ACK never means a command was admitted."""

    def __init__(
        self,
        gateway: UserCommandGateway,
        publisher: ModalPublisher,
        presenter: ReservationReviewPresenter,
    ) -> None:
        self.gateway, self.publisher, self.presenter = gateway, publisher, presenter
        exposed = gateway.allowed & OWN_COMMANDS
        if not exposed or any(
            not gateway.definition(name).requires_confirmation for name in exposed
        ):
            raise ValueError("reservation drafts require exposed confirmed owner commands")
        self.forms = (
            SlackReservationForms(
                gateway, publisher, presenter, presenter.queries, clock=presenter.clock
            )
            if "reservation.create" in exposed
            else None
        )

    def validate_configuration(self, config: SlackSettings) -> None:
        namespace = self.gateway.intake.namespace
        if (
            not config.configured
            or namespace.provider != "slack"
            or namespace.namespace != config.team_id
        ):
            raise ValueError("reservation drafts require the configured workspace")

    def start(
        self, command: SlashCommand, body: bytes, request_id: str, received: float
    ) -> SlackReply:
        if command.text.strip().lower() == "reserve":
            self.gateway.definition("reservation.create")
            assert self.forms is not None
            return self.forms.start(command, request_id, received)
        first = command.text.strip().split(maxsplit=1)
        operation = first[0].lower() if first else ""
        try:
            request = self._parse(command.text, operation)
        except (ValidationError, ValueError):
            return SlackReply(
                {
                    "response_type": "ephemeral",
                    "text": USAGE if operation == "reserve" else CHANGE_USAGE,
                }
            )
        name = "reservation." + ("create" if operation == "reserve" else operation)
        self.gateway.definition(name)
        if command.trigger_id is None:
            raise DomainError(
                "VALIDATION_ERROR", "Reservation review requires a Slack trigger", 422
            )
        return SlackReply(
            {"response_type": "ephemeral", "text": "Opening your reservation review."},
            after_ack=partial(
                self._prepare,
                command,
                request,
                hashlib.sha256(body).hexdigest(),
                request_id,
                received,
            ),
        )

    @staticmethod
    def _parse(text: str, operation: str) -> CreateReservation | ReservationChangeDraft:
        if operation == "reserve":
            pieces = text.strip().split(maxsplit=4)
            if len(pieces) != 5:
                raise ValueError("invalid reserve arguments")
            return CreateReservation.model_validate(
                dict(zip(("gpu_uuid", "start_at", "end_at", "purpose"), pieces[1:], strict=True))
            )
        pieces = text.strip().split(maxsplit=3 if operation == "extend" else 2)
        if operation not in {"cancel", "release", "extend"} or len(pieces) != (
            4 if operation == "extend" else 3
        ):
            raise ValueError("invalid change arguments")
        return ReservationChangeDraft.model_validate(
            {
                "operation": operation,
                "reservation_id": pieces[1],
                "reason": pieces[-1],
                "new_end_at": pieces[2] if operation == "extend" else None,
            }
        )

    def _prepare(
        self,
        command: SlashCommand,
        request: CreateReservation | ReservationChangeDraft,
        body_hash: str,
        request_id: str,
        received: float,
    ) -> None:
        if time.monotonic() - received >= 2.5:
            logger.warning(
                "request_id=%s reservation_review_open_skipped code=trigger_expired", request_id
            )
            return
        assert command.trigger_id is not None
        try:
            handle = self.publisher.open_modal(
                command.trigger_id.get_secret_value(),
                information_view("Preparing the request details…"),
            )
        except Exception as error:
            self._log(request_id, "open", error)
            return
        context = ExternalCommandSubmit(
            subject={
                "provider": "slack",
                "namespace": command.team_id,
                "external_id": command.user_id,
            },
            request_id=request_id,
            receipt_key="slack.reservation:" + body_hash,
            idempotency_key="slack.reservation:" + body_hash,
        )
        try:
            actor = ExternalCommandActor(subject=context.subject, request_id=request_id)
            review = self.gateway.recover_by_key(context)
            name = (
                "reservation.create"
                if isinstance(request, CreateReservation)
                else "reservation." + request.operation
            )
            if review is None:
                parameters = self._parameters(actor, request)
                result = self.gateway.submit(
                    context, UserCommandInput(command=name, parameters=parameters)
                )
                if result.outcome is None:
                    # Another copy can admit the original intent after our
                    # first lookup, while the reservation version changes.
                    review = (
                        self.gateway.recover_by_key(context)
                        if result.error_code == "CONFLICT"
                        else None
                    )
                    if review is None:
                        raise DomainError(
                            result.error_code or "INTERNAL_ERROR",
                            "Request preparation rejected",
                            result.error_status or 500,
                        )
                else:
                    review = self.gateway.review(actor, result.outcome.command_id)
            if review.request.command != name:
                raise DomainError("CONFLICT", "Original command differs", 409)
            view = self.presenter.view(review, actor)
        except Exception as error:
            self._log(request_id, "prepare", error)
            view = information_view(
                "You are not authorized to perform this reservation action."
                if isinstance(error, DomainError) and error.code == "PERMISSION_DENIED"
                else "The request could not be verified. Check your reservations before retrying."
            )
        self._update(handle, view, request_id)

    def _parameters(
        self, actor: ExternalCommandActor, request: CreateReservation | ReservationChangeDraft
    ) -> dict[str, Any]:
        if isinstance(request, CreateReservation):
            return request.model_dump(mode="json")
        detail = self.presenter.queries.detail(actor, request.reservation_id)
        if detail.reservation.version is None:
            raise DomainError("DEPENDENCY_UNAVAILABLE", "Reservation version is unavailable", 503)
        values = {
            "reservation_id": str(request.reservation_id),
            "expected_version": detail.reservation.version,
            "reason": request.reason,
        }
        if request.operation == "extend":
            return ExtendReservation.model_validate(
                {**values, "new_end_at": request.new_end_at}
            ).model_dump(mode="json")
        schema = CancelReservation if request.operation == "cancel" else ReleaseReservation
        return schema.model_validate(values).model_dump(mode="json")

    def _update(self, handle: ModalHandle, view: dict[str, Any], request_id: str) -> None:
        try:
            self.publisher.update_modal(handle, view)
        except Exception as error:
            self._log(request_id, "update", error)

    @staticmethod
    def _log(request_id: str, stage: str, error: Exception) -> None:
        logger.warning(
            "request_id=%s reservation_review_failed stage=%s failure_type=%s",
            request_id,
            stage,
            type(error).__name__,
        )
