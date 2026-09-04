"""Shared constants — canonical queue/service names from docs/08 (placeholders only)."""

# Canonical control-plane API prefix (docs/06). Not a runtime-configurable setting.
API_V1_PREFIX = "/api/v1"

# Canonical queues (docs/08). Worker wiring is out of scope for this skeleton.
CANONICAL_QUEUES = (
    "agent",
    "execution",
    "mcp_stdio",
    "factory",
    "maintenance",
)

# Canonical deployment services (docs/08). Compose is out of scope for this skeleton.
CANONICAL_SERVICES = (
    "traefik",
    "frontend",
    "api",
    "worker",
    "mcp-worker",
    "factory-worker",
    "scheduler",
    "outbox",
    "postgres",
    "redis",
    "object-storage",
    "migration",
)
