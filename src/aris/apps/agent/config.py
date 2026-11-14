from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIS_AGENT_", env_file=".env.agent", extra="ignore", hide_input_in_errors=True
    )
    id: str = Field(min_length=1, max_length=128)
    node_id: str = Field(min_length=1, max_length=128)
    token: SecretStr = Field(min_length=16)
    api_url: str = "http://127.0.0.1:8000"
    spool_path: Path = Path(".local/agent-spool.sqlite3")
    interval_seconds: int = Field(default=15, ge=1)
    max_buffered_batches: int = Field(default=10000, ge=1)

    @model_validator(mode="after")
    def validate_transport(self) -> "AgentSettings":
        url = urlsplit(self.api_url)
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ValueError("API URL must not contain credentials, query or fragment")
        if url.scheme != "https" and not (
            url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError("use HTTPS, or an HTTP loopback endpoint over an SSH tunnel")
        return self
