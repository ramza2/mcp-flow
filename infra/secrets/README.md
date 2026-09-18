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

- `--force` — overwrite existing **ordinary** credentials/passwords only
  (`postgres_*`, `minio_*`). **Never** overwrites an existing
  `secret_master_key`.
- `--force-master-key` — **dangerous**: overwrite an existing
  `secret_master_key` after a stderr warning. Makes prior `secret_records`
  ciphertext unreadable without decrypt/re-encrypt. Not a supported
  rotation path in PR #30.
- `--dir PATH` — custom output directory (default: `infra/secrets/local`)

Generated files:

```text
postgres_admin_password
postgres_migration_password
postgres_app_password
minio_root_user
minio_root_password
secret_master_key
```

`secret_master_key` is a 32-byte AES-256-GCM key encoded as standard base64.
Compose mounts it into `worker` only as `/run/secrets/secret_master_key`
(`MCPFLOW_SECRET_MASTER_KEY_FILE`). Never commit the file.

### Master key lifecycle (not a simple rotate)

- **Initial generation** is supported (local generator / `deploy.sh` ensure).
- **Back up** the master key separately and securely. Losing it makes
  `secret_records` ciphertext permanently unreadable.
- **Do not** replace or delete `secret_master_key` while encrypted
  `secret_records` still depend on it.
- ``--force`` intentionally **cannot** rotate the master key (password
  rotation must stay safe).
- Existing ciphertext was sealed with the current key; swapping the file and
  restarting `worker` does **not** re-encrypt rows and will fail closed on
  resolve.
- A future rotation must decrypt with the old key and re-encrypt with the new
  key under a controlled migration. **Automated key rotation is not
  implemented in PR #30.**
- Resetting the Postgres data volume is **not** a master-key rotation
  mechanism (and would destroy data).

Values are URL-safe / hex-based / base64 and are **not** printed to stdout.

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
# Regenerates postgres_*/minio_* only. Existing secret_master_key is preserved.
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

Do **not** use `--force-master-key` as part of Postgres password rotation.

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
