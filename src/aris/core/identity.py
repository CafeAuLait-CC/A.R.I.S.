import hmac

from aris.core.config import Settings
from aris.core.contracts import DomainError


def require_read(settings: Settings, authorization: str | None) -> None:
    expected = settings.read_token.get_secret_value()
    if not expected or not hmac.compare_digest(
        (authorization or "").encode(), f"Bearer {expected}".encode()
    ):
        raise DomainError("PERMISSION_DENIED", "Read credentials required", 401)


def require_agent(settings: Settings, agent_id: str, node_id: str, token: str | None) -> None:
    credential = settings.agent_credentials.get(agent_id)
    if credential is None or not hmac.compare_digest(
        (token or "").encode(), credential.token.get_secret_value().encode()
    ):
        raise DomainError("PERMISSION_DENIED", "Invalid agent credentials", 401)
    if credential.node_id != node_id:
        raise DomainError("PERMISSION_DENIED", "Agent cannot report for this node", 403)
