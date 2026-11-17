import hashlib
import json
import logging
import time
from functools import partial
from typing import Any

from pydantic import ValidationError
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
from aris.interfaces.slack.client import ModalPublisher
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.presentation import markdown, plain, slack_time
from aris.interfaces.slack.schemas import ModalHandle, SlackReply, SlashCommand
from aris.interfaces.slack.user_commands import (
    CONFIRM_ACTION,
    STATUS_ACTION,
    WITHDRAW_ACTION,
    status_view,
)
from aris.modules.nova.reservation_schemas import CreateReservation
from aris.modules.vega.target_schemas import GpuTargetQuery
from aris.modules.vega.target_service import VegaTargetService

logger = logging.getLogger(__name__)
PREPARING_CALLBACK = "aris.reservation.preparing"
USAGE = (
    "Use /gpu reserve GPU_UUID START_AT END_AT PURPOSE. "
    "Both times must include a timezone, for example 2026-12-01T10:00:00-08:00."
)


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
        self, sessions: sessionmaker[Session], targets: VegaTargetService, limits: GatewayLimits
    ) -> None:
        self.sessions, self.targets, self.limits = sessions, targets, limits

    def view(self, review: CommandReview) -> dict[str, Any]:
        outcome = review.outcome
        if (
            not isinstance(outcome, PendingConfirmation)
            or review.request.command != "reservation.create"
        ):
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


class SlackReservationDrafts:
    """After-ACK UI preparation; the ACK never means a command was admitted."""

    def __init__(
        self,
        gateway: UserCommandGateway,
        publisher: ModalPublisher,
        presenter: ReservationReviewPresenter,
    ) -> None:
        self.gateway, self.publisher, self.presenter = gateway, publisher, presenter
        if "reservation.create" not in gateway.allowed:
            raise ValueError("reservation drafts require the exposed create command")
        definition, _ = gateway.intake.commands._registration("reservation.create")
        if not definition.requires_confirmation:
            raise ValueError("reservation drafts require confirmed create")

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
        pieces = command.text.strip().split(maxsplit=4)
        try:
            if len(pieces) != 5 or pieces[0].lower() != "reserve":
                raise ValueError("invalid reserve arguments")
            request = CreateReservation.model_validate(
                {
                    "gpu_uuid": pieces[1],
                    "start_at": pieces[2],
                    "end_at": pieces[3],
                    "purpose": pieces[4],
                }
            )
        except (ValidationError, ValueError):
            return SlackReply({"response_type": "ephemeral", "text": USAGE})
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

    def _prepare(
        self,
        command: SlashCommand,
        request: CreateReservation,
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
            receipt_key="slack.reserve:" + body_hash,
            idempotency_key="slack.reserve:" + body_hash,
        )
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
                    "Request preparation rejected",
                    result.error_status or 500,
                )
            review = self.gateway.review(
                ExternalCommandActor(subject=context.subject, request_id=request_id),
                result.outcome.command_id,
            )
            view = self.presenter.view(review)
        except Exception as error:
            self._log(request_id, "prepare", error)
            view = information_view(
                "You are not authorized to make this reservation."
                if isinstance(error, DomainError) and error.code == "PERMISSION_DENIED"
                else "The request could not be verified. Check your reservations before retrying."
            )
        self._update(handle, view, request_id)

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
