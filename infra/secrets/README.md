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

## Rotate

1. `python infra/scripts/generate_local_secrets.py --force`
2. `docker compose -f compose.yaml -f compose.local.yaml down -v`
3. `docker compose -f compose.yaml -f compose.local.yaml up -d --build`

Step 2 is required so Postgres re-runs first-boot bootstrap with the new role passwords.

## What does not belong here

- Production / pilot credentials
- Values pasted into `compose.yaml`, `.env`, or Dockerfiles
