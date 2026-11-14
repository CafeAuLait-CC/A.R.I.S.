import pytest
from pydantic import ValidationError

from aris.core.config import Settings
from aris.core.contracts import DomainError
from aris.core.identity import require_agent, require_read


def test_production_rejects_sqlite_and_missing_credentials():
    with pytest.raises(ValidationError, match="requires PostgreSQL"):
        Settings(_env_file=None, environment="production", database_url="sqlite:///:memory:")
    with pytest.raises(ValidationError, match="requires read and node credentials"):
        Settings(
            _env_file=None,
            environment="production",
            read_token="",
            agent_credentials={},
            database_url="postgresql+psycopg://localhost/aris",
        )


def test_agent_tokens_and_node_bindings_must_be_independent():
    with pytest.raises(ValidationError, match="independent token"):
        Settings(
            _env_file=None,
            agent_credentials={
                "a": {"node_id": "a", "token": "same-test-token-1234"},
                "b": {"node_id": "b", "token": "same-test-token-1234"},
            },
        )
    with pytest.raises(ValidationError, match="unique agent identity"):
        Settings(
            _env_file=None,
            agent_credentials={
                "a": {"node_id": "same", "token": "first-test-token-1234"},
                "b": {"node_id": "same", "token": "other-test-token-1234"},
            },
        )


def test_non_ascii_credentials_fail_closed_instead_of_crashing():
    settings = Settings(
        _env_file=None,
        read_token="test-read-token-1234",
        agent_credentials={
            "a": {"node_id": "a", "token": "test-agent-token-1234"},
        },
    )
    with pytest.raises(DomainError):
        require_read(settings, "Bearer 非法凭据")
    with pytest.raises(DomainError):
        require_agent(settings, "a", "a", "非法凭据")
