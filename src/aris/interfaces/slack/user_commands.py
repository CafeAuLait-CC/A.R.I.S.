import hashlib
import json
import logging
from collections.abc import Callable
from functools import partial
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import Field

from aris.core.commands.gateway import UserCommandGateway
from aris.core.commands.gateway_schemas import ExternalCommandActor, ExternalCommandContext
from aris.core.commands.queue_schemas import IntakeResult, WithdrawCommand
from aris.core.commands.schemas import (
    CommandOutcome,
    CommandReview,
    ConfirmCommand,
    PendingConfirmation,
)
from aris.core.contracts import DomainError
from aris.interfaces.slack.client import ModalPublisher, SlackDeliveryError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.schemas import Interaction, ModalHandle, SlackPayload, SlackReply

logger = logging.getLogger(__name__)
COMMAND_PREFIX = "aris.command."
CONFIRM_ACTION = COMMAND_PREFIX + "confirm"
WITHDRAW_ACTION = COMMAND_PREFIX + "withdraw"
STATUS_ACTION = COMMAND_PREFIX + "status"


class CommandReviewPresenter(Protocol):
    def view(self, review: CommandReview, context: ExternalCommandActor) -> dict[str, Any]: ...


class CommandModalSource(SlackPayload):
    type: Literal["modal"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9]+$")
    hash: str | None = Field(default=None, max_length=256)
    callback_id: Literal["aris.command.confirmation", "aris.command.status"]


def status_view(
    command_id: UUID, outcome: CommandOutcome | None, error_code: str | None = None
) -> dict[str, Any]:
    if outcome is None:
        message = {
            "PERMISSION_DENIED": "This request is not authorized. No current result is available.",
            "COMMAND_ALREADY_FINISHED": (
                "This command has already finished and cannot be withdrawn."
            ),
        }.get(error_code or "", "The current result cannot be confirmed. Refresh this command.")
    elif outcome.state == "queued":
        message = (
            "The execution deadline has passed. The final result is still being checked."
            if outcome.deadline_elapsed
            else "Confirmed and queued. Waiting to run."
        )
    else:
        message = {
            "awaiting_confirmation": (
                "Waiting for confirmation. Return to the original request to review it."
            ),
            "succeeded": "This command has completed.",
            "expired": "The confirmation or execution deadline has passed.",
            "cancelled": "The unexecuted command was withdrawn.",
            "failed": "This command was not completed.",
        }[outcome.state]
    actions = [
        {
            "type": "button",
            "action_id": STATUS_ACTION,
            "text": {"type": "plain_text", "text": "Refresh"},
            "value": json.dumps({"command_id": str(command_id)}, separators=(",", ":")),
        }
    ]
    if outcome is not None and outcome.state in {"queued", "awaiting_confirmation"}:
        actions.append(
            {
                "type": "button",
                "action_id": WITHDRAW_ACTION,
                "text": {"type": "plain_text", "text": "Withdraw request"},
                "value": json.dumps({"command_id": str(command_id)}, separators=(",", ":")),
            }
        )
    return {
        "type": "modal",
        "callback_id": "aris.command.status",
        "title": {"type": "plain_text", "text": "Command status"},
        "close": {"type": "plain_text", "text": "Close"},
        "blocks": [
            {"type": "section", "text": {"type": "plain_text", "text": message}},
            {"type": "actions", "elements": actions},
        ],
    }


class SlackCommandAdapter:
    """Called only after SlackService verifies raw-body signature and workspace."""

    def __init__(
        self,
        gateway: UserCommandGateway,
        publisher: ModalPublisher,
        review_presenter: CommandReviewPresenter | None = None,
    ) -> None:
        self.gateway, self.publisher = gateway, publisher
        self.review_presenter = review_presenter

    def validate_configuration(self, config: SlackSettings) -> None:
        namespace = self.gateway.intake.namespace
        if (
            not config.configured
            or namespace.provider != "slack"
            or namespace.namespace != config.team_id
        ):
            raise ValueError("Slack user commands require the configured identity workspace")

    @staticmethod
    def handles(interaction: Interaction) -> bool:
        return any(item.action_id.startswith(COMMAND_PREFIX) for item in interaction.actions)

    def interact(
        self, body: bytes, payload: dict[str, Any], interaction: Interaction, request_id: str
    ) -> SlackReply:
        if interaction.type != "block_actions" or len(interaction.actions) != 1:
            raise DomainError("VALIDATION_ERROR", "Command requires one modal action", 422)
        source = CommandModalSource.model_validate(payload.get("view"))
        action = interaction.actions[0]
        if (
            action.action_id not in {CONFIRM_ACTION, WITHDRAW_ACTION, STATUS_ACTION}
            or action.value is None
        ):
            raise DomainError("VALIDATION_ERROR", "Unsupported command action", 422)
        context = ExternalCommandContext(
            subject={
                "provider": "slack",
                "namespace": interaction.team.id,
                "external_id": interaction.user.id,
            },
            request_id=request_id,
            receipt_key="slack.action:" + hashlib.sha256(body).hexdigest(),
        )
        outcome, error_code = None, None
        operation: Callable[[], IntakeResult] | None
        if action.action_id == CONFIRM_ACTION:
            confirmation = ConfirmCommand.model_validate_json(action.value)
            command_id = confirmation.command_id
            operation = partial(self.gateway.confirm, context, confirmation)
        else:
            selection = WithdrawCommand.model_validate_json(action.value)
            command_id = selection.command_id
            operation = (
                partial(self.gateway.withdraw, context, selection)
                if action.action_id == WITHDRAW_ACTION
                else None
            )
        try:
            if operation is None:
                review = self.gateway.review(
                    ExternalCommandActor(subject=context.subject, request_id=context.request_id),
                    command_id,
                )
                outcome = review.outcome
                if self.review_presenter is not None and isinstance(outcome, PendingConfirmation):
                    return SlackReply(
                        {},
                        after_ack=partial(
                            self._review_update,
                            ModalHandle(source.id, source.hash),
                            review,
                            ExternalCommandActor(
                                subject=context.subject, request_id=context.request_id
                            ),
                            request_id,
                        ),
                    )
            else:
                result = operation()
                outcome, error_code = result.outcome, result.error_code
        except DomainError as error:
            error_code = error.code
        view = status_view(command_id, outcome, error_code)
        return SlackReply(
            {},
            after_ack=partial(self._update, ModalHandle(source.id, source.hash), view, request_id),
        )

    def _review_update(
        self,
        handle: ModalHandle,
        review: CommandReview,
        context: ExternalCommandActor,
        request_id: str,
    ) -> None:
        assert self.review_presenter is not None
        try:
            view = self.review_presenter.view(review, context)
        except Exception as error:
            logger.warning(
                "request_id=%s command_review_failed failure_type=%s",
                request_id,
                type(error).__name__,
            )
            view = status_view(review.command_id, None)
        self._update(handle, view, request_id)

    def _update(self, handle: ModalHandle, view: dict[str, Any], request_id: str) -> None:
        try:
            self.publisher.update_modal(handle, view)
        except SlackDeliveryError as error:
            logger.warning(
                "request_id=%s command_modal_update_failed code=%s", request_id, error.code
            )
        except Exception as error:
            logger.warning(
                "request_id=%s command_modal_update_failed type=%s",
                request_id,
                type(error).__name__,
            )
