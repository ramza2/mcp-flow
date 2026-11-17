"""Activate → Outbox → Celery/Redis maintenance → Mock Embedding → READY → retrieval.

Reuses the PR #232 dedicated loopback ``redis-server`` pattern. Never FLUSHDB /
mutates a pre-existing Redis. Does not pass via manual READY seed or direct
``ToolEmbeddingService.ensure_embedding`` / ``upsert_ready_if_current``.
Celery ``mcpflow.maintenance.ensure_tool_embedding`` owns the provider call.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from shutil import which
from typing import Any
from urllib.parse import urlparse

import pytest
from app.core.config import get_settings
from app.domain.enums import (
    AgentToolGrantEffect,
    MCPServerStatus,
    MCPToolStatus,
    ResourceGrantResourceType,
    ToolEmbeddingStatus,
    ToolVersionValidationStatus,
    UserStatus,
)
from app.execution.queue import OutboxRelayService, validate_tool_embedding_ensure_event
from app.infrastructure.celery_app import celery_app
from app.infrastructure.queue import CeleryExecutionQueuePublisher
from app.models.outbox import OutboxEvent
from app.repositories.agent import AgentRepository
from app.repositories.agent_tool_grant import AgentToolGrantRepository
from app.repositories.agent_version import AgentVersionRepository
from app.repositories.embedding_profile import EmbeddingProfileRepository
from app.repositories.llm_profile import LLMProfileRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from app.repositories.role import PermissionRepository
from app.repositories.tool_embedding import ToolEmbeddingRepository
from app.schemas.auth import (
    ResourceGrantCreate,
    RoleCreate,
    RolePermissionReplaceRequest,
    UserCreate,
    UserRoleReplaceRequest,
)
from app.search.tool_retrieval import ToolRetrievalService
from app.services.authorization import ResourceGrantService
from app.services.mcp_tool import MCPToolService
from app.services.role import RoleService
from app.services.user import UserService
from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_REDIS_DIR = Path("/tmp/mcpflow-test-redis-embedding-e2e")
_ENSURE_TASK_NAME = "mcpflow.maintenance.ensure_tool_embedding"
_EMBED_DIM = 4
_FIXED_VECTOR = [0.25, 0.5, 0.75, 1.0]


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _redis_ping(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            pass
    except OSError:
        return False
    try:
        import redis

        return redis.Redis(host=host, port=port, socket_connect_timeout=0.5).ping() is True
    except Exception:
        return False


def _stop_redis_process(pid_path: Path) -> None:
    if not pid_path.is_file():
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid_path.unlink(missing_ok=True)
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    pid_path.unlink(missing_ok=True)


def _start_dedicated_redis() -> tuple[str, Path]:
    """Spawn a throwaway redis-server on a free loopback port (test-owned)."""
    if which("redis-server") is None:
        pytest.skip(
            "redis-server binary required for dedicated broker E2E "
            "(ops Redis is not used; FLUSHDB on shared Redis is forbidden)."
        )
    _REDIS_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = _REDIS_DIR / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_path = run_dir / "redis.pid"
    port = _free_loopback_port()
    host = "127.0.0.1"
    proc = subprocess.Popen(
        [
            "redis-server",
            "--port",
            str(port),
            "--bind",
            host,
            "--dir",
            str(run_dir),
            "--pidfile",
            str(pid_path),
            "--logfile",
            str(run_dir / "redis.log"),
            "--dbfilename",
            "dump.rdb",
            "--save",
            "",
            "--appendonly",
            "no",
            "--daemonize",
            "yes",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=10)
    deadline = time.time() + 5
    while time.time() < deadline:
        if _redis_ping(host, port):
            return f"redis://{host}:{port}/0", pid_path
        time.sleep(0.05)
    _stop_redis_process(pid_path)
    pytest.skip(f"Dedicated redis-server failed to become ready on {host}:{port}")


class _MockEmbeddingServer:
    """OpenAI-compatible /v1/embeddings HTTP server for worker ModelProviderClient."""

    def __init__(self, *, dimension: int = _EMBED_DIM) -> None:
        self.embed_call_count = 0
        self.last_inputs: list[str] = []
        self._dimension = dimension
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                path = urlparse(self.path).path.rstrip("/")
                if not path.endswith("/embeddings"):
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.loads(raw.decode("utf-8"))
                inputs = body.get("input")
                if isinstance(inputs, str):
                    inputs = [inputs]
                if not isinstance(inputs, list):
                    self.send_response(400)
                    self.end_headers()
                    return
                with parent._lock:
                    parent.embed_call_count += 1
                    parent.last_inputs = [str(item) for item in inputs]
                data = [
                    {
                        "index": i,
                        "embedding": list(_FIXED_VECTOR[: parent._dimension]),
                        "object": "embedding",
                    }
                    for i, _ in enumerate(inputs)
                ]
                payload = {
                    "object": "list",
                    "data": data,
                    "model": body.get("model") or "emb-mock",
                }
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def _wait_task_postrun(*, task_id: str) -> threading.Event:
    done = threading.Event()
    expected_task_id = task_id

    def _on_postrun(
        sender: object = None,
        task_id: str | None = None,
        **_kwargs: object,
    ) -> None:
        if task_id == expected_task_id:
            done.set()

    task_postrun.connect(_on_postrun, weak=False)
    done._mcpflow_disconnect = (  # type: ignore[attr-defined]
        lambda: task_postrun.disconnect(_on_postrun)
    )
    return done


@pytest.fixture
def isolated_redis_broker() -> Iterator[str]:
    """Dedicated redis-server + restore env/settings/Celery on teardown."""
    previous_env = {
        "MCPFLOW_REDIS_URL": os.environ.get("MCPFLOW_REDIS_URL"),
        "MCPFLOW_DATABASE_URL": os.environ.get("MCPFLOW_DATABASE_URL"),
    }
    previous_broker_url = celery_app.conf.broker_url
    previous_result_backend = celery_app.conf.result_backend

    db_url = os.environ.get("TEST_DATABASE_URL", "").strip()
    if not db_url:
        pytest.skip("TEST_DATABASE_URL is not set")

    redis_url, pid_path = _start_dedicated_redis()
    parsed = urlparse(redis_url)
    assert parsed.hostname in {"127.0.0.1", "localhost"}

    os.environ["MCPFLOW_REDIS_URL"] = redis_url
    os.environ["MCPFLOW_DATABASE_URL"] = db_url
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.redis_url == redis_url
    assert settings.database_url == db_url
    celery_app.conf.broker_url = redis_url
    celery_app.conf.result_backend = None

    try:
        yield redis_url
    finally:
        _stop_redis_process(pid_path)
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        celery_app.conf.broker_url = previous_broker_url
        celery_app.conf.result_backend = previous_result_backend
        try:
            pool = getattr(celery_app, "pool", None)
            if pool is not None:
                pool.force_close_all()
        except Exception:
            pass
        get_settings.cache_clear()


async def _seed_discovered_valid_tool(
    session: AsyncSession,
    *,
    remote_name: str,
    description: str,
) -> tuple[uuid.UUID, uuid.UUID, int]:
    server = await MCPServerRepository(session).create(
        code=f"srv-{uuid.uuid4().hex[:8]}",
        name="Emb Auto Index E2E Server",
        transport_type="STREAMABLE_HTTP",
        endpoint_url="https://mcp.test/mcp",
        status=MCPServerStatus.ACTIVE,
    )
    tools = MCPToolRepository(session)
    tool = await tools.create_tool(
        mcp_server_id=server.id,
        remote_name=remote_name,
        display_name="Weather Forecast Lookup",
        tags=["weather", "forecast"],
        status=MCPToolStatus.DISCOVERED,
    )
    version = await tools.create_version(
        mcp_tool_id=tool.id,
        version_no=1,
        content_hash=hashlib.sha256(remote_name.encode()).hexdigest(),
        validation_status=ToolVersionValidationStatus.VALID,
        remote_description=description,
        input_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
        output_schema={"description": "forecast summary"},
    )
    tool.current_version_id = version.id
    await session.flush()
    return tool.id, version.id, int(tool.lock_version)


async def _seed_active_embedding_profile(session: AsyncSession, *, base_url: str) -> uuid.UUID:
    profile = await EmbeddingProfileRepository(session).create(
        code=f"emb-{uuid.uuid4().hex[:8]}",
        name="Active Emb E2E",
        provider="OPENAI_COMPATIBLE",
        model="emb-mock",
        base_url=base_url,
        dimension=_EMBED_DIM,
        distance_metric="cosine",
        is_active_for_tools=True,
    )
    await session.flush()
    return profile.id


async def _seed_agent_and_user(
    session: AsyncSession, *, tool_id: uuid.UUID
) -> tuple[uuid.UUID, uuid.UUID]:
    llm = await LLMProfileRepository(session).create(
        code=f"llm-{uuid.uuid4().hex[:8]}",
        name="Emb E2E LLM",
        provider="OPENAI_COMPATIBLE",
        model="gpt-test",
        base_url="https://llm.test/v1",
    )
    agent = await AgentRepository(session).create(
        code=f"agt-{uuid.uuid4().hex[:8]}",
        name="Emb E2E Agent",
    )
    version = await AgentVersionRepository(session).create(
        agent_id=agent.id,
        version_no=1,
        system_instruction="retrieve",
        llm_profile_id=llm.id,
        request_schema_version="1.0",
        plan_schema_version="1.0",
        selection_settings={"max_candidates": 5},
        planning_settings={},
        response_settings={},
        content_hash=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    await AgentToolGrantRepository(session).replace_all(
        version.id,
        [{"mcp_tool_id": tool_id, "effect": AgentToolGrantEffect.ALLOW.value}],
    )
    user = await UserService(session).create(
        UserCreate(
            username=f"u-{uuid.uuid4().hex[:8]}",
            display_name="Emb E2E User",
            email=f"u-{uuid.uuid4().hex[:8]}@example.com",
            status=UserStatus.ACTIVE,
        )
    )
    role = await RoleService(session).create(
        RoleCreate(code=f"r-{uuid.uuid4().hex[:8]}", name="Emb E2E Role")
    )
    execute = await PermissionRepository(session).get_by_code("mcp.tool.execute")
    assert execute is not None
    await RoleService(session).replace_permissions(
        role.id,
        RolePermissionReplaceRequest(permission_ids=[execute.id]),
        expected_lock_version=1,
    )
    await UserService(session).replace_roles(
        user.id,
        UserRoleReplaceRequest(role_ids=[role.id]),
        expected_lock_version=1,
    )
    await ResourceGrantService(session).create_for_user(
        user.id,
        ResourceGrantCreate(
            resource_type=ResourceGrantResourceType.MCP_TOOL,
            resource_id=tool_id,
        ),
    )
    await session.flush()
    return version.id, user.id


async def _wait_embedding_ready(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    tool_version_id: uuid.UUID,
    embedding_profile_id: uuid.UUID,
    timeout_s: float = 30.0,
) -> Any:
    deadline = time.time() + timeout_s
    last_status: str | None = None
    while time.time() < deadline:
        async with session_factory() as session:
            row = await ToolEmbeddingRepository(session).get(
                tool_version_id=tool_version_id,
                embedding_profile_id=embedding_profile_id,
            )
            if row is not None:
                last_status = row.status
                if row.status == ToolEmbeddingStatus.READY and row.embedding is not None:
                    return row
        await __import__("asyncio").sleep(0.05)
    raise AssertionError(
        f"ToolEmbedding did not reach READY via Celery worker; last_status={last_status}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_activate_outbox_celery_maintenance_mock_embed_ready_retrieval(
    integration_session_factory: async_sessionmaker[AsyncSession],
    isolated_redis_broker: str,
) -> None:
    mock = _MockEmbeddingServer()
    mock.start()
    remote_name = f"weather.get_forecast.{uuid.uuid4().hex[:6]}"
    description = "lookup Seoul weather forecast temperature humidity"
    try:
        async with integration_session_factory() as session:
            profile_id = await _seed_active_embedding_profile(session, base_url=mock.base_url)
            tool_id, version_id, lock = await _seed_discovered_valid_tool(
                session,
                remote_name=remote_name,
                description=description,
            )
            agent_version_id, user_id = await _seed_agent_and_user(session, tool_id=tool_id)
            await session.commit()

            # Precondition: no embedding row before activate/worker.
            pre = await ToolEmbeddingRepository(session).get(
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
            )
            assert pre is None

            activated = await MCPToolService(session).activate(tool_id, expected_lock_version=lock)
            await session.commit()
            assert activated.status == MCPToolStatus.ACTIVE

            outbox = (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.event_type == "TOOL_EMBEDDING_ENSURE",
                        OutboxEvent.aggregate_id == version_id,
                    )
                )
            ).scalar_one()
            outbox_event_id = outbox.id
            assert outbox.published_at is None
            assert outbox.payload == {
                "tool_version_id": str(version_id),
                "embedding_profile_id": str(profile_id),
            }
            validate_tool_embedding_ensure_event(outbox)

        publisher = CeleryExecutionQueuePublisher()
        with start_worker(
            celery_app,
            pool="solo",
            concurrency=1,
            queues=["maintenance"],
            loglevel="warning",
            perform_ping_check=False,
            shutdown_timeout=20,
        ):
            first_task_id = str(outbox_event_id)
            first_done = _wait_task_postrun(task_id=first_task_id)
            try:
                async with integration_session_factory() as session:
                    relay = await OutboxRelayService(session).publish_batch(
                        publisher=publisher,
                        limit=10,
                    )
                    await session.commit()
                    assert relay.published == 1
                    assert relay.failed == 0
                    row = await session.get(OutboxEvent, outbox_event_id)
                    assert row is not None
                    assert row.published_at is not None

                assert first_done.wait(timeout=30), (
                    f"Celery did not finish {_ENSURE_TASK_NAME} task_id={first_task_id}"
                )
            finally:
                first_done._mcpflow_disconnect()  # type: ignore[attr-defined]

            # READY must come from the worker + mock provider, not a test seed.
            ready = await _wait_embedding_ready(
                integration_session_factory,
                tool_version_id=version_id,
                embedding_profile_id=profile_id,
            )
            assert ready.status == ToolEmbeddingStatus.READY
            assert list(ready.embedding) == _FIXED_VECTOR
            assert mock.embed_call_count >= 1

        query = "Seoul weather forecast temperature"
        async with integration_session_factory() as session:
            # Real ModelProviderClient → mock HTTP (same profile base_url).
            result = await ToolRetrievalService(session).retrieve(
                user_id=user_id,
                agent_version_id=agent_version_id,
                query_text=query,
            )
            names = [c.descriptor.name for c in result.candidates]
            assert remote_name in names
            assert result.embedding_profile_id == profile_id
            assert mock.embed_call_count >= 2  # ensure + query
    finally:
        mock.stop()
