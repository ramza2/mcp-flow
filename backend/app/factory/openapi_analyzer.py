"""Pure OpenAPI 3.0/3.1 source analyzer for Tool Factory (REQ-FAC-001..004 foundation).

No network, filesystem, subprocess, import/exec of user source, or persistence.
Analysis success ≠ generated Tool ≠ verified Tool ≠ activated Tool.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

import yaml

from app.factory.contracts import (
    FactoryAnalysisIssue,
    FactoryOpenAPIAnalysis,
    FactoryOperationCandidate,
    FactoryServerCandidate,
    FactorySourceFormat,
)
from app.factory.errors import (
    FACTORY_ANALYSIS_LIMIT_EXCEEDED,
    FACTORY_EXTERNAL_REF_REJECTED,
    FACTORY_OPENAPI_STRUCTURE_INVALID,
    FACTORY_OPENAPI_VERSION_UNSUPPORTED,
    FACTORY_SERVER_URL_INVALID,
    FACTORY_SOURCE_PARSE_ERROR,
    FACTORY_SOURCE_TOO_LARGE,
    FactoryAnalysisError,
)

# --- Hard bounds (documented in docs/09) ------------------------------------

MAX_SOURCE_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_PATHS = 500
MAX_OPERATIONS = 2000
MAX_SERVERS = 50
MAX_TAGS_PER_OPERATION = 32
MAX_STRING_LEN = 2000
MAX_REF_DEPTH = 32
MAX_SCHEMA_NODES = 5_000

_HTTP_METHODS = (
    "get",
    "put",
    "post",
    "delete",
    "options",
    "head",
    "patch",
    "trace",
)
_SUPPORTED_OPENAPI = re.compile(r"^3\.(0|1)(\.\d+)?$")
_STRIP_SCHEMA_KEYS = frozenset(
    {
        "example",
        "examples",
        "default",
        "xml",
        "externalDocs",
    }
)


def analyze_openapi(
    source: bytes,
    *,
    filename: str | None = None,
) -> FactoryOpenAPIAnalysis:
    """Analyze OpenAPI 3.0/3.1 document bytes into a safe bounded result."""

    if not isinstance(source, (bytes, bytearray)):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "Factory source must be raw bytes.",
        )
    raw = bytes(source)
    if len(raw) > MAX_SOURCE_BYTES:
        raise FactoryAnalysisError(
            FACTORY_SOURCE_TOO_LARGE,
            f"Factory source exceeds maximum size of {MAX_SOURCE_BYTES} bytes.",
        )

    source_sha256 = hashlib.sha256(raw).hexdigest()
    document, source_format = _parse_document(raw, filename=filename)

    if not isinstance(document, dict):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI document root must be an object.",
        )

    openapi_version = _require_supported_version(document)
    title, api_version = _parse_info(document)
    servers = _parse_servers(document.get("servers"))
    operations, issues = _parse_operations(document)

    return FactoryOpenAPIAnalysis(
        source_sha256=source_sha256,
        source_format=source_format,
        openapi_version=openapi_version,
        title=title,
        version=api_version,
        servers=servers,
        operations=operations,
        issues=issues,
    )


def _parse_document(
    raw: bytes,
    *,
    filename: str | None,
) -> tuple[Any, FactorySourceFormat]:
    text = _decode_text(raw)
    prefer_json = _prefer_json(text, filename)

    if prefer_json:
        try:
            return json.loads(text), "JSON"
        except json.JSONDecodeError:
            # Explicit .json filenames must not fall through to YAML.
            if filename and filename.lower().endswith(".json"):
                raise FactoryAnalysisError(
                    FACTORY_SOURCE_PARSE_ERROR,
                    "Factory source is not valid JSON.",
                ) from None
        except RecursionError as exc:
            raise FactoryAnalysisError(
                FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                "Factory source JSON nesting exceeds analyzer limits.",
            ) from exc

    try:
        # yaml.safe_load only — never FullLoader / unsafe loaders.
        loaded = yaml.safe_load(text)
    except yaml.YAMLError:
        if prefer_json:
            raise FactoryAnalysisError(
                FACTORY_SOURCE_PARSE_ERROR,
                "Factory source is not valid JSON or YAML.",
            ) from None
        raise FactoryAnalysisError(
            FACTORY_SOURCE_PARSE_ERROR,
            "Factory source is not valid YAML.",
        ) from None
    except RecursionError as exc:
        raise FactoryAnalysisError(
            FACTORY_ANALYSIS_LIMIT_EXCEEDED,
            "Factory source YAML nesting exceeds analyzer limits.",
        ) from exc

    return loaded, "YAML"


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FactoryAnalysisError(
            FACTORY_SOURCE_PARSE_ERROR,
            "Factory source must be valid UTF-8 text.",
        ) from exc


def _prefer_json(text: str, filename: str | None) -> bool:
    if filename:
        lower = filename.lower()
        if lower.endswith(".json"):
            return True
        if lower.endswith((".yaml", ".yml")):
            return False
    return _looks_like_json(text)


def _looks_like_json(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _require_supported_version(document: dict[str, Any]) -> str:
    version = document.get("openapi")
    if version is None and "swagger" in document:
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_VERSION_UNSUPPORTED,
            "Swagger 2.0 is not supported; OpenAPI 3.0.x or 3.1.x is required.",
        )
    if not isinstance(version, str) or not version.strip():
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI document requires a string openapi version field.",
        )
    normalized = version.strip()
    if not _SUPPORTED_OPENAPI.match(normalized):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_VERSION_UNSUPPORTED,
            "Unsupported OpenAPI version; only 3.0.x and 3.1.x are accepted.",
        )
    return normalized


def _parse_info(document: dict[str, Any]) -> tuple[str | None, str | None]:
    info = document.get("info")
    if info is None:
        return None, None
    if not isinstance(info, dict):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI info must be an object when present.",
        )
    title = _bound_str(info.get("title"))
    version = _bound_str(info.get("version"), max_len=128)
    return title, version


def _parse_servers(raw_servers: Any) -> list[FactoryServerCandidate]:
    if raw_servers is None:
        return []
    if not isinstance(raw_servers, list):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI servers must be a list when present.",
        )
    if len(raw_servers) > MAX_SERVERS:
        raise FactoryAnalysisError(
            FACTORY_ANALYSIS_LIMIT_EXCEEDED,
            f"OpenAPI servers exceed maximum of {MAX_SERVERS}.",
        )

    servers: list[FactoryServerCandidate] = []
    for entry in raw_servers:
        if not isinstance(entry, dict):
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI server entries must be objects.",
            )
        url = entry.get("url")
        if not isinstance(url, str) or not url.strip():
            raise FactoryAnalysisError(
                FACTORY_SERVER_URL_INVALID,
                "OpenAPI server url must be a non-empty string.",
            )
        validated = _validate_server_url(url.strip())
        # Template variables object — fail closed (no deterministic expansion).
        if "variables" in entry and entry["variables"] is not None:
            raise FactoryAnalysisError(
                FACTORY_SERVER_URL_INVALID,
                "OpenAPI server URL templates with variables are not supported.",
            )
        servers.append(
            FactoryServerCandidate(
                url=validated,
                description=_bound_str(entry.get("description")),
            )
        )
    return servers


def _validate_server_url(url: str) -> str:
    if len(url) > 2048:
        raise FactoryAnalysisError(
            FACTORY_SERVER_URL_INVALID,
            "OpenAPI server url exceeds maximum length.",
        )
    # Fail closed on OpenAPI templated server URLs (e.g. https://{host}/v1).
    if "{" in url or "}" in url:
        raise FactoryAnalysisError(
            FACTORY_SERVER_URL_INVALID,
            "OpenAPI server URL templates with variables are not supported.",
        )
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise FactoryAnalysisError(
            FACTORY_SERVER_URL_INVALID,
            "OpenAPI server url must use http or https.",
        )
    if parsed.username is not None or parsed.password is not None:
        raise FactoryAnalysisError(
            FACTORY_SERVER_URL_INVALID,
            "OpenAPI server url must not include userinfo.",
        )
    if not parsed.hostname:
        raise FactoryAnalysisError(
            FACTORY_SERVER_URL_INVALID,
            "OpenAPI server url must include a valid hostname.",
        )
    return url


def _parse_operations(
    document: dict[str, Any],
) -> tuple[list[FactoryOperationCandidate], list[FactoryAnalysisIssue]]:
    paths = document.get("paths")
    if paths is None:
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI document requires a paths object.",
        )
    if not isinstance(paths, dict):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI paths must be an object.",
        )
    if len(paths) > MAX_PATHS:
        raise FactoryAnalysisError(
            FACTORY_ANALYSIS_LIMIT_EXCEEDED,
            f"OpenAPI paths exceed maximum of {MAX_PATHS}.",
        )

    operations: list[FactoryOperationCandidate] = []
    issues: list[FactoryAnalysisIssue] = []
    resolver = _RefResolver(document)

    # Deterministic path order.
    for path in sorted(paths.keys(), key=lambda p: str(p)):
        if not isinstance(path, str) or not path.startswith("/"):
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI path keys must be strings that start with '/'.",
            )
        item = paths[path]
        if item is None:
            continue
        if not isinstance(item, dict):
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI path items must be objects.",
            )

        # Path-level parameters are ignored for request schema in this slice
        # except when merged via operation.parameters (handled below).
        for method in _HTTP_METHODS:
            if method not in item:
                continue
            op = item[method]
            if op is None:
                continue
            if not isinstance(op, dict):
                raise FactoryAnalysisError(
                    FACTORY_OPENAPI_STRUCTURE_INVALID,
                    "OpenAPI operation values must be objects.",
                )
            if len(operations) >= MAX_OPERATIONS:
                raise FactoryAnalysisError(
                    FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                    f"OpenAPI operations exceed maximum of {MAX_OPERATIONS}.",
                )

            method_upper = method.upper()
            operation_key = f"{method_upper} {path}"
            operation_id = _bound_str(op.get("operationId"), max_len=256)
            tags = _parse_tags(op.get("tags"))

            request_schema: dict[str, Any] | None = None
            response_schema: dict[str, Any] | None = None
            try:
                request_schema = _extract_request_schema(op, resolver)
                response_schema = _extract_response_schema(op, resolver)
            except FactoryAnalysisError as exc:
                if exc.code in {
                    FACTORY_EXTERNAL_REF_REJECTED,
                    FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                    FACTORY_OPENAPI_STRUCTURE_INVALID,
                }:
                    raise
                issues.append(
                    FactoryAnalysisIssue(
                        code=exc.code,
                        message=exc.message,
                        location=operation_key,
                        severity="ERROR",
                    )
                )

            operations.append(
                FactoryOperationCandidate(
                    operation_key=operation_key,
                    method=method_upper,
                    path=path,
                    operation_id=operation_id,
                    summary=_bound_str(op.get("summary"), max_len=512),
                    description=_bound_str(op.get("description")),
                    tags=tags,
                    deprecated=bool(op.get("deprecated", False)),
                    request_schema=request_schema,
                    response_schema=response_schema,
                )
            )

    # Stable order: path then method (already produced by sorted paths + method order).
    operations.sort(key=lambda o: (o.path, o.method))
    return operations, issues


def _parse_tags(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FactoryAnalysisError(
            FACTORY_OPENAPI_STRUCTURE_INVALID,
            "OpenAPI operation tags must be a list when present.",
        )
    if len(raw) > MAX_TAGS_PER_OPERATION:
        raise FactoryAnalysisError(
            FACTORY_ANALYSIS_LIMIT_EXCEEDED,
            f"OpenAPI operation tags exceed maximum of {MAX_TAGS_PER_OPERATION}.",
        )
    tags: list[str] = []
    for item in raw:
        bound = _bound_str(item, max_len=128)
        if bound:
            tags.append(bound)
    return tags


def _extract_request_schema(
    operation: dict[str, Any],
    resolver: _RefResolver,
) -> dict[str, Any] | None:
    body = operation.get("requestBody")
    if body is None:
        return None
    body_obj = resolver.resolve_node(body, context="requestBody")
    if not isinstance(body_obj, dict):
        return None
    content = body_obj.get("content")
    if not isinstance(content, dict) or not content:
        return None
    # Prefer application/json, else first media type in sorted order.
    media_key = (
        "application/json"
        if "application/json" in content
        else sorted(str(k) for k in content.keys())[0]
    )
    media = content.get(media_key)
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    if schema is None:
        return None
    return resolver.normalize_schema(schema)


def _extract_response_schema(
    operation: dict[str, Any],
    resolver: _RefResolver,
) -> dict[str, Any] | None:
    responses = operation.get("responses")
    if not isinstance(responses, dict) or not responses:
        return None

    # Prefer 200, then 201, then default, then first 2xx in sorted order.
    preferred_keys = ["200", "201", "default"]
    selected_key: str | None = None
    for key in preferred_keys:
        if key in responses:
            selected_key = key
            break
    if selected_key is None:
        two_xx = sorted(
            str(k)
            for k in responses.keys()
            if isinstance(k, str) and len(k) == 3 and k.startswith("2")
        )
        if two_xx:
            selected_key = two_xx[0]
        else:
            return None

    response = responses.get(selected_key)
    response_obj = resolver.resolve_node(response, context="response")
    if not isinstance(response_obj, dict):
        return None
    content = response_obj.get("content")
    if not isinstance(content, dict) or not content:
        return None
    media_key = (
        "application/json"
        if "application/json" in content
        else sorted(str(k) for k in content.keys())[0]
    )
    media = content.get(media_key)
    if not isinstance(media, dict):
        return None
    schema = media.get("schema")
    if schema is None:
        return None
    return resolver.normalize_schema(schema)


class _RefResolver:
    """Internal `#/...` ref resolver with hard depth/node bounds."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document
        self._nodes_seen = 0

    def resolve_node(self, node: Any, *, context: str) -> Any:
        return self._resolve(node, stack=(), context=context)

    def normalize_schema(self, schema: Any) -> dict[str, Any]:
        resolved = self._resolve(schema, stack=(), context="schema")
        normalized = self._sanitize(resolved, depth=0)
        if not isinstance(normalized, dict):
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI schema must resolve to an object.",
            )
        return normalized

    def _resolve(self, node: Any, *, stack: tuple[str, ...], context: str) -> Any:
        self._nodes_seen += 1
        if self._nodes_seen > MAX_SCHEMA_NODES:
            raise FactoryAnalysisError(
                FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                f"OpenAPI schema traversal exceeds maximum of {MAX_SCHEMA_NODES} nodes.",
            )
        if not isinstance(node, dict):
            return node
        if "$ref" not in node:
            return node

        ref = node.get("$ref")
        if not isinstance(ref, str) or not ref.strip():
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI $ref must be a non-empty string.",
            )
        ref = ref.strip()
        if not ref.startswith("#/"):
            raise FactoryAnalysisError(
                FACTORY_EXTERNAL_REF_REJECTED,
                "External or non-document OpenAPI $ref values are rejected.",
            )
        if len(stack) >= MAX_REF_DEPTH:
            raise FactoryAnalysisError(
                FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                f"OpenAPI $ref depth exceeds maximum of {MAX_REF_DEPTH}.",
            )
        if ref in stack:
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI $ref cycle detected.",
            )

        target = self._lookup(ref)
        return self._resolve(target, stack=stack + (ref,), context=context)

    def _lookup(self, ref: str) -> Any:
        # JSON Pointer subset: #/a/b/c
        pointer = ref[1:]  # drop leading '#'
        if not pointer.startswith("/"):
            raise FactoryAnalysisError(
                FACTORY_OPENAPI_STRUCTURE_INVALID,
                "OpenAPI internal $ref pointer is invalid.",
            )
        current: Any = self._document
        for raw_part in pointer.lstrip("/").split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict):
                if part not in current:
                    raise FactoryAnalysisError(
                        FACTORY_OPENAPI_STRUCTURE_INVALID,
                        "OpenAPI internal $ref could not be resolved.",
                    )
                current = current[part]
            elif isinstance(current, list):
                try:
                    index = int(part)
                except ValueError as exc:
                    raise FactoryAnalysisError(
                        FACTORY_OPENAPI_STRUCTURE_INVALID,
                        "OpenAPI internal $ref could not be resolved.",
                    ) from exc
                if index < 0 or index >= len(current):
                    raise FactoryAnalysisError(
                        FACTORY_OPENAPI_STRUCTURE_INVALID,
                        "OpenAPI internal $ref could not be resolved.",
                    )
                current = current[index]
            else:
                raise FactoryAnalysisError(
                    FACTORY_OPENAPI_STRUCTURE_INVALID,
                    "OpenAPI internal $ref could not be resolved.",
                )
        return current

    def _sanitize(self, node: Any, *, depth: int) -> Any:
        if depth > MAX_REF_DEPTH:
            raise FactoryAnalysisError(
                FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                f"OpenAPI schema depth exceeds maximum of {MAX_REF_DEPTH}.",
            )
        self._nodes_seen += 1
        if self._nodes_seen > MAX_SCHEMA_NODES:
            raise FactoryAnalysisError(
                FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                f"OpenAPI schema traversal exceeds maximum of {MAX_SCHEMA_NODES} nodes.",
            )

        if isinstance(node, dict):
            # Resolve nested refs before sanitizing.
            if "$ref" in node:
                resolved = self._resolve(node, stack=(), context="schema")
                return self._sanitize(resolved, depth=depth + 1)

            out: dict[str, Any] = {}
            for key, value in node.items():
                if not isinstance(key, str):
                    continue
                if key.startswith("x-"):
                    continue
                if key in _STRIP_SCHEMA_KEYS:
                    continue
                # Never copy credential-bearing free-form maps from security schemes.
                if key in {"authorizationCode", "clientSecret", "password"}:
                    continue
                out[key] = self._sanitize(value, depth=depth + 1)
            return out

        if isinstance(node, list):
            if len(node) > MAX_SCHEMA_NODES:
                raise FactoryAnalysisError(
                    FACTORY_ANALYSIS_LIMIT_EXCEEDED,
                    "OpenAPI schema list exceeds analyzer limits.",
                )
            return [self._sanitize(item, depth=depth + 1) for item in node]

        if isinstance(node, str):
            return node[:MAX_STRING_LEN]
        if isinstance(node, (int, float, bool)) or node is None:
            return node
        # Reject unexpected YAML objects (dates, etc.) — stringify fail-closed.
        return None


def _bound_str(value: Any, *, max_len: int = MAX_STRING_LEN) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not trimmed:
        return None
    return trimmed[:max_len]
