from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings (MCPFLOW_* environment variables)."""

    model_config = SettingsConfigDict(
        env_prefix="MCPFLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "MCPFlow API"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"

    docs_enabled: bool = True

    database_url: str = Field(
        default="postgresql+asyncpg://mcpflow:change-me@localhost:5432/mcpflow",
        description="Async SQLAlchemy PostgreSQL URL",
    )

    # Placeholders for future canonical services (docs/08) — not connected in this skeleton.
    redis_url: str = "redis://localhost:6379/0"
    object_storage_endpoint: str = "http://localhost:9000"

    request_id_header: str = "X-Request-ID"
    request_id_max_length: int = 128


@lru_cache
def get_settings() -> Settings:
    return Settings()
