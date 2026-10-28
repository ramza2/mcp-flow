"""Mock-HTTP unit tests for pets sequential E2E ops helper (no ops network)."""

from __future__ import annotations

import importlib.util
import json
import stat
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "ops" / "pets-sequential-e2e" / "pets_sequential_e2e.py"


def _load_mod() -> Any:
    spec = importlib.util.spec_from_file_location("pets_sequential_e2e_ops", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def e2e() -> Any:
    return _load_mod()


def test_require_clean_discovery_accepts_clean(e2e: Any) -> None:
    e2e._require_clean_discovery(
        {
            "success": True,
            "factory_diff": {"status": "CLEAN", "matched": 2},
        },
        phase="preview",
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"success": False, "factory_diff": {"status": "CLEAN"}},
        {"success": True, "factory_diff": {"status": "MISMATCHED"}},
        {"success": True, "factory_diff": {"status": "AMBIGUOUS"}},
        {"success": True, "factory_diff": {}},
        {"success": True},
        {"success": True, "factory_diff": None},
        {"factory_diff": {"status": "CLEAN"}},
    ],
)
def test_require_clean_discovery_fail_closed(e2e: Any, payload: dict[str, Any]) -> None:
    with pytest.raises(e2e.ApiError):
        e2e._require_clean_discovery(payload, phase="apply")


def test_save_state_is_0600_atomic_and_scrubs_secrets(e2e: Any, tmp_path: Path) -> None:
    path = tmp_path / "pets-sequential-e2e.state.json"
    e2e._save_state(
        path,
        {
            "run_id": "pets-seq-e2e-v1",
            "job_id": "job-1",
            "password": "should-not-persist",
            "csrf_token": "csrf",
            "session": "cookie",
            "raw_session_token": "tok",
        },
    )
    assert path.is_file()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["job_id"] == "job-1"
    assert loaded["run_id"] == "pets-seq-e2e-v1"
    assert "password" not in loaded
    assert "csrf_token" not in loaded
    assert "session" not in loaded
    assert "raw_session_token" not in loaded
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_resume_state_keeps_job_id_without_recreate_gate(e2e: Any, tmp_path: Path) -> None:
    """Partial completion: persisted job_id is the resume gate for analyze."""
    path = tmp_path / "state.json"
    e2e._save_state(path, {"run_id": "pets-seq-e2e-v1", "job_id": "existing-job"})
    state = e2e._load_state(path)
    assert "job_id" in state
    assert state["job_id"] == "existing-job"
    # Mirrors cmd_setup: only create when job_id absent.
    assert not ("job_id" not in state)


class _FakeGrantClient:
    def __init__(
        self,
        pages: list[dict[str, Any]],
        *,
        api_error: type[Exception],
        post_status: int = 201,
    ) -> None:
        self.pages = pages
        self.post_status = post_status
        self.api_error = api_error
        self.gets: list[str] = []
        self.posts: list[dict[str, Any]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        expect: set[int] | None = None,
        **_kwargs: Any,
    ) -> Any:
        if method.upper() == "GET":
            self.gets.append(path)
            page = 1
            if "page=" in path:
                for part in path.split("?")[-1].split("&"):
                    if part.startswith("page=") and not part.startswith("page_size"):
                        page = int(part.split("=", 1)[1])
            idx = page - 1
            if idx < 0 or idx >= len(self.pages):
                return {"items": [], "has_next": False, "page": page}
            return self.pages[idx]
        if method.upper() == "POST":
            self.posts.append({"path": path, "json_body": json_body})
            if self.post_status == 409:
                raise self.api_error("conflict", status=409, body={"error": {}})
            if expect is not None and self.post_status not in expect:
                raise self.api_error("unexpected", status=self.post_status)
            return {"id": "grant-new", "_status": self.post_status}
        raise AssertionError(f"unexpected {method} {path}")


def test_ensure_resource_grant_paginates_and_skips_duplicate(e2e: Any) -> None:
    tool_id = "tool-on-page-2"
    pages = [
        {
            "items": [
                {"resource_type": "MCP_TOOL", "resource_id": "other"},
            ],
            "has_next": True,
            "page": 1,
        },
        {
            "items": [
                {"resource_type": "MCP_TOOL", "resource_id": tool_id},
            ],
            "has_next": False,
            "page": 2,
        },
    ]
    client = _FakeGrantClient(pages, api_error=e2e.ApiError)
    e2e._ensure_resource_grant(client, "user-1", tool_id)
    assert len(client.gets) == 2
    assert client.posts == []


def test_ensure_resource_grant_creates_when_missing(e2e: Any) -> None:
    client = _FakeGrantClient(
        [{"items": [], "has_next": False, "page": 1}],
        api_error=e2e.ApiError,
        post_status=201,
    )
    e2e._ensure_resource_grant(client, "user-1", "tool-new")
    assert len(client.posts) == 1
    assert client.posts[0]["json_body"]["resource_id"] == "tool-new"


def test_ensure_resource_grant_treats_409_as_present(e2e: Any) -> None:
    client = _FakeGrantClient(
        [{"items": [], "has_next": False, "page": 1}],
        api_error=e2e.ApiError,
        post_status=409,
    )
    e2e._ensure_resource_grant(client, "user-1", "tool-race")
    assert len(client.posts) == 1


def test_client_request_surfaces_http_error(e2e: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    client = e2e.Client("http://example.test")

    def _open(_req: Any, timeout: float = 0) -> Any:  # noqa: ARG001
        raise HTTPError(
            "http://example.test/api/v1/x",
            500,
            "err",
            hdrs=None,
            fp=BytesIO(b'{"error":{"code":"INTERNAL"}}'),
        )

    monkeypatch.setattr(client.opener, "open", _open)
    with pytest.raises(e2e.ApiError) as exc:
        client.request("GET", "/api/v1/x", expect={200})
    assert exc.value.status == 500


def test_discovery_mismatch_blocks_setup_gate(e2e: Any) -> None:
    """Simulate Factory Discovery MISMATCHED the same way setup gates do."""
    preview = {
        "success": True,
        "factory_diff": {
            "status": "MISMATCHED",
            "contract_changed": 1,
        },
    }
    with pytest.raises(e2e.ApiError) as exc:
        e2e._require_clean_discovery(preview, phase="preview")
    assert "CLEAN" in str(exc.value)


class _FakeProbeClient:
    """Minimal Client stand-in for embedding probe unit tests."""

    def __init__(
        self,
        *,
        api_error: type[Exception],
        analyze_status: int = 200,
        analyze_body: dict[str, Any] | None = None,
        select_status: int = 200,
        select_bodies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.api_error = api_error
        self.analyze_status = analyze_status
        self.analyze_body = analyze_body or {"status": "RETRIEVING"}
        self.select_status = select_status
        self.select_bodies = list(select_bodies or [])
        self.select_calls = 0
        self.created = 0

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,  # noqa: ARG002
        expect: set[int] | None = None,
        **_kwargs: Any,
    ) -> Any:
        if method.upper() == "POST" and path == "/api/v1/agent-requests":
            self.created += 1
            if expect is not None and 201 not in expect:
                raise self.api_error("create unexpected", status=201)
            return {"id": f"req-{self.created}"}
        if method.upper() == "POST" and path.endswith("/analyze"):
            if expect is not None and self.analyze_status not in expect:
                raise self.api_error(
                    f"analyze HTTP {self.analyze_status}",
                    status=self.analyze_status,
                    body={"error": {"code": "UPSTREAM"}},
                )
            if self.analyze_status != 200:
                return {"_status": self.analyze_status, "_body": self.analyze_body}
            return dict(self.analyze_body)
        if method.upper() == "POST" and path.endswith("/select-tool"):
            self.select_calls += 1
            if expect is not None and self.select_status not in expect:
                raise self.api_error(
                    f"select HTTP {self.select_status}",
                    status=self.select_status,
                    body={"error": {"code": "CONFLICT"}},
                )
            if self.select_status != 200:
                return {"_status": self.select_status, "_body": {}}
            idx = min(self.select_calls - 1, len(self.select_bodies) - 1)
            return dict(self.select_bodies[idx])
        raise AssertionError(f"unexpected {method} {path}")


def test_embedding_probe_aborts_on_analyze_502(e2e: Any) -> None:
    client = _FakeProbeClient(api_error=e2e.ApiError, analyze_status=502)
    with pytest.raises(e2e.ApiError) as exc:
        e2e._wait_embeddings_via_select(
            client,
            agent_id="agent-1",
            list_version_id="list-v",
            detail_version_id="detail-v",
            timeout_s=30.0,
        )
    assert exc.value.status == 502
    assert client.created == 1
    assert client.select_calls == 0


def test_embedding_probe_aborts_on_select_409(e2e: Any) -> None:
    client = _FakeProbeClient(
        api_error=e2e.ApiError,
        analyze_status=200,
        select_status=409,
        select_bodies=[{"selection": {"decision": "NO_MATCH"}}],
    )
    with pytest.raises(e2e.ApiError) as exc:
        e2e._wait_embeddings_via_select(
            client,
            agent_id="agent-1",
            list_version_id="list-v",
            detail_version_id="detail-v",
            timeout_s=30.0,
        )
    assert exc.value.status == 409


@pytest.mark.parametrize(
    "status",
    ["WAITING_INPUT", "WAITING_CONFIRMATION", "FAILED", "REJECTED", "CANCELLED"],
)
def test_embedding_probe_aborts_non_retrieving_analyze(e2e: Any, status: str) -> None:
    client = _FakeProbeClient(
        api_error=e2e.ApiError,
        analyze_body={"status": status},
    )
    with pytest.raises(e2e.ApiError) as exc:
        e2e._wait_embeddings_via_select(
            client,
            agent_id="agent-1",
            list_version_id="list-v",
            detail_version_id="detail-v",
            timeout_s=30.0,
        )
    assert status in str(exc.value)
    assert "not treated as embedding-not-ready" in str(exc.value)
    assert client.select_calls == 0


def test_embedding_probe_retries_no_match_then_auto_select(
    e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(e2e.time, "sleep", lambda s: sleeps.append(s))
    list_v = "list-version"
    client = _FakeProbeClient(
        api_error=e2e.ApiError,
        select_bodies=[
            {"selection": {"decision": "NO_MATCH", "selected_tool_version_id": None}},
            {
                "selection": {
                    "decision": "AUTO_SELECT",
                    "selected_tool_version_id": list_v,
                    "selected_tool_name": "GET /pets",
                }
            },
        ],
    )
    result = e2e._wait_embeddings_via_select(
        client,
        agent_id="agent-1",
        list_version_id=list_v,
        detail_version_id="detail-v",
        timeout_s=60.0,
    )
    assert result["probe"] == "ready_list_primary"
    assert result["no_match_retries"] == 1
    assert result["request_id"] == "req-2"
    assert sleeps == [e2e.EMBEDDING_PROBE_SLEEP_S]
    assert client.select_calls == 2


def test_embedding_probe_no_match_timeout_records_retries(
    e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(e2e.time, "sleep", lambda _s: None)
    # Force a single loop iteration then deadline expiry.
    times = iter([100.0, 100.1, 999.0])
    monkeypatch.setattr(e2e.time, "time", lambda: next(times, 999.0))
    client = _FakeProbeClient(
        api_error=e2e.ApiError,
        select_bodies=[
            {
                "selection": {
                    "decision": "NO_MATCH",
                    "reason_summary": "no candidates",
                }
            }
        ],
    )
    with pytest.raises(e2e.ApiError) as exc:
        e2e._wait_embeddings_via_select(
            client,
            agent_id="agent-1",
            list_version_id="list-v",
            detail_version_id="detail-v",
            timeout_s=1.0,
        )
    msg = str(exc.value)
    assert "NO_MATCH retries" in msg
    assert "last_request_id=" in msg
    assert "last_selection=" in msg


def test_embedding_probe_aborts_unexpected_select_decision(
    e2e: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(e2e.time, "sleep", lambda _s: None)
    client = _FakeProbeClient(
        api_error=e2e.ApiError,
        select_bodies=[
            {
                "selection": {
                    "decision": "NEED_CONFIRMATION",
                    "selected_tool_version_id": "x",
                }
            }
        ],
    )
    with pytest.raises(e2e.ApiError) as exc:
        e2e._wait_embeddings_via_select(
            client,
            agent_id="agent-1",
            list_version_id="list-v",
            detail_version_id="detail-v",
            timeout_s=30.0,
        )
    assert "NEED_CONFIRMATION" in str(exc.value)
    assert "not retried as embedding-not-ready" in str(exc.value)


def _ok_evidence_payload() -> dict[str, Any]:
    return {
        "execution_id": "cddf58fb-a650-4d6c-8ac4-09de794ecf59",
        "status": "SUCCEEDED",
        "steps": [
            {
                "step_key": "tool_1",
                "status": "SUCCEEDED",
                "sequence_hint": 1,
                "resolved_input": {},
                "result_inline": {
                    "structured_content": {
                        "ok": True,
                        "http_status": 200,
                        "body": {
                            "pets": [
                                {"id": 1, "name": "Alpha", "status": "available"},
                                {"id": 2, "name": "Beta", "status": "pending"},
                            ]
                        },
                    }
                },
            },
            {
                "step_key": "tool_2",
                "status": "SUCCEEDED",
                "sequence_hint": 2,
                "resolved_input": {"pet_id": 1},
                "result_inline": {
                    "structured_content": {
                        "ok": True,
                        "http_status": 200,
                        "body": {
                            "id": 1,
                            "name": "Alpha",
                            "status": "available",
                        },
                    }
                },
            },
        ],
    }


def test_evaluate_step_output_evidence_pass(e2e: Any) -> None:
    ok, message = e2e.evaluate_step_output_evidence(_ok_evidence_payload())
    assert ok is True
    assert message.startswith("PASS:")
    assert "pet_id=1" in message
    assert "Alpha" in message


@pytest.mark.parametrize("bad_status", ["FAILED", "CANCELLED", "PARTIALLY_SUCCEEDED"])
def test_evaluate_step_output_evidence_requires_execution_succeeded(
    e2e: Any, bad_status: str
) -> None:
    payload = _ok_evidence_payload()
    payload["status"] = bad_status
    # Steps remain SUCCEEDED — execution status alone must fail closed.
    ok, message = e2e.evaluate_step_output_evidence(payload)
    assert ok is False
    assert message.startswith("FAIL:")
    assert "execution.status must be SUCCEEDED" in message
    assert f"execution.status={bad_status}" in message
    assert "Alpha" not in message
    assert "99" not in message


def test_evaluate_step_output_evidence_fail_wrong_pet_id(e2e: Any) -> None:
    payload = _ok_evidence_payload()
    payload["steps"][1]["resolved_input"] = {"pet_id": 99}
    ok, message = e2e.evaluate_step_output_evidence(payload)
    assert ok is False
    assert message.startswith("FAIL:")
    assert "resolved_input.pet_id" in message
    assert "99" not in message


def test_evaluate_step_output_evidence_fail_step_status(e2e: Any) -> None:
    payload = _ok_evidence_payload()
    payload["steps"][0]["status"] = "FAILED"
    ok, message = e2e.evaluate_step_output_evidence(payload)
    assert ok is False
    assert "tool_1.status=FAILED" in message
    assert "SUCCEEDED" in message


def test_evaluate_step_output_evidence_fail_list_first_id(e2e: Any) -> None:
    payload = _ok_evidence_payload()
    payload["steps"][0]["result_inline"]["structured_content"]["body"]["pets"][0]["id"] = 42
    ok, message = e2e.evaluate_step_output_evidence(payload)
    assert ok is False
    assert "pets[0].id" in message
    assert "42" not in message
    assert "got " not in message


def test_evaluate_step_output_evidence_fail_hides_detail_name(e2e: Any) -> None:
    payload = _ok_evidence_payload()
    payload["steps"][1]["result_inline"]["structured_content"]["body"]["name"] = "SecretPetName"
    ok, message = e2e.evaluate_step_output_evidence(payload)
    assert ok is False
    assert message.startswith("FAIL:")
    assert "name=Alpha" in message  # expected condition only
    assert "SecretPetName" not in message
    assert "got id=" not in message
    assert "got name=" not in message


def test_fetch_execution_evidence_uses_readonly_sql_and_runner(e2e: Any) -> None:
    captured: dict[str, Any] = {}
    payload = _ok_evidence_payload()

    class _Completed:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    def _runner(argv: list[str], **kwargs: Any) -> _Completed:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Completed()

    got = e2e.fetch_execution_evidence(
        "cddf58fb-a650-4d6c-8ac4-09de794ecf59",
        runner=_runner,
    )
    assert got["execution_id"] == payload["execution_id"]
    sql = captured["argv"][-1]
    assert "SET TRANSACTION READ ONLY" in sql
    assert "cddf58fb-a650-4d6c-8ac4-09de794ecf59" in sql
    assert "FROM executions e" in sql
    assert "WHERE e.id =" in sql
    assert captured["argv"][:4] == [
        "docker",
        "compose",
        "--env-file",
        ".env.server",
    ]
    assert captured["argv"][4:8] == [
        "-f",
        "compose.yaml",
        "-f",
        "compose.server.yaml",
    ]


def test_evidence_docker_argv_matches_ops_compose(e2e: Any) -> None:
    argv = e2e._evidence_docker_argv("SELECT 1;")
    assert argv[:8] == [
        "docker",
        "compose",
        "--env-file",
        ".env.server",
        "-f",
        "compose.yaml",
        "-f",
        "compose.server.yaml",
    ]
    assert "exec" in argv
    assert "postgres" in argv
    assert argv[-1] == "SELECT 1;"


def test_fetch_execution_evidence_parse_error_hides_raw(
    e2e: Any,
) -> None:
    class _Completed:
        returncode = 0
        stdout = '{"not": "valid json"'
        stderr = "SecretPetName leaked"

    def _runner(_argv: list[str], **_kwargs: Any) -> _Completed:
        return _Completed()

    with pytest.raises(e2e.ApiError) as exc:
        e2e.fetch_execution_evidence(
            "cddf58fb-a650-4d6c-8ac4-09de794ecf59",
            runner=_runner,
        )
    msg = str(exc.value)
    assert "parse failed" in msg
    assert "SecretPetName" not in msg
    assert '{"not"' not in msg
    assert "cddf58fb-a650-4d6c-8ac4-09de794ecf59" in msg


def test_fetch_execution_evidence_rc_failure_hides_stderr(e2e: Any) -> None:
    class _Completed:
        returncode = 2
        stdout = ""
        stderr = "DETAIL: name=Alpha secret=xyz"

    def _runner(_argv: list[str], **_kwargs: Any) -> _Completed:
        return _Completed()

    with pytest.raises(e2e.ApiError) as exc:
        e2e.fetch_execution_evidence(
            "cddf58fb-a650-4d6c-8ac4-09de794ecf59",
            runner=_runner,
        )
    msg = str(exc.value)
    assert "rc=2" in msg
    assert "secret=xyz" not in msg
    assert "Alpha" not in msg


def test_cmd_evidence_pass_with_state(
    e2e: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    eid = "cddf58fb-a650-4d6c-8ac4-09de794ecf59"
    e2e._save_state(state_path, {"run_id": "pets-seq-e2e-v1", "last_execution_id": eid})
    monkeypatch.setenv("MCPFLOW_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        e2e,
        "fetch_execution_evidence",
        lambda _eid, runner=None: _ok_evidence_payload(),
    )
    args = e2e.build_parser().parse_args(["--state-path", str(state_path), "evidence"])
    assert e2e.cmd_evidence(args) == 0


def test_cmd_evidence_fail_returns_one(
    e2e: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_path = tmp_path / "state.json"
    eid = "cddf58fb-a650-4d6c-8ac4-09de794ecf59"
    e2e._save_state(state_path, {"last_execution_id": eid})
    bad = _ok_evidence_payload()
    bad["steps"][1]["resolved_input"] = {"pet_id": 2}
    monkeypatch.setattr(e2e, "fetch_execution_evidence", lambda _eid, runner=None: bad)
    args = e2e.build_parser().parse_args(["--state-path", str(state_path), "evidence"])
    assert e2e.cmd_evidence(args) == 1
