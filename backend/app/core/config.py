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

    redis_url: str = "redis://localhost:6379/0"
    object_storage_endpoint: str = "http://localhost:9000"

    execution_lease_seconds: int = Field(default=60, gt=0)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    celery_broker_connection_timeout: float = Field(default=5.0, gt=0)
    celery_publish_connect_timeout: float = Field(default=5.0, gt=0)
    celery_publish_socket_timeout: float = Field(default=5.0, gt=0)
    celery_publish_max_retries: int = Field(default=3, ge=0, le=10)

    request_id_header: str = "X-Request-ID"
    request_id_max_length: int = 128

    session_cookie_name: str = "mcpflow_session"
    session_ttl_seconds: int = Field(default=28800, gt=0)
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    csrf_header_name: str = "X-CSRF-Token"


@lru_cache
def get_settings() -> Settings:
    return Settings()
