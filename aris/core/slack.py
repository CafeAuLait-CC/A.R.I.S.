import hmac
import hashlib
import time
import ssl

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import certifi

from .config import settings


_ssl_context = ssl.create_default_context(cafile=certifi.where())
slack_client = WebClient(
    token=settings.SLACK_BOT_TOKEN,
    ssl=_ssl_context,
)


def verify_slack_signature(body: bytes, sig: str | None, ts: str | None) -> bool:
    """Verify Slack request signature."""
    if not settings.SLACK_SIGNING_SECRET:
        # Decline if no SECRET is provided
        return False

    if not sig or not ts:
        return False

    # Prevent replay-attacks: 5 mins window
    try:
        if abs(time.time() - int(ts)) > 60 * 5:
            return False
    except Exception:
        return False

    base = f"v0:{ts}:{body.decode()}".encode()
    expected = (
        "v0="
        + hmac.new(
            settings.SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256
        ).hexdigest()
    )
    return hmac.compare_digest(expected, sig)


def safe_chat_post_message(channel: str, **kwargs) -> None:
    """Wrap the code to isolate the handler from crashes due to exceptions."""
    try:
        slack_client.chat_postMessage(channel=channel, **kwargs)
    except SlackApiError as e:
        # TODO: change to logging
        print(f"[Slack] chat_postMessage error: {e.response.data}")
    except Exception as e:
        print(f"[Slack] chat_postMessage error: {e}")
