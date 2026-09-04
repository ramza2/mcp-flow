# Multi-stage Backend image for MCPFlow API / migration (same image, different command).
# Targets: builder → runtime (prod) → dev (local hot-reload + pytest/ruff)

ARG PYTHON_IMAGE=python:3.12.10-slim-bookworm

FROM ${PYTHON_IMAGE} AS builder
WORKDIR /build
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.lock /build/requirements.lock
COPY backend/pyproject.toml backend/README.md /build/
COPY backend/app /build/app
COPY backend/alembic /build/alembic
COPY backend/alembic.ini /build/alembic.ini

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r /build/requirements.lock \
    && /opt/venv/bin/pip install --no-deps .

FROM ${PYTHON_IMAGE} AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/home/mcpflow

RUN groupadd --system --gid 10001 mcpflow \
    && useradd --system --uid 10001 --gid mcpflow --create-home --home-dir /home/mcpflow mcpflow

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /build/app /app/app
COPY --from=builder /build/alembic /app/alembic
COPY --from=builder /build/alembic.ini /app/alembic.ini
COPY infra/docker/backend-entrypoint.sh /entrypoint.sh

RUN chmod 755 /entrypoint.sh \
    && chown -R mcpflow:mcpflow /app /home/mcpflow

USER mcpflow
EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM runtime AS dev
USER root
COPY backend/pyproject.toml /tmp/pyproject.toml
# Install optional dev extras without mutating the locked runtime set unnecessarily.
RUN /opt/venv/bin/pip install --no-cache-dir \
      "pytest>=8.3.0,<9.0.0" \
      "pytest-asyncio>=0.24.0,<1.0.0" \
      "httpx>=0.28.0,<1.0.0" \
      "ruff>=0.8.0,<1.0.0" \
    && chown -R mcpflow:mcpflow /opt/venv
USER mcpflow
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
