from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Slack
    SLACK_SIGNING_SECRET: str
    SLACK_BOT_TOKEN: str

    # DB
    DATABASE_URL: str = "sqlite:///./aris.db"

    # internal API authentication (for GPU agent)
    INTERNAL_API_TOKEN: str = "CHANGE_ME"

    # pydantic v2 config
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
    )


settings = Settings()
