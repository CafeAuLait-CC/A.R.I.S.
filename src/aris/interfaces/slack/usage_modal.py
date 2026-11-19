"""User-initiated read-only modal; loading and report updates happen after ACK."""

import logging
from functools import partial
from time import monotonic
from typing import Any

from pydantic import ValidationError

from aris.core.commands.gateway_schemas import ExternalCommandActor
from aris.core.contracts import DomainError
from aris.interfaces.slack.client import ModalPublisher, SlackDeliveryError
from aris.interfaces.slack.presentation import plain
from aris.interfaces.slack.schemas import Interaction, ModalHandle, SlackReply
from aris.interfaces.slack.usage import personal_usage_view
from aris.interfaces.slack.usage_contracts import (
    USAGE_CALLBACK,
    USAGE_CONTROLS,
    USAGE_HOME_ACTION,
    USAGE_NODE_ACTION,
    USAGE_PERIOD_ACTION,
    USAGE_PREFIX,
    USAGE_REFRESH_ACTION,
    UsageModalBinding,
)
from aris.interfaces.usage.personal import PersonalUsageQueries, ReportPeriod

logger = logging.getLogger(__name__)


def message_view(message: str) -> dict[str, Any]:
    return {
        "type": "modal",
        "callback_id": USAGE_CALLBACK,
        "title": plain("My usage"),
        "close": plain("Close"),
        "blocks": [{"type": "section", "text": plain(message)}],
    }


def option(label: str, value: str) -> dict[str, Any]:
    return {"text": plain(label if len(label) <= 75 else label[:74] + "…"), "value": value}


class UsageModalAdapter:
    def __init__(self, queries: PersonalUsageQueries, publisher: ModalPublisher) -> None:
        self.queries, self.publisher = queries, publisher

    @staticmethod
    def handles(interaction: Interaction, payload: dict[str, Any]) -> bool:
        view = payload.get("view")
        return any(action.action_id.startswith(USAGE_PREFIX) for action in interaction.actions) or (
            isinstance(view, dict) and view.get("callback_id") == USAGE_CALLBACK
        )

    def interact(
        self, interaction: Interaction, payload: dict[str, Any], request_id: str, received: float
    ) -> SlackReply:
        if interaction.type == "view_closed":
            return SlackReply({"ok": True})
        try:
            if interaction.type != "block_actions" or len(interaction.actions) != 1:
                raise ValueError("one usage block action is required")
            action = interaction.actions[0].action_id
            context = ExternalCommandActor(
                subject={
                    "provider": "slack",
                    "namespace": interaction.team.id,
                    "external_id": interaction.user.id,
                },
                request_id=request_id,
            )
            if action == USAGE_HOME_ACTION:
                if interaction.trigger_id is None:
                    raise ValueError("usage trigger required")
                return SlackReply(
                    {"ok": True},
                    after_ack=partial(
                        self._open, context, interaction.trigger_id.get_secret_value(), received
                    ),
                )
            if action not in (USAGE_NODE_ACTION, USAGE_PERIOD_ACTION, USAGE_REFRESH_ACTION):
                raise ValueError("unknown usage action")
            view = payload["view"]
            if not isinstance(view, dict) or view.get("callback_id") != USAGE_CALLBACK:
                raise ValueError("usage modal required")
            metadata = view["private_metadata"]
            if not isinstance(metadata, str) or len(metadata.encode()) > 2048:
                raise ValueError("invalid usage metadata")
            binding = UsageModalBinding.model_validate_json(metadata)
            if (binding.team_id, binding.user_id, binding.view_id) != (
                interaction.team.id,
                interaction.user.id,
                view.get("id"),
            ) or view.get("team_id", interaction.team.id) != interaction.team.id:
                raise DomainError("PERMISSION_DENIED", "Usage modal owner or view differs", 403)
            hash = view.get("hash")
            if not isinstance(hash, str) or not 1 <= len(hash) <= 256:
                raise ValueError("usage modal hash required")
            selection = self._selection(payload, binding, action)
            handle = ModalHandle(binding.view_id, hash)
            return SlackReply(
                {"ok": True},
                after_ack=partial(
                    self._update, context, handle, selection.node_id, selection.period
                ),
            )
        except (ValueError, KeyError, TypeError, ValidationError):
            raise DomainError(
                "VALIDATION_ERROR", "Invalid personal usage interaction", 422
            ) from None

    @staticmethod
    def _selection(
        payload: dict[str, Any], binding: UsageModalBinding, action: str
    ) -> UsageModalBinding:
        state = payload.get("state", {})
        if not isinstance(state, dict):
            raise ValueError("invalid usage state")
        values = state.get("values", {})
        if not isinstance(values, dict):
            raise ValueError("invalid usage values")
        group = values.get(USAGE_CONTROLS, {})
        if not isinstance(group, dict):
            raise ValueError("invalid usage controls")
        fields = {USAGE_NODE_ACTION: "node_id", USAGE_PERIOD_ACTION: "period"}
        changes = {}
        for id, field in fields.items():
            item = group.get(id)
            if item is not None:
                if not isinstance(item, dict):
                    raise ValueError("invalid usage selection")
                selected = item.get("selected_option")
                if not isinstance(selected, dict) or not isinstance(selected.get("value"), str):
                    raise ValueError("usage selection required")
                changes[field] = selected["value"]
        if action in fields:
            selected = payload["actions"][0]["selected_option"]
            if not isinstance(selected, dict) or not isinstance(selected.get("value"), str):
                raise ValueError("usage action selection required")
            field = fields[action]
            if field in changes and changes[field] != selected["value"]:
                raise ValueError("ambiguous usage selection")
            changes[field] = selected["value"]
        return UsageModalBinding.model_validate({**binding.model_dump(), **changes})

    def _render(
        self,
        context: ExternalCommandActor,
        handle: ModalHandle,
        node_id: str | None,
        period: ReportPeriod,
    ) -> dict[str, Any]:
        nodes = self.queries.choices(context)
        selected = node_id or nodes[0]
        if selected not in nodes:
            raise DomainError("PERMISSION_DENIED", "Current usage node access is unavailable", 403)
        report = self.queries.mine(context, selected, period)
        node_options = [option(node, node) for node in nodes]
        periods = [option("Week so far", "week"), option("Today so far", "today")]
        binding = UsageModalBinding(
            team_id=context.subject.namespace,
            user_id=context.subject.external_id,
            view_id=handle.view_id,
            node_id=selected,
            period=period,
        )
        view = message_view("")
        view["private_metadata"] = binding.model_dump_json()
        view["blocks"] = [
            {
                "type": "actions",
                "block_id": USAGE_CONTROLS,
                "elements": [
                    {
                        "type": "static_select",
                        "action_id": USAGE_NODE_ACTION,
                        "placeholder": plain("Node"),
                        "options": node_options,
                        "initial_option": next(
                            value for value in node_options if value["value"] == selected
                        ),
                    },
                    {
                        "type": "static_select",
                        "action_id": USAGE_PERIOD_ACTION,
                        "placeholder": plain("Period"),
                        "options": periods,
                        "initial_option": next(
                            value for value in periods if value["value"] == period
                        ),
                    },
                    {"type": "button", "action_id": USAGE_REFRESH_ACTION, "text": plain("Refresh")},
                ],
            },
            *personal_usage_view(report)["blocks"][1:],
        ]
        return view

    def _update(
        self,
        context: ExternalCommandActor,
        handle: ModalHandle,
        node_id: str | None,
        period: ReportPeriod,
    ) -> None:
        try:
            try:
                view = self._render(context, handle, node_id, period)
            except DomainError as error:
                message = (
                    "Your Slack identity is not authorized to view usage on this node."
                    if error.code == "PERMISSION_DENIED"
                    else "Your recorded usage is unavailable. Close and reopen My usage to retry."
                )
                view = message_view(message)
            except Exception:
                view = message_view(
                    "Your recorded usage is unavailable. Close and reopen My usage to retry."
                )
            self.publisher.update_modal(handle, view)
        except SlackDeliveryError as error:
            logger.warning("slack_usage_modal_delivery_failed code=%s", error.code)
        except Exception as error:
            logger.warning("slack_usage_modal_delivery_failed type=%s", type(error).__name__)

    def _open(self, context: ExternalCommandActor, trigger: str, received: float) -> None:
        try:
            if monotonic() - received >= 2.5:
                raise SlackDeliveryError("trigger_expired_locally", retryable=False)
            handle = self.publisher.open_modal(
                trigger, message_view("Loading your recorded usage…")
            )
            self._update(context, handle, None, "week")
        except SlackDeliveryError as error:
            logger.warning("slack_usage_modal_open_failed code=%s", error.code)
        except Exception as error:
            logger.warning("slack_usage_modal_open_failed type=%s", type(error).__name__)
