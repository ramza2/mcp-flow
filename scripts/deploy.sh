#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env.server"
BASE_COMPOSE="${REPO_ROOT}/compose.yaml"
SERVER_COMPOSE="${REPO_ROOT}/compose.server.yaml"
SECRETS_DIR="${REPO_ROOT}/infra/secrets/server"

ACTION="deploy"
RECONFIGURE=false
BUILD=true

usage() {
  cat <<'EOF'
Usage:
  ./scripts/deploy.sh [options]

Default:
  Interactive first-time server deployment using the host's existing Traefik.

Options:
  --reconfigure  Re-enter server deployment settings.
  --status       Show container status.
  --logs         Follow server deployment logs.
  --restart      Restart runtime services without rebuilding or migrating.
  --down         Stop/remove MCPFlow containers and networks; named volumes are kept.
  --no-build     Deploy without rebuilding images.
  -h, --help     Show this help.
EOF
}

die() {
  echo "[ERROR] $*" >&2
  exit 1
}

info() {
  echo "[INFO] $*"
}

for arg in "$@"; do
  case "${arg}" in
    --reconfigure) RECONFIGURE=true ;;
    --status) ACTION="status" ;;
    --logs) ACTION="logs" ;;
    --restart) ACTION="restart" ;;
    --down) ACTION="down" ;;
    --no-build) BUILD=false ;;
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: ${arg}" ;;
  esac
done

command -v docker >/dev/null 2>&1 || die "docker is required."
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required."

[[ -f "${BASE_COMPOSE}" ]] || die "Missing ${BASE_COMPOSE}"
[[ -f "${SERVER_COMPOSE}" ]] || die "Missing ${SERVER_COMPOSE}"

env_get() {
  local key="$1"
  [[ -f "${ENV_FILE}" ]] || return 0
  awk -F= -v key="${key}" '
    $1 == key {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "${ENV_FILE}"
}

prompt_required() {
  local prompt="$1"
  local value=""
  while [[ -z "${value}" ]]; do
    read -r -p "${prompt}: " value
  done
  printf '%s' "${value}"
}

prompt_default() {
  local prompt="$1"
  local default="$2"
  local value=""
  read -r -p "${prompt} [${default}]: " value
  printf '%s' "${value:-${default}}"
}

prompt_bool() {
  local prompt="$1"
  local default="$2"
  local suffix="[y/N]"
  [[ "${default}" == "true" ]] && suffix="[Y/n]"
  local answer=""
  read -r -p "${prompt} ${suffix}: " answer
  if [[ -z "${answer}" ]]; then
    printf '%s' "${default}"
    return
  fi
  case "${answer}" in
    y|Y|yes|YES) printf 'true' ;;
    n|N|no|NO) printf 'false' ;;
    *) die "Expected yes or no for: ${prompt}" ;;
  esac
}

validate_host() {
  local host="$1"
  [[ "${host}" =~ ^[A-Za-z0-9.-]+$ ]] || die "Invalid host: ${host}"
  [[ "${host}" == *.* ]] || die "Host must be a DNS name, e.g. mcpflow.example.com"
}

validate_simple_name() {
  local label="$1"
  local value="$2"
  [[ "${value}" =~ ^[A-Za-z0-9_.-]+$ ]] || die "Invalid ${label}: ${value}"
}

configure() {
  echo
  echo "MCPFlow server deployment configuration"
  echo

  local old_host old_network old_http old_https old_resolver old_env old_log old_docs
  local old_lease old_poll old_batch old_timeout

  old_host="$(env_get MCPFLOW_HOST)"
  old_network="$(env_get MCPFLOW_TRAEFIK_NETWORK)"
  old_http="$(env_get MCPFLOW_TRAEFIK_HTTP_ENTRYPOINT)"
  old_https="$(env_get MCPFLOW_TRAEFIK_HTTPS_ENTRYPOINT)"
  old_resolver="$(env_get MCPFLOW_TRAEFIK_CERTRESOLVER)"
  old_env="$(env_get MCPFLOW_ENVIRONMENT)"
  old_log="$(env_get MCPFLOW_LOG_LEVEL)"
  old_docs="$(env_get MCPFLOW_DOCS_ENABLED)"
  old_lease="$(env_get MCPFLOW_EXECUTION_LEASE_SECONDS)"
  old_poll="$(env_get MCPFLOW_OUTBOX_POLL_INTERVAL_SECONDS)"
  old_batch="$(env_get MCPFLOW_OUTBOX_BATCH_SIZE)"
  old_timeout="$(env_get MCPFLOW_DEPLOY_HEALTH_TIMEOUT)"

  local host
  if [[ -n "${old_host}" ]]; then
    host="$(prompt_default "Service domain" "${old_host}")"
  else
    host="$(prompt_required "Service domain (required)")"
  fi

  local network http_entrypoint https_entrypoint resolver environment log_level docs_enabled
  local lease_seconds poll_seconds batch_size health_timeout

  network="$(prompt_default "Traefik Docker network" "${old_network:-traefik_proxy}")"
  http_entrypoint="$(prompt_default "Traefik HTTP entrypoint" "${old_http:-web}")"
  https_entrypoint="$(prompt_default "Traefik HTTPS entrypoint" "${old_https:-websecure}")"
  resolver="$(prompt_default "Traefik certificate resolver" "${old_resolver:-letsencrypt}")"
  environment="$(prompt_default "Application environment" "${old_env:-pilot}")"
  log_level="$(prompt_default "Log level" "${old_log:-INFO}")"
  docs_enabled="$(prompt_bool "Enable API docs" "${old_docs:-false}")"
  lease_seconds="$(prompt_default "Execution lease seconds" "${old_lease:-60}")"
  poll_seconds="$(prompt_default "Outbox poll interval seconds" "${old_poll:-1.0}")"
  batch_size="$(prompt_default "Outbox batch size" "${old_batch:-50}")"
  health_timeout="$(prompt_default "Deployment health timeout seconds" "${old_timeout:-180}")"

  validate_host "${host}"
  validate_simple_name "Traefik network" "${network}"
  validate_simple_name "HTTP entrypoint" "${http_entrypoint}"
  validate_simple_name "HTTPS entrypoint" "${https_entrypoint}"
  validate_simple_name "certificate resolver" "${resolver}"
  validate_simple_name "application environment" "${environment}"
  validate_simple_name "log level" "${log_level}"
  [[ "${lease_seconds}" =~ ^[1-9][0-9]*$ ]] || die "Execution lease seconds must be a positive integer."
  [[ "${batch_size}" =~ ^[1-9][0-9]*$ ]] || die "Outbox batch size must be a positive integer."
  (( batch_size <= 500 )) || die "Outbox batch size must be <= 500."
  [[ "${health_timeout}" =~ ^[1-9][0-9]*$ ]] || die "Health timeout must be a positive integer."
  [[ "${poll_seconds}" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "Outbox poll interval must be numeric."
  awk "BEGIN { exit !(${poll_seconds} > 0) }" || die "Outbox poll interval must be > 0."

  umask 077
  cat > "${ENV_FILE}" <<EOF
# Generated by scripts/deploy.sh. Do not commit.
MCPFLOW_HOST=${host}
MCPFLOW_TRAEFIK_NETWORK=${network}
MCPFLOW_TRAEFIK_HTTP_ENTRYPOINT=${http_entrypoint}
MCPFLOW_TRAEFIK_HTTPS_ENTRYPOINT=${https_entrypoint}
MCPFLOW_TRAEFIK_CERTRESOLVER=${resolver}

MCPFLOW_ENVIRONMENT=${environment}
MCPFLOW_DEBUG=false
MCPFLOW_LOG_LEVEL=${log_level}
MCPFLOW_DOCS_ENABLED=${docs_enabled}
MCPFLOW_SESSION_COOKIE_SECURE=true

MCPFLOW_EXECUTION_LEASE_SECONDS=${lease_seconds}
MCPFLOW_OUTBOX_POLL_INTERVAL_SECONDS=${poll_seconds}
MCPFLOW_OUTBOX_BATCH_SIZE=${batch_size}

MCPFLOW_DEPLOY_HEALTH_TIMEOUT=${health_timeout}
EOF
  chmod 600 "${ENV_FILE}"
  info "Saved ${ENV_FILE}"
}

ensure_config() {
  if [[ ! -f "${ENV_FILE}" || "${RECONFIGURE}" == "true" ]]; then
    configure
  fi
}

compose() {
  docker compose \
    --env-file "${ENV_FILE}" \
    -f "${BASE_COMPOSE}" \
    -f "${SERVER_COMPOSE}" \
    "$@"
}

ensure_server_secrets() {
  command -v python3 >/dev/null 2>&1 || die "python3 is required to generate server secrets."
  mkdir -p "${SECRETS_DIR}"
  python3 "${REPO_ROOT}/infra/scripts/generate_local_secrets.py" --dir "${SECRETS_DIR}"
}

wait_for_url() {
  local url="$1"
  local timeout="$2"
  local started now
  started="$(date +%s)"
  while true; do
    if curl --fail --silent --show-error --max-time 5 "${url}" >/dev/null 2>&1; then
      return 0
    fi
    now="$(date +%s)"
    if (( now - started >= timeout )); then
      return 1
    fi
    sleep 3
  done
}

show_identity() {
  local branch="unknown"
  local sha="unknown"
  if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    branch="$(git -C "${REPO_ROOT}" branch --show-current || true)"
    sha="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD || true)"
  fi
  echo "Branch              : ${branch:-detached}"
  echo "Commit              : ${sha}"
  echo "Host                : $(env_get MCPFLOW_HOST)"
  echo "Traefik network     : $(env_get MCPFLOW_TRAEFIK_NETWORK)"
  echo "Environment         : $(env_get MCPFLOW_ENVIRONMENT)"
  echo "Image tag           : ${MCPFLOW_IMAGE_TAG}"
}

ensure_config

case "${ACTION}" in
  status)
    compose ps -a
    exit 0
    ;;
  logs)
    compose logs -f --tail=200 api worker outbox frontend migration postgres redis object-storage
    exit 0
    ;;
  restart)
    compose restart api worker outbox frontend
    compose ps -a
    exit 0
    ;;
  down)
    read -r -p "Stop MCPFlow server containers? Named volumes will be kept. [y/N]: " answer
    case "${answer}" in
      y|Y|yes|YES) compose down --remove-orphans ;;
      *) info "Cancelled." ;;
    esac
    exit 0
    ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required for deployment health checks."

if command -v git >/dev/null 2>&1 && git -C "${REPO_ROOT}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git -C "${REPO_ROOT}" diff --quiet || die "Tracked working-tree changes exist. Commit/stash them before server deployment."
  git -C "${REPO_ROOT}" diff --cached --quiet || die "Staged changes exist. Commit/stash them before server deployment."
fi

CURRENT_SHA="$(git -C "${REPO_ROOT}" rev-parse --short=12 HEAD 2>/dev/null || echo local)"
: "${MCPFLOW_IMAGE_TAG:=${CURRENT_SHA}}"
export MCPFLOW_IMAGE_TAG

TRAEFIK_NETWORK="$(env_get MCPFLOW_TRAEFIK_NETWORK)"
MCPFLOW_HOST_VALUE="$(env_get MCPFLOW_HOST)"
HEALTH_TIMEOUT="$(env_get MCPFLOW_DEPLOY_HEALTH_TIMEOUT)"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"

[[ -n "${TRAEFIK_NETWORK}" ]] || die "MCPFLOW_TRAEFIK_NETWORK is empty."
[[ -n "${MCPFLOW_HOST_VALUE}" ]] || die "MCPFLOW_HOST is empty."

docker network inspect "${TRAEFIK_NETWORK}" >/dev/null 2>&1 \
  || die "External Traefik network '${TRAEFIK_NETWORK}' does not exist."

ensure_server_secrets

info "Validating merged Compose configuration..."
compose config >/dev/null

echo
show_identity
echo

if [[ "${BUILD}" == "true" ]]; then
  info "Building MCPFlow images..."
  compose build
fi

info "Starting MCPFlow server deployment..."
compose up -d --remove-orphans

info "Container status:"
compose ps -a

MIGRATION_ID="$(compose ps -aq migration)"
[[ -n "${MIGRATION_ID}" ]] || die "Migration container was not created."
MIGRATION_EXIT="$(docker inspect -f '{{.State.ExitCode}}' "${MIGRATION_ID}")"
if [[ "${MIGRATION_EXIT}" != "0" ]]; then
  compose logs --tail=200 migration
  die "Migration failed with exit code ${MIGRATION_EXIT}."
fi

LIVE_URL="https://${MCPFLOW_HOST_VALUE}/health/live"
READY_URL="https://${MCPFLOW_HOST_VALUE}/health/ready"

info "Waiting for ${LIVE_URL}"
wait_for_url "${LIVE_URL}" "${HEALTH_TIMEOUT}" || {
  compose logs --tail=200 api frontend
  die "Public liveness check failed."
}

info "Waiting for ${READY_URL}"
wait_for_url "${READY_URL}" "${HEALTH_TIMEOUT}" || {
  compose logs --tail=200 api postgres
  die "Public readiness check failed."
}

echo
echo "MCPFlow deployment complete."
echo "  URL       : https://${MCPFLOW_HOST_VALUE}/"
echo "  Liveness  : ${LIVE_URL}"
echo "  Readiness : ${READY_URL}"
echo "  Image tag : ${MCPFLOW_IMAGE_TAG}"
