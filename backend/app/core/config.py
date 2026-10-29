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
    object_storage_backend: str = Field(
        default="memory",
        description="Object Storage backend: memory | s3 | minio",
    )
    object_storage_bucket: str = Field(
        default="mcpflow-factory",
        description="Default bucket for Tool Factory source handoff.",
    )
    object_storage_region: str = "us-east-1"
    object_storage_access_key: str | None = None
    object_storage_secret_key: str | None = None
    object_storage_access_key_file: str | None = Field(
        default=None,
        description="Path to Object Storage access key secret file.",
    )
    object_storage_secret_key_file: str | None = Field(
        default=None,
        description="Path to Object Storage secret key secret file.",
    )
    factory_lease_seconds: int = Field(default=120, gt=0)
    factory_runtime_public_base_url: str = Field(
        default="http://factory-runtime:8090",
        description=(
            "Base URL used to advertise READY Factory Runtime MCP endpoints "
            "(compose-internal by default)."
        ),
    )
    factory_runtime_outbound_host_allowlist: str = Field(
        default="factory-mock-upstream",
        description=(
            "Comma-separated hostnames allowed as Factory runtime outbound "
            "base_url even when they resolve to private networks (pilot mock)."
        ),
    )
    factory_queued_republish_age_seconds: int = Field(
        default=30,
        ge=0,
        description=(
            "Minimum QUEUED age before outbox poller republishes analyze delivery "
            "(covers lost broker messages after Outbox mark-published)."
        ),
    )
    tool_embedding_republish_age_seconds: int = Field(
        default=30,
        ge=0,
        description=(
            "Minimum STALE/FAILED ToolEmbedding age before outbox poller "
            "republishes ensure delivery (MISSING rows are always eligible)."
        ),
    )

    execution_lease_seconds: int = Field(default=60, gt=0)
    outbox_poll_interval_seconds: float = Field(default=1.0, gt=0)
    outbox_batch_size: int = Field(default=50, ge=1, le=500)
    # Dedicated AGENT_SCHEDULE_PLAN planner (not the shared outbox poller).
    agent_schedule_plan_poll_interval_seconds: float = Field(default=1.0, gt=0)
    agent_schedule_plan_batch_size: int = Field(default=1, ge=1, le=20)
    agent_schedule_plan_lease_seconds: int = Field(default=120, gt=0)
    scheduler_poll_interval_seconds: float = Field(default=1.0, gt=0)
    scheduler_batch_size: int = Field(default=50, ge=1, le=500)
    scheduler_misfire_grace_seconds: int = Field(default=60, ge=0)
    scheduler_due_scan_limit: int = Field(default=10_000, ge=100, le=100_000)
    celery_broker_connection_timeout: float = Field(default=5.0, gt=0)
    celery_publish_connect_timeout: float = Field(default=5.0, gt=0)
    celery_publish_socket_timeout: float = Field(default=5.0, gt=0)
    celery_publish_max_retries: int = Field(default=3, ge=0, le=10)
    secret_master_key_file: str | None = Field(
        default=None,
        description="External AES-256-GCM master key file path (never commit).",
    )
    result_inline_max_bytes: int = Field(
        default=256_000,
        gt=0,
        description="Max JSONB inline result size before fail-closed (no object storage yet).",
    )

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
