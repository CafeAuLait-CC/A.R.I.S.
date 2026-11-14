from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgentCredential(BaseModel):
    model_config = {"extra": "forbid", "hide_input_in_errors": True}
    node_id: str = Field(min_length=1, max_length=128)
    token: SecretStr = Field(min_length=16)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIS_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = Field(default="sqlite+pysqlite:///.local/aris.db", repr=False)
    read_token: SecretStr = SecretStr("")
    agent_credentials: dict[str, AgentCredential] = Field(default_factory=dict)
    stale_after_seconds: int = Field(default=60, ge=1)
    offline_after_seconds: int = Field(default=180, ge=2)
    max_clock_skew_seconds: int = Field(default=60, ge=0)

    @model_validator(mode="after")
    def validate_configuration(self) -> "Settings":
        if self.offline_after_seconds <= self.stale_after_seconds:
            raise ValueError("offline threshold must exceed stale threshold")
        node_ids = [credential.node_id for credential in self.agent_credentials.values()]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("each node must have a unique agent identity")
        tokens = [
            credential.token.get_secret_value() for credential in self.agent_credentials.values()
        ]
        if len(tokens) != len(set(tokens)):
            raise ValueError("each agent must have an independent token")
        if self.read_token.get_secret_value() and self.read_token.get_secret_value() in tokens:
            raise ValueError("read and agent tokens must differ")
        if self.environment == "production":
            if not self.database_url.startswith("postgresql+psycopg://"):
                raise ValueError("production requires PostgreSQL with psycopg")
            if len(self.read_token.get_secret_value()) < 16 or not self.agent_credentials:
                raise ValueError("production requires read and node credentials")
        return self
