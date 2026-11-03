#!/usr/bin/env bash
# Optional protocol test server; never redeploys API/Worker or runs migrations.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$PROJECT_ROOT"

ACTION="${1:-status}"
NETWORK="mcpflow_mcp_test"
ENDPOINT="http://everything-mcp-test:3001/mcp"

if [[ ! -f .env.server ]]; then
  echo "ERROR: .env.server not found (run on the MCPFlow deployment host)." >&2
  exit 1
fi

DC=(
  docker compose
  --env-file .env.server
  -f compose.yaml
  -f compose.server.yaml
  -f compose.mcp-test.yaml
)

container_id() {
  local service="$1"
  local id
  id="$("${DC[@]}" ps -q "$service")"
  [[ -n "$id" ]] || { echo "ERROR: $service is not running." >&2; return 1; }

  local actual_project actual_service
  actual_project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$id")"
  actual_service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$id")"
  [[ "$actual_project" == "mcpflow" && "$actual_service" == "$service" ]] || {
    echo "ERROR: unexpected Compose container for $service." >&2
    return 1
  }
  printf '%s\n' "$id"
}

on_test_network() {
  local id="$1"
  [[ "$(docker inspect -f '{{if index .NetworkSettings.Networks "mcpflow_mcp_test"}}yes{{else}}no{{end}}' "$id")" == "yes" ]]
}

attach_if_needed() {
  local service="$1"
  local id
  id="$(container_id "$service")"
  if ! on_test_network "$id"; then
    docker network connect "$NETWORK" "$id"
  fi
  docker exec "$id" python -c \
    'import socket; s = socket.create_connection(("everything-mcp-test", 3001), 3); s.close()'
  echo "MCP_TEST_NETWORK_PASS: $service"
}

case "$ACTION" in
  start)
    # Only the optional test service is built/started. No backend image or
    # migration is touched; API and Worker are connected in-place.
    "${DC[@]}" up -d --no-deps --build --wait everything-mcp-test
    docker network inspect "$NETWORK" >/dev/null
    attach_if_needed api
    attach_if_needed worker
    echo "MCP_EVERYTHING_NETWORK_READY"
    echo "Register as a DRAFT with endpoint: $ENDPOINT"
    ;;
  status)
    "${DC[@]}" ps everything-mcp-test
    for service in api worker; do
      id="$(container_id "$service")"
      if on_test_network "$id"; then
        echo "MCP_TEST_NETWORK_ATTACHED: $service"
      else
        echo "MCP_TEST_NETWORK_NOT_ATTACHED: $service"
      fi
    done
    ;;
  stop)
    # Deliberately preserve existing API/Worker networks; no container is
    # restarted and no data, image, volume, or network is deleted.
    "${DC[@]}" stop everything-mcp-test
    echo "MCP_EVERYTHING_STOPPED"
    ;;
  *)
    echo "Usage: $0 {start|status|stop}" >&2
    exit 2
    ;;
esac
