"""Outbox → Celery/Redis → worker → Mock MCP → SUCCEEDED (docs/08, docs/09).

Starts a **dedicated** loopback ``redis-server`` for this suite only. Never
``FLUSHDB`` / mutates a pre-existing Redis. Does not use ops Redis/containers.
Does not pass via manual ``ExecutionClaimService.claim`` or direct
``McpToolRunner`` — Celery ``mcpflow.execution.claim`` owns claim + MCP call.
"""

from __future__ import annotations

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
from app.domain.enums import ExecutionStatus, StepStatus, ToolCallNormalizedStatus
from app.execution.queue import ExecutionQueueService, OutboxRelayService
from app.infrastructure.celery_app import celery_app
from app.infrastructure.queue import CeleryExecutionQueuePublisher
from app.models.outbox import OutboxEvent
from app.repositories.execution import ExecutionRepository
from app.repositories.mcp_server import MCPServerRepository
from app.repositories.mcp_tool import MCPToolRepository
from celery.contrib.testing.worker import start_worker
from celery.signals import task_postrun
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.test_execution_creation import _create, _idem_key, _seed_ready

_REDIS_DIR = Path("/tmp/mcpflow-test-redis-broker-e2e")
_CLAIM_TASK_NAME = "mcpflow.execution.claim"


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
    # Unique dir/pid per invocation so parallel workers do not collide.
    run_dir = _REDIS_DIR / uuid.uuid4().hex
    run_dir.mkdir(parents=True, exist_ok=True)
    pid_path = run_dir / "redis.pid"
    port = _free_loopback_port()
    host = "127.0.0.1"
    # Dedicated instance — empty DB by construction; never FLUSHDB.
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


class _MockMCPServer:
    """Minimal Current MCP tools/call HTTP server for worker CurrentMCPClient."""

    def __init__(self) -> None:
        self.tools_call_count = 0
        self._lock = threading.Lock()
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:  # noqa: D401
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                method = body.get("method")
                with parent._lock:
                    if method == "tools/call":
                        parent.tools_call_count += 1
                payload = {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {
                        "content": [
                            {"type": "text", "text": "broker-celery-mcp-ok"}
                        ],
                        "structuredContent": {
                            "source": "mock-mcp",
                            "condition": "broker-celery-mcp-ok",
                        },
                        "isError": False,
                    },
                }
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = int(self._httpd.server_address[1])
        self.endpoint_url = f"http://127.0.0.1:{self.port}/mcp"
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._thread.join(timeout=5)


def _wait_task_postrun(*, task_id: str) -> threading.Event:
    """Return an Event that is set when Celery finishes the given task_id."""
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
    # Stash disconnect callback on the event for the caller.
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
    # Refuse non-loopback URLs even if somehow constructed incorrectly.
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
        # Restore process-wide env / settings / Celery broker bindings.
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        celery_app.conf.broker_url = previous_broker_url
        celery_app.conf.result_backend = previous_result_backend
        # Drop pooled connections that may still point at the dedicated broker.
        try:
            pool = getattr(celery_app, "pool", None)
            if pool is not None:
                pool.force_close_all()
        except Exception:
            pass
        get_settings.cache_clear()


async def _point_execution_at_mock_mcp(
    session: AsyncSession,
    *,
    execution_id: uuid.UUID,
    endpoint_url: str,
) -> str:
    steps = await ExecutionRepository(session).list_steps(execution_id)
    assert len(steps) == 1
    tool_version = await MCPToolRepository(session).get_version(
        steps[0].mcp_tool_version_id
    )
    assert tool_version is not None
    tool = await MCPToolRepository(session).get(tool_version.mcp_tool_id)
    assert tool is not None
    server = await MCPServerRepository(session).get(tool.mcp_server_id)
    assert server is not None
    server.endpoint_url = endpoint_url
    server.auth_type = "NONE"
    await session.commit()
    return tool.remote_name


async def _wait_execution_status(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    execution_id: uuid.UUID,
    expected: str,
    timeout_s: float = 30.0,
) -> Any:
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        async with session_factory() as session:
            execution = await ExecutionRepository(session).get(execution_id)
            assert execution is not None
            last = execution.status
            if execution.status == expected:
                return execution
        await __import__("asyncio").sleep(0.05)
    raise AssertionError(
        f"Execution {execution_id} did not reach {expected}; last={last}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_pg_outbox_celery_redis_worker_mock_mcp_succeeded_and_duplicate_safe(
    integration_session_factory: async_sessionmaker[AsyncSession],
    isolated_redis_broker: str,
) -> None:
    mock = _MockMCPServer()
    mock.start()
    try:
        async with integration_session_factory() as session:
            seeded = await _seed_ready(session)
            created = await _create(session, seeded, idempotency_key=_idem_key())
            execution_id = created.result.id
            await _point_execution_at_mock_mcp(
                session,
                execution_id=execution_id,
                endpoint_url=mock.endpoint_url,
            )

        async with integration_session_factory() as session:
            staged = await ExecutionQueueService(session).stage_created_batch(
                limit=10
            )
            await session.commit()
            assert staged == 1
            event = (
                await session.execute(
                    select(OutboxEvent).where(
                        OutboxEvent.aggregate_id == execution_id
                    )
                )
            ).scalar_one()
            outbox_event_id = event.id
            assert event.published_at is None
            assert event.payload == {"execution_id": str(execution_id)}

        publisher = CeleryExecutionQueuePublisher()
        with start_worker(
            celery_app,
            pool="solo",
            concurrency=1,
            loglevel="warning",
            perform_ping_check=False,
            shutdown_timeout=20,
        ):
            # Real Outbox → Celery/Redis publish (not FakePublisher).
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
                    f"Celery did not finish {_CLAIM_TASK_NAME} task_id={first_task_id}"
                )
            finally:
                first_done._mcpflow_disconnect()  # type: ignore[attr-defined]

            execution = await _wait_execution_status(
                integration_session_factory,
                execution_id=execution_id,
                expected=ExecutionStatus.SUCCEEDED.value,
            )
            # Terminal success clears the worker lease (worker_id/lease_token → null).
            assert execution.worker_id is None
            assert execution.lease_token is None
            assert execution.started_at is not None
            assert mock.tools_call_count == 1

            async with integration_session_factory() as session:
                step = (
                    await ExecutionRepository(session).list_steps(execution_id)
                )[0]
                assert step.status == StepStatus.SUCCEEDED.value
                attempts = await ExecutionRepository(session).list_attempts(step.id)
                assert len(attempts) == 1
                tool_calls = await ExecutionRepository(session).list_tool_calls(
                    attempts[0].id
                )
                assert len(tool_calls) == 1
                assert tool_calls[0].normalized_status == (
                    ToolCallNormalizedStatus.SUCCEEDED.value
                )
                assert "broker-celery-mcp-ok" in str(step.result_inline)

            # At-least-once redelivery: same IDs, new task_id. Wait for actual
            # Celery task_postrun before asserting MCP was not called again.
            duplicate_task_id = str(uuid.uuid4())
            dup_done = _wait_task_postrun(task_id=duplicate_task_id)
            try:
                celery_app.send_task(
                    _CLAIM_TASK_NAME,
                    kwargs={
                        "execution_id": str(execution_id),
                        "outbox_event_id": str(outbox_event_id),
                    },
                    queue="execution",
                    task_id=duplicate_task_id,
                )
                assert dup_done.wait(timeout=30), (
                    "Celery did not finish duplicate "
                    f"{_CLAIM_TASK_NAME} task_id={duplicate_task_id}"
                )
            finally:
                dup_done._mcpflow_disconnect()  # type: ignore[attr-defined]

            assert mock.tools_call_count == 1

            async with integration_session_factory() as session:
                execution = await ExecutionRepository(session).get(execution_id)
                assert execution is not None
                assert execution.status == ExecutionStatus.SUCCEEDED.value
                step = (
                    await ExecutionRepository(session).list_steps(execution_id)
                )[0]
                attempts = await ExecutionRepository(session).list_attempts(step.id)
                tool_calls = await ExecutionRepository(session).list_tool_calls(
                    attempts[0].id
                )
                assert len(attempts) == 1
                assert len(tool_calls) == 1
    finally:
        mock.stop()
