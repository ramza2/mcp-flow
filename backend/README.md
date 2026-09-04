# MCPFlow API

FastAPI application skeleton for the MCPFlow control plane.

Canonical design docs (`docs/01–09`) and root `AGENTS.md` are the Source of Truth.
This package does **not** yet implement MCP/Agent/Execution business features.

## Requirements

- Python **3.12+**
- PostgreSQL for real readiness checks (optional for unit/API tests)

## Install

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Environment

Copy `.env.example` to `.env` and adjust values. Never commit real secrets.

```bash
cp .env.example .env
```

Settings use the `MCPFLOW_` prefix (see `app/core/config.py`).

## Run API

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

## Health

- `GET /health/live` — process liveness
- `GET /health/ready` — readiness (DB ping when configured)

OpenAPI (when docs enabled): `GET /docs`

## Tests

```bash
pytest
```

Unit/API tests do **not** require a live PostgreSQL or Redis instance.

## Lint

```bash
ruff check .
```

## Alembic

```bash
# Generate a revision when models exist (none in this skeleton)
alembic revision -m "describe change"

# Apply migrations against DATABASE_URL
alembic upgrade head
```

Do not use `Base.metadata.create_all()` for production schema management.

## Dependency lock (Docker runtime)

Docker production images install pinned transitive dependencies from
`requirements.lock`, then install this package with `--no-deps`.

Regenerate after changing runtime dependencies in `pyproject.toml`:

```bash
python3 -m venv .venv-lock
source .venv-lock/bin/activate
pip install -U pip
pip install -e .
pip freeze | grep -v -E '^-e |mcpflow-backend' | sort > requirements.lock
```

Editable local development (`pip install -e ".[dev]"`) continues to work without the lock file.

## Architecture notes

Package boundaries under `app/` mirror `docs/03` responsibilities
(`agent`, `execution`, `mcp`, `factory`, `scheduler`, …).
Business logic for those areas is intentionally empty in this skeleton.
