"""Authentication codes go only to the pre-bound Slack member, never a channel input."""

import re

from pydantic import SecretStr

from aris.core.access.passwords import Purpose
from aris.core.access.schemas import ExternalSubject
from aris.interfaces.slack.client import SlackClient


class SlackInvitationSender:
    def __init__(self, client: SlackClient, workspace: str) -> None:
        self.client, self.workspace = client, workspace

    def send(self, subject: ExternalSubject, code: SecretStr, purpose: Purpose) -> None:
        if (
            subject.provider != "slack"
            or subject.namespace != self.workspace
            or re.fullmatch(r"[UW][A-Z0-9]{8,127}", subject.external_id) is None
        ):
            raise ValueError("invitation recipient must be the bound workspace member")
        action = (
            "activate your ARIS account" if purpose == "activate" else "reset your ARIS password"
        )
        self.client.send_direct_message(
            subject.external_id,
            f"Your ARIS invitation code is {code.get_secret_value()}. "
            f"Enter it on the ARIS page to {action}. It expires in 10 minutes. "
            "If you did not request this, ignore this message. Do not share the code.",
        )
