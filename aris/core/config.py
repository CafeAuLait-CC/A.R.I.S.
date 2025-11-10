from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Slack
    SLACK_SIGNING_SECRET: str = "xxxx"
    SLACK_BOT_TOKEN: str = "xoxb-xxx"

    # DB
    DATABASE_URL: str = "sqlite:///./aris.db"

    # internal API authentication (for GPU agent)
    INTERNAL_API_TOKEN: str = "CHANGE_ME"

    # GPU seconds until idle
    STALE_HEARTBEAT_SECS: int = 60

    # pydantic v2 config
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
