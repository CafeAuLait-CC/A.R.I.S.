import hashlib
import hmac
import re
import time

from aris.core.contracts import DomainError
from aris.interfaces.slack.config import SlackSettings


def verify_request(
    config: SlackSettings, body: bytes, timestamp: str | None, signature: str | None
) -> None:
    if not config.configured:
        raise DomainError("DEPENDENCY_UNAVAILABLE", "Slack is not configured", 503)
    if len(body) > config.max_body_bytes:
        raise DomainError("PAYLOAD_TOO_LARGE", "Slack request exceeds the body limit", 413)
    valid_headers = (
        timestamp is not None
        and re.fullmatch(r"[0-9]{1,12}", timestamp) is not None
        and signature is not None
        and re.fullmatch(r"v0=[0-9a-f]{64}", signature) is not None
    )
    if not valid_headers:
        raise DomainError("PERMISSION_DENIED", "Invalid Slack signature", 401)
    assert timestamp is not None and signature is not None
    if abs(time.time() - int(timestamp)) > config.signature_window_seconds:
        raise DomainError("PERMISSION_DENIED", "Expired Slack request", 401)
    message = b"v0:" + timestamp.encode("ascii") + b":" + body
    expected = (
        "v0="
        + hmac.new(
            config.signing_secret.get_secret_value().encode(), message, hashlib.sha256
        ).hexdigest()
    )
    if not hmac.compare_digest(expected, signature):
        raise DomainError("PERMISSION_DENIED", "Invalid Slack signature", 401)


def require_workspace(config: SlackSettings, team_id: str) -> None:
    if not hmac.compare_digest(config.team_id.encode(), team_id.encode()):
        raise DomainError("PERMISSION_DENIED", "Slack workspace is not authorized", 403)
