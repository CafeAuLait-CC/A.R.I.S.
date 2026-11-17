import hashlib
import json
import logging
import time
from datetime import UTC, datetime
from functools import partial
from typing import Any
from urllib.parse import parse_qs
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from aris.core.audit import AuditEntry
from aris.core.contracts import DomainError
from aris.interfaces.slack.client import ModalPublisher, SlackClient, SlackDeliveryError
from aris.interfaces.slack.config import SlackSettings
from aris.interfaces.slack.home import (
    DETAILS_ACTION,
    details_view,
    loading_details_view,
    unavailable_details_view,
)
from aris.interfaces.slack.presentation import resource_text
from aris.interfaces.slack.repo import SlackRepository
from aris.interfaces.slack.reservations import SlackReservationQueries
from aris.interfaces.slack.schemas import (
    EventEnvelope,
    GpuSelection,
    Interaction,
    SlackReply,
    SlashCommand,
)
from aris.interfaces.slack.security import require_workspace, verify_request
from aris.interfaces.slack.user_commands import SlackCommandAdapter
from aris.modules.vega.service import VegaService

logger = logging.getLogger(__name__)


def json_object(raw: bytes | str) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    return value


def form_fields(body: bytes) -> dict[str, str]:
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64)
    if any(len(v) != 1 for v in values.values()):
        raise ValueError("duplicate form field")
    return {key: value[0] for key, value in values.items()}


class SlackService:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        config: SlackSettings,
        vega: VegaService,
        modal_publisher: ModalPublisher | None = None,
        *,
        command_adapter: SlackCommandAdapter | None = None,
        reservation_queries: SlackReservationQueries | None = None,
    ) -> None:
        self.sessions = sessions
        self.config = config
        self.vega = vega
        self.modal = modal_publisher or SlackClient(config)
        self.command_adapter = command_adapter
        self.reservation_queries = reservation_queries
        if command_adapter is not None:
            command_adapter.validate_configuration(config)
        if reservation_queries is not None:
            reservation_queries.validate_configuration(config)

    def command(
        self, body: bytes, timestamp: str | None, signature: str | None, request_id: str
    ) -> SlackReply:
        verify_request(self.config, body, timestamp, signature)
        try:
            command = SlashCommand.model_validate(form_fields(body))
        except (ValueError, ValidationError) as exc:
            raise DomainError("VALIDATION_ERROR", "Invalid Slack command", 422) from exc
        require_workspace(self.config, command.team_id)
        if command.command == "/gpu" and command.text.strip().lower() == "mine":
            if self.reservation_queries is None:
                raise DomainError(
                    "DEPENDENCY_UNAVAILABLE", "Personal reservation queries are not enabled", 503
                )
            return self.reservation_queries.mine(command, request_id)
        if command.command != "/gpu":
            text = "当前支持 /gpu status 查询资源观测。"
        elif command.text.strip().lower() in {"", "status", "list"}:
            text = resource_text(self.vega.status())
        else:
            text = "当前支持 /gpu status。预约、释放等写入功能尚未上线。"
        return SlackReply({"response_type": "ephemeral", "text": text})

    def event(
        self,
        body: bytes,
        content_type: str,
        timestamp: str | None,
        signature: str | None,
        request_id: str,
    ) -> SlackReply:
        received = time.monotonic()
        verify_request(self.config, body, timestamp, signature)
        try:
            media_type = content_type.split(";", 1)[0].lower().strip()
            if media_type == "application/json":
                payload = json_object(body)
                if payload.get("type") == "url_verification":
                    challenge = payload.get("challenge")
                    if not isinstance(challenge, str) or not 1 <= len(challenge) <= 512:
                        raise ValueError("invalid challenge")
                    return SlackReply({"challenge": challenge})
                envelope = EventEnvelope.model_validate(payload)
                require_workspace(self.config, envelope.team_id)
                if envelope.type != "event_callback":
                    raise ValueError("unsupported envelope")
                event = envelope.event
                home_user = (
                    event.user
                    if event.type == "app_home_opened"
                    and event.tab == "home"
                    and not event.bot_id
                    and not event.subtype
                    else None
                )
                self._receive(
                    envelope.team_id,
                    "e:" + envelope.event_id,
                    payload,
                    event.type,
                    home_user,
                    request_id,
                )
            elif media_type == "application/x-www-form-urlencoded":
                payload = json_object(form_fields(body)["payload"])
                interaction = Interaction.model_validate(payload)
                require_workspace(self.config, interaction.team.id)
                if SlackCommandAdapter.handles(interaction):
                    if self.command_adapter is None:
                        raise DomainError(
                            "DEPENDENCY_UNAVAILABLE", "User command callbacks are not enabled", 503
                        )
                    return self.command_adapter.interact(body, payload, interaction, request_id)
                home_user = (
                    interaction.user.id
                    if (
                        interaction.type == "block_actions"
                        and any(
                            action.action_id == "refresh_home" for action in interaction.actions
                        )
                    )
                    else None
                )
                fingerprint = self._fingerprint(payload)
                detail_actions = [
                    action
                    for action in interaction.actions
                    if action.action_id.startswith(DETAILS_ACTION)
                ]
                selection = None
                if detail_actions:
                    if len(interaction.actions) != 1 or interaction.type != "block_actions":
                        raise ValueError("details require one block action")
                    if not interaction.trigger_id or detail_actions[0].value is None:
                        raise ValueError("details require trigger and resource selection")
                    selection = GpuSelection.model_validate_json(detail_actions[0].value)
                created = self._receive(
                    interaction.team.id,
                    "i:" + fingerprint,
                    payload,
                    interaction.type,
                    home_user,
                    request_id,
                )
                if created and selection is not None and interaction.trigger_id is not None:
                    return SlackReply(
                        {"ok": True},
                        after_ack=partial(
                            self._details,
                            selection,
                            interaction.trigger_id.get_secret_value(),
                            interaction.team.id,
                            interaction.user.id,
                            request_id,
                            received,
                        ),
                    )
            else:
                raise DomainError("UNSUPPORTED_MEDIA_TYPE", "Unsupported Slack media type", 415)
        except (ValueError, KeyError, ValidationError) as exc:
            raise DomainError("VALIDATION_ERROR", "Invalid Slack event", 422) from exc
        return SlackReply({"ok": True})

    @staticmethod
    def _fingerprint(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()

    def _receive(
        self,
        team_id: str,
        external_id: str,
        payload: dict[str, Any],
        kind: str,
        home_user: str | None,
        request_id: str,
    ) -> bool:
        now = datetime.now(UTC)
        fingerprint = self._fingerprint(payload)
        with self.sessions.begin() as session:
            repo = SlackRepository(session)
            receipt, created = repo.receive(team_id, external_id, fingerprint, kind, now)
            if receipt.fingerprint != fingerprint:
                raise DomainError("CONFLICT", "Slack event ID has different content", 409)
            if not created:
                return False
            if home_user:
                repo.add_home_job(receipt, home_user, now)
            session.add(
                AuditEntry(
                    id=str(uuid4()),
                    actor=team_id,
                    action="slack.event.received",
                    target=home_user or kind,
                    request_id=request_id,
                    recorded_at=now,
                    changes={"kind": kind, "home_job_created": bool(home_user)},
                )
            )
        return True

    def _details(
        self,
        selection: GpuSelection,
        trigger: str,
        team: str,
        user: str,
        request_id: str,
        received: float,
    ) -> None:
        error_code = ""
        try:
            if time.monotonic() - received >= 2.5:
                raise SlackDeliveryError("trigger_expired_locally", retryable=False)
            # Consume the short-lived trigger before querying the database.
            # A native loading modal is then updated outside the trigger deadline.
            handle = self.modal.open_modal(trigger, loading_details_view())
            try:
                snapshot = self.vega.resource_status()
                entry = next(
                    (item for item in snapshot.nodes if item.node.node_id == selection.node_id),
                    None,
                )
                gpu = (
                    next(
                        (item for item in entry.node.gpus if item.gpu_uuid == selection.gpu_uuid),
                        None,
                    )
                    if entry
                    else None
                )
                if entry is None or gpu is None:
                    error_code = "resource_not_found"
                    view = unavailable_details_view(
                        "No observation exists for this node or GPU. Refresh Home."
                    )
                else:
                    view = details_view(entry, gpu)
            except Exception:
                error_code = "dependency_unavailable"
                view = unavailable_details_view(
                    "Resource observations could not be loaded. "
                    "Current metrics are unknown; please try again."
                )
            self.modal.update_modal(handle, view)
        except SlackDeliveryError as exc:
            error_code = exc.code
        except Exception:
            error_code = "delivery_unavailable"
        try:
            with self.sessions.begin() as session:
                session.add(
                    AuditEntry(
                        id=str(uuid4()),
                        actor=team + ":" + user,
                        action="slack.details.failed" if error_code else "slack.details.opened",
                        target=selection.node_id,
                        request_id=request_id,
                        recorded_at=datetime.now(UTC),
                        changes={"error_code": error_code},
                    )
                )
        except Exception as exc:
            logger.warning("slack_details_audit failure_type=%s", type(exc).__name__)
