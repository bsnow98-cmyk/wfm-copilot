"""
Centralised settings, populated from environment variables.

Why pydantic-settings: type-checked config beats hand-rolled os.getenv() calls,
and you get a single object you can pass around or import anywhere.
"""
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Postgres
    postgres_user: str = Field("wfm")
    postgres_password: str = Field("wfm_dev_password")
    postgres_db: str = Field("wfm_copilot")
    postgres_host: str = Field("postgres")
    postgres_port: int = Field(5432)

    # Redis
    redis_host: str = Field("redis")
    redis_port: int = Field(6379)

    # API
    api_host: str = Field("0.0.0.0")
    api_port: int = Field(8000)
    log_level: str = Field("INFO")

    # Anthropic (Phase 6)
    anthropic_api_key: str | None = Field(None)
    anthropic_model: str = Field("claude-sonnet-4-6")

    # Phase 6 — single shared password gate.
    # Unset = open in dev. Set = required for /chat (and all routes per spec).
    wfm_demo_password: str | None = Field(None)

    # Solver/tool timeout for chat tools that touch heavy services.
    tool_timeout_seconds: int = Field(30)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Cached so we only parse env once per process."""
    return Settings()
