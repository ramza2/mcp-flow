# MCPFlow Infrastructure

Local Docker Compose baseline for MCPFlow (docs/08).

## Prerequisites

- Docker Engine + Docker Compose v2
- Python 3.12+ (secret generator / smoke script; stdlib only)
- Host ports free for Traefik (`8080` by default) and optional loopback diagnostics

## Architecture

```text
Browser → Traefik (:8080)
            ├─ /             → frontend
            ├─ /api/v1/*     → api
            └─ /health/*     → api

api → postgres (+ pgvector)
api → redis
api → object-storage (endpoint only; no MinIO root credentials)
```

Compose strategy: **base + local override** (no Compose Profiles in this baseline).

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Project name: `mcpflow`

## Services currently implemented

| Service | Role |
|---|---|
| `traefik` | Edge routing (file provider; **no Docker socket**) |
| `frontend` | SPA (local: Vite hot reload; base: nginx-unprivileged) |
| `api` | FastAPI control plane |
| `postgres` | PostgreSQL 17 + pgvector |
| `redis` | Future Celery broker / short coordination |
| `object-storage` | MinIO (S3-compatible) |
| `migration` | One-shot `alembic upgrade head` (same backend image) |

## Services intentionally deferred

No placeholder containers in this PR:

```text
worker
mcp-worker
factory-worker
scheduler
outbox
```

They share the backend image later via different commands/roles.

## Generate local secrets

```bash
python infra/scripts/generate_local_secrets.py
```

PowerShell:

```powershell
python infra/scripts/generate_local_secrets.py
```

Secrets are written under `infra/secrets/local/` (gitignored). Values are never printed.

Use `--force` only when you intentionally want to rotate secrets.
For Postgres role password rotation, reset **only**
`mcpflow_postgres-data` (see `infra/secrets/README.md`) — do not use
blanket `down -v` for routine rotation.

See `infra/secrets/README.md`.

## Build

```bash
docker compose -f compose.yaml -f compose.local.yaml build --pull
```

## Start

```bash
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

PowerShell (one line):

```powershell
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

## Check status

```bash
docker compose -f compose.yaml -f compose.local.yaml ps -a
```

Expected shape:

- `traefik`, `frontend`, `api`, `postgres`, `redis`, `object-storage` → running/healthy
- `migration` → exited 0

## URLs

| URL | Purpose |
|---|---|
| http://localhost:8080/ | Frontend via Traefik |
| http://localhost:8080/health/live | API liveness |
| http://localhost:8080/health/ready | API readiness (DB) |
| http://localhost:8080/api/v1/... | API v1 |
| http://127.0.0.1:8000/docs | Direct API docs (local override) |

Override host ports via root `.env` (see `.env.example`). **Do not put secrets in `.env`.**

## Smoke test

```bash
python infra/scripts/smoke_local.py --base-url http://localhost:8080
```

## Logs

```bash
docker compose -f compose.yaml -f compose.local.yaml logs -f api
docker compose -f compose.yaml -f compose.local.yaml logs migration
```

## Migration

`migration` runs `alembic upgrade head` after Postgres/Redis/Object Storage are healthy and **before** `api` starts.

API startup does **not** run migrations.

## Diagnostic ports (local override only)

Bound to loopback only:

| Service | Default |
|---|---|
| API | 127.0.0.1:8000 |
| PostgreSQL | 127.0.0.1:5432 |
| Redis | 127.0.0.1:6379 |
| MinIO API | 127.0.0.1:9000 |
| MinIO Console | 127.0.0.1:9001 |

## Stop

```bash
docker compose -f compose.yaml -f compose.local.yaml down
```

Volumes are kept.

## Reset volumes

### Postgres-only reset (secret rotation / DB re-bootstrap)

```bash
docker compose -f compose.yaml -f compose.local.yaml down
docker volume rm mcpflow_postgres-data
docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Keeps Redis and Object Storage data.

### Full local reset (destructive)

WARNING: Deletes PostgreSQL, Redis, Object Storage, and local frontend
`node_modules` volumes.

```bash
docker compose -f compose.yaml -f compose.local.yaml down -v
```

## Backend dependency lock

Production image installs from `backend/requirements.lock` (fully pinned transitive deps), then installs the local package with `--no-deps`.

Regenerate lock (from repo root):

```bash
cd backend
python3 -m venv .venv-lock
source .venv-lock/bin/activate   # Windows: .venv-lock\Scripts\activate
pip install -U pip
pip install -e .
pip freeze | grep -v -E '^-e |mcpflow-backend' | sort > requirements.lock
```

## Security notes

- No `/var/run/docker.sock` mounts
- Secrets via file mounts under `/run/secrets`
- MinIO root credentials are **not** mounted into `api`
- API DB role is `mcpflow_app` (non-superuser); Alembic uses `mcpflow_migration`
- Custom app containers run as non-root where practical
- Traefik dashboard disabled

## Troubleshooting

**Secrets missing**

```text
error ... postgres_admin_password
```

Run `python infra/scripts/generate_local_secrets.py`.

**Ready still 503**

Check `migration` exited 0 and Postgres healthy. Inspect `docker compose ... logs api migration postgres`.

**Frontend SPA 404 on refresh**

Production nginx config uses `try_files ... /index.html`. Confirm you are not hitting a stale container.

**Port already allocated**

Change `MCPFLOW_HTTP_PORT` / debug ports in `.env`.

**Container DNS works but TCP times out (nested Docker / CI VMs)**

Some restricted hosts drop Docker bridge `FORWARD` traffic. Ensure Docker can forward between containers (for example `iptables -P FORWARD ACCEPT` and allowing the `br-*` interfaces). This is an environment networking issue, not an MCPFlow Compose misconfiguration.
