"""Application configuration loaded from environment variables."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for collectors and analysis jobs."""

    environment: str = "development"
    request_timeout_seconds: float = 30.0
    user_agent: str = "news-collector/0.1"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="NEWS_COLLECTOR_",
        extra="ignore",
    )
