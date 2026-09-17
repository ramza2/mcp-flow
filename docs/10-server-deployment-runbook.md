# MCPFlow Server Deployment Runbook

This runbook covers deployment to a Linux server that already operates a shared
Traefik instance using the Docker provider and container labels.

The server deployment does **not** start MCPFlow's bundled/file-provider Traefik.
`compose.server.yaml` disables that service and remaps the MCPFlow `edge` network
to the existing external Traefik network.

## 1. Server contract

Default values follow the company server convention:

| Setting | Default |
|---|---|
| External Traefik network | `traefik_proxy` |
| HTTP entrypoint | `web` |
| HTTPS entrypoint | `websecure` |
| ACME certificate resolver | `letsencrypt` |
| Application environment | `pilot` |
| API docs | disabled |
| Session cookie secure flag | enabled |

Only `frontend` and `api` join the external Traefik network.

`postgres`, `redis`, `object-storage`, `worker`, `outbox`, and `migration` remain
on MCPFlow internal networks and publish no host ports.

Routing:

```text
http://<host>/*                 -> HTTPS redirect
https://<host>/                 -> frontend:8080
https://<host>/api/*            -> api:8000
https://<host>/health/*         -> api:8000
https://<host>/docs*            -> api:8000 (only when docs are enabled)
https://<host>/openapi.json     -> api:8000 (only when docs are enabled)
```

## 2. Prerequisites

- Docker Engine
- Docker Compose v2
- Existing Traefik container connected to the external Docker network
- DNS record for the MCPFlow host pointing to the server
- Python 3 available on the host only for one-time secret-file generation
- `curl` for deployment smoke checks

Verify the shared Traefik network:

```bash
docker network inspect traefik_proxy
```

If the server uses a different network name, enter it during first deployment.

## 3. First deployment

Checkout the desired MCPFlow revision and run:

```bash
./scripts/deploy.sh
```

On first run the script creates `.env.server` interactively. The service domain
is required; lower-risk settings provide defaults on Enter.

Example:

```text
Service domain (required): mcpflow.example.com
Traefik Docker network [traefik_proxy]:
Traefik HTTP entrypoint [web]:
Traefik HTTPS entrypoint [websecure]:
Traefik certificate resolver [letsencrypt]:
Application environment [pilot]:
Log level [INFO]:
Enable API docs [y/N]:
Execution lease seconds [60]:
Outbox poll interval seconds [1.0]:
Outbox batch size [50]:
Deployment health timeout seconds [180]:
```

The generated `.env.server` is mode `0600` and is gitignored.

Server-only Docker secret files are generated under:

```text
infra/secrets/server/
```

Existing secret files are preserved. Deployment never prints secret values.

## 4. Deployment sequence

`./scripts/deploy.sh` performs:

```text
configuration load/create
→ Docker/Compose prerequisite check
→ external Traefik network check
→ server secret generation/preservation
→ merged Compose validation
→ image build (git SHA image tag)
→ postgres/redis/object-storage startup
→ Alembic migration one-shot
→ api/worker/outbox/frontend startup
→ migration exit-code verification
→ public HTTPS liveness/readiness checks
```

The base Compose dependency contract prevents `api`, `worker`, and `outbox` from
starting successfully before the migration service completes.

The deployment script does not automatically pull or change Git branches. The
currently checked-out clean revision is what gets deployed.

## 5. Reconfiguration

```bash
./scripts/deploy.sh --reconfigure
```

Existing values become prompt defaults.

For a configuration template, see `.env.server.example`.

## 6. Operational commands

Status:

```bash
./scripts/deploy.sh --status
```

Follow logs:

```bash
./scripts/deploy.sh --logs
```

Restart runtime services without image rebuild or migration:

```bash
./scripts/deploy.sh --restart
```

Deploy existing images without rebuilding:

```bash
./scripts/deploy.sh --no-build
```

Stop/remove MCPFlow containers:

```bash
./scripts/deploy.sh --down
```

`--down` does not pass `-v`; PostgreSQL, Redis, and Object Storage named volumes
are retained.

## 7. Health verification

Public checks:

```bash
curl -fsS https://<host>/health/live
curl -fsS https://<host>/health/ready
```

Container state:

```bash
./scripts/deploy.sh --status
```

Expected:

- `postgres`, `redis`, `object-storage`, `api`, `worker`, `outbox`, `frontend`:
  running/healthy as applicable.
- `migration`: exited with code 0.
- no MCPFlow-owned Traefik container in the server deployment.

## 8. Troubleshooting

### External Traefik network not found

```text
[ERROR] External Traefik network '...' does not exist.
```

Verify the server network:

```bash
docker network ls
docker network inspect traefik_proxy
```

Then reconfigure:

```bash
./scripts/deploy.sh --reconfigure
```

### Public health times out

Check routing labels and application logs:

```bash
./scripts/deploy.sh --status
./scripts/deploy.sh --logs
```

Also confirm DNS resolves to the server and the shared Traefik has the configured
`web` / `websecure` entrypoints and certificate resolver.

### Migration fails

The script prints migration logs and exits non-zero. Do not force API rollout by
removing the migration dependency. Correct the migration/database issue and
rerun the deployment.

### Existing server secrets

Re-running deploy preserves secret files. Do not delete `infra/secrets/server/`
or the PostgreSQL volume during ordinary application deployment.

## 9. Rollback

Application rollback is revision-based:

```text
checkout known-good revision
→ ./scripts/deploy.sh
→ health verification
```

Database downgrade is never an automatic rollback action. Confirm backward
compatibility of already-applied migrations before rolling application code
back.
