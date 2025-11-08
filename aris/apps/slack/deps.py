import hmac
import hashlib
import time
from fastapi import Header, HTTPException
from ..core.config import settings

def verify_slack_signature(
    x_slack_request_timestamp: str = Header(..., alias="X-Slack-Request-Timestamp"),
    x_slack_signature: str = Header(..., alias="X-Slack-Signature"),
    body: bytes = b"",
):
    # simplified: get body before calling this method
    ts = int(x_slack_request_timestamp)
    if abs(time.time() - ts) > 60 * 5:
        raise HTTPException(status_code=400, detail="timestamp expired")

    sig_basestring = f"v0:{ts}:{body.decode()}".encode()
    my_sig = "v0=" + hmac.new(
        settings.SLACK_SIGNING_SECRET.encode(),
        sig_basestring,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(my_sig, x_slack_signature):
        raise HTTPException(status_code=403, detail="invalid slack signature")
