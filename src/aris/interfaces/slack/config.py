from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class SlackSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ARIS_SLACK_", env_file=".env.slack", extra="ignore", hide_input_in_errors=True
    )
    signing_secret: SecretStr = SecretStr("")
    bot_token: SecretStr = SecretStr("")
    team_id: str = Field(default="", max_length=128)
    signature_window_seconds: int = Field(default=300, ge=1, le=300)
    max_body_bytes: int = Field(default=262144, ge=1024, le=1048576)
    job_lease_seconds: int = Field(default=60, ge=30, le=600)
    job_max_attempts: int = Field(default=5, ge=1, le=20)

    @property
    def configured(self) -> bool:
        return bool(
            self.signing_secret.get_secret_value()
            and self.bot_token.get_secret_value()
            and self.team_id
        )
