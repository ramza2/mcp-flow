# Local secrets

Local credentials for Docker Compose live in:

```text
infra/secrets/local/
```

This directory is **gitignored**. Never commit secret files.

## Generate

From repository root:

```bash
python infra/scripts/generate_local_secrets.py
```

Options:

- `--force` — overwrite existing secret files
- `--dir PATH` — custom output directory (default: `infra/secrets/local`)

Generated files:

```text
postgres_admin_password
postgres_migration_password
postgres_app_password
minio_root_user
minio_root_password
```

Values are URL-safe / hex-based and are **not** printed to stdout.

On Unix-like systems files are created with mode `0644` so non-root
container users can read Compose bind-mounted secrets. These files are
gitignored and for **local development only** — never reuse them for
production.

## Rotate (Postgres role passwords)

Postgres bootstrap creates `mcpflow_migration` / `mcpflow_app` passwords
**only on first volume init**. After regenerating DB password secrets with
`--force`, reset **only** the PostgreSQL data volume so bootstrap can
re-run. Redis and Object Storage volumes stay intact.

```bash
python infra/scripts/generate_local_secrets.py --force

docker compose -f compose.yaml -f compose.local.yaml down

docker volume rm mcpflow_postgres-data

docker compose -f compose.yaml -f compose.local.yaml up -d --build
```

Canonical volume name for Postgres in this baseline: `mcpflow_postgres-data`
(from Compose `name: mcpflow` + volume key `postgres-data`).

MinIO root credentials are read from secret files at process start. Rotating
`minio_root_*` with `--force` and restarting `object-storage` is enough —
do **not** delete `mcpflow_object-storage-data` for a DB password rotation.

## Full local reset (destructive)

WARNING: This deletes **all** local persistent Compose volumes for the
project — PostgreSQL, Redis, Object Storage, and local frontend
`node_modules` cache. Use only when you intentionally want a clean slate.

```bash
docker compose -f compose.yaml -f compose.local.yaml down -v
```

Do **not** use `down -v` as the default secret-rotation step.

## What does not belong here

- Production / pilot credentials
- Values pasted into `compose.yaml`, `.env`, or Dockerfiles
