"""Unit tests for Tool Factory OpenAPI analyzer foundation (PR #76).

No network or filesystem access. Pure in-memory source bytes only.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from unittest.mock import patch

import pytest
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
from app.factory.openapi_analyzer import (
    MAX_OPERATIONS,
    MAX_PATHS,
    MAX_REF_DEPTH,
    MAX_SOURCE_BYTES,
    analyze_openapi,
)

MINIMAL_30: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {"title": "Demo", "version": "1.0.0"},
    "paths": {
        "/ping": {
            "get": {
                "operationId": "ping",
                "summary": "Ping",
                "responses": {"200": {"description": "ok"}},
            }
        }
    },
}

MINIMAL_31_YAML = """\
openapi: 3.1.0
info:
  title: Demo YAML
  version: "2.0.0"
paths:
  /health:
    get:
      summary: Health
      responses:
        "200":
          description: ok
"""


def _json_bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")


def test_valid_minimal_openapi_30_json() -> None:
    result = analyze_openapi(_json_bytes(MINIMAL_30), filename="demo.json")
    assert result.openapi_version == "3.0.3"
    assert result.source_format == "JSON"
    assert result.title == "Demo"
    assert result.version == "1.0.0"
    assert len(result.operations) == 1
    op = result.operations[0]
    assert op.operation_key == "GET /ping"
    assert op.method == "GET"
    assert op.path == "/ping"
    assert op.operation_id == "ping"


def test_valid_minimal_openapi_31_yaml() -> None:
    result = analyze_openapi(MINIMAL_31_YAML.encode("utf-8"), filename="demo.yaml")
    assert result.openapi_version == "3.1.0"
    assert result.source_format == "YAML"
    assert result.title == "Demo YAML"
    assert len(result.operations) == 1
    assert result.operations[0].operation_id is None
    assert result.operations[0].summary == "Health"


def test_deterministic_source_sha256() -> None:
    raw = _json_bytes(MINIMAL_30)
    expected = hashlib.sha256(raw).hexdigest()
    result = analyze_openapi(raw)
    assert result.source_sha256 == expected
    assert result.source_sha256 == expected.lower()
    assert analyze_openapi(raw).source_sha256 == expected


def test_deterministic_operation_ordering() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "Order", "version": "1"},
        "paths": {
            "/z": {"post": {"responses": {"200": {"description": "ok"}}}},
            "/a": {
                "get": {"responses": {"200": {"description": "ok"}}},
                "post": {"responses": {"200": {"description": "ok"}}},
            },
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    keys = [op.operation_key for op in result.operations]
    assert keys == ["GET /a", "POST /a", "POST /z"]


def test_multiple_operations_and_schemas() -> None:
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1"},
        "servers": [{"url": "https://api.example.com/v1", "description": "prod"}],
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "tags": ["pets"],
                    "responses": {
                        "200": {
                            "description": "list",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    }
                                }
                            },
                        }
                    },
                },
                "post": {
                    "operationId": "createPet",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Pet"}
                                }
                            },
                        }
                    },
                },
            }
        },
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    assert len(result.operations) == 2
    assert result.servers[0].url == "https://api.example.com/v1"
    create = result.operations[1]
    assert create.method == "POST"
    assert create.request_schema is not None
    assert create.request_schema["type"] == "object"
    assert "name" in create.request_schema["properties"]
    assert create.response_schema is not None
    assert create.response_schema["type"] == "object"
    assert "id" in create.response_schema["properties"]


def test_missing_operation_id_safe() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/x": {"get": {"responses": {"200": {"description": "ok"}}}},
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    assert result.operations[0].operation_id is None
    assert result.operations[0].operation_key == "GET /x"


def test_malformed_json() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(b"{not-json", filename="bad.json")
    assert exc.value.code == FACTORY_SOURCE_PARSE_ERROR
    assert "{not-json" not in exc.value.message


def test_malformed_yaml() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(b"openapi: [\n  - unfinished", filename="bad.yaml")
    assert exc.value.code == FACTORY_SOURCE_PARSE_ERROR


def test_root_non_object() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(b"[1, 2, 3]", filename="list.json")
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID


def test_missing_openapi_field() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes({"info": {"title": "x", "version": "1"}, "paths": {}}))
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID


def test_unsupported_swagger_20() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes(
                {
                    "swagger": "2.0",
                    "info": {"title": "old", "version": "1"},
                    "paths": {},
                }
            )
        )
    assert exc.value.code == FACTORY_OPENAPI_VERSION_UNSUPPORTED
    assert "Swagger" in exc.value.message


def test_source_too_large() -> None:
    huge = b"{" + (b"a" * (MAX_SOURCE_BYTES + 1))
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(huge)
    assert exc.value.code == FACTORY_SOURCE_TOO_LARGE
    assert huge[:20].decode("latin-1") not in exc.value.message


def test_paths_invalid_type() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "paths": ["/not-an-object"],
                }
            )
        )
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID


def test_invalid_server_scheme() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "servers": [{"url": "ftp://files.example/api"}],
                    "paths": {},
                }
            )
        )
    assert exc.value.code == FACTORY_SERVER_URL_INVALID


def test_server_userinfo_rejected() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "servers": [{"url": "https://user:secret@api.example.com/v1"}],
                    "paths": {},
                }
            )
        )
    assert exc.value.code == FACTORY_SERVER_URL_INVALID
    assert "userinfo" in exc.value.message.lower()
    assert "secret" not in exc.value.message


def test_server_template_url_fail_closed() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "t", "version": "1"},
                    "servers": [
                        {
                            "url": "https://{host}/v1",
                            "variables": {"host": {"default": "x"}},
                        }
                    ],
                    "paths": {},
                }
            )
        )
    assert exc.value.code == FACTORY_SERVER_URL_INVALID


def test_external_http_ref_rejected_zero_network() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "https://evil.example/schemas/Pet.json"
                                    }
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    with (
        patch("socket.create_connection") as mock_conn,
        patch("urllib.request.urlopen") as mock_urlopen,
        pytest.raises(FactoryAnalysisError) as exc,
    ):
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_EXTERNAL_REF_REJECTED
    mock_conn.assert_not_called()
    mock_urlopen.assert_not_called()
    assert "evil.example" not in exc.value.message


def test_file_path_external_ref_rejected() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "./local-schema.yaml#/Pet"}
                            }
                        }
                    },
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_EXTERNAL_REF_REJECTED


def test_unresolved_internal_ref() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Missing"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {"schemas": {}},
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID
    assert "resolved" in exc.value.message.lower()


def test_internal_ref_cycle_bounded() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/A"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "A": {"$ref": "#/components/schemas/B"},
                "B": {"$ref": "#/components/schemas/A"},
            }
        },
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID
    assert "cycle" in exc.value.message.lower()


def test_ref_depth_limit() -> None:
    schemas: dict[str, Any] = {}
    for i in range(MAX_REF_DEPTH + 2):
        nxt = i + 1
        schemas[f"S{i}"] = {"$ref": f"#/components/schemas/S{nxt}"}
    schemas[f"S{MAX_REF_DEPTH + 2}"] = {"type": "string"}
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/S0"}
                                }
                            },
                        }
                    }
                }
            }
        },
        "components": {"schemas": schemas},
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_ANALYSIS_LIMIT_EXCEEDED


def test_excessive_paths_bounded() -> None:
    paths = {
        f"/p{i}": {"get": {"responses": {"200": {"description": "ok"}}}}
        for i in range(MAX_PATHS + 1)
    }
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": paths,
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_ANALYSIS_LIMIT_EXCEEDED


def test_excessive_operations_bounded() -> None:
    # Stay under MAX_PATHS but exceed MAX_OPERATIONS via multiple methods.
    methods = ["get", "put", "post", "delete", "patch", "options", "head", "trace"]
    paths_needed = (MAX_OPERATIONS // len(methods)) + 2
    assert paths_needed <= MAX_PATHS
    paths: dict[str, Any] = {}
    for i in range(paths_needed):
        paths[f"/p{i}"] = {
            m: {"responses": {"200": {"description": "ok"}}} for m in methods
        }
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": paths,
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_ANALYSIS_LIMIT_EXCEEDED


def test_extensions_and_credential_like_values_not_leaked() -> None:
    secret = "super-secret-api-key-value-9f3a"
    auth_header = f"Bearer {secret}"
    doc = {
        "openapi": "3.0.3",
        "info": {"title": "Sec", "version": "1", "x-internal-token": secret},
        "x-api-key": secret,
        "components": {
            "securitySchemes": {
                "ApiKey": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "x-example": secret,
                }
            },
            "schemas": {
                "AuthBody": {
                    "type": "object",
                    "properties": {
                        "token": {
                            "type": "string",
                            "example": secret,
                            "default": secret,
                            "x-credential": secret,
                        }
                    },
                    "example": {"token": secret},
                }
            },
        },
        "paths": {
            "/login": {
                "post": {
                    "x-auth-example": auth_header,
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/AuthBody"},
                                "example": {"token": secret, "Authorization": auth_header},
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "ok": {"type": "boolean"}
                                        },
                                    },
                                    "examples": {
                                        "a": {
                                            "value": {
                                                "Authorization": auth_header,
                                                "apiKey": secret,
                                            }
                                        }
                                    },
                                }
                            },
                        }
                    },
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    dumped = result.model_dump_json()
    assert secret not in dumped
    assert auth_header not in dumped
    assert "x-api-key" not in dumped
    assert "x-internal-token" not in dumped
    assert "securitySchemes" not in dumped
    assert "Authorization" not in dumped
    create = result.operations[0]
    assert create.request_schema is not None
    token_schema = create.request_schema["properties"]["token"]
    assert "example" not in token_schema
    assert "default" not in token_schema
    assert "x-credential" not in token_schema


def test_error_messages_omit_raw_source_and_secrets() -> None:
    secret = "cred-leak-should-not-appear"
    raw = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": secret, "version": "1"},
            "servers": [{"url": f"https://user:{secret}@api.example.com"}],
            "paths": {},
        }
    ).encode("utf-8")
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(raw)
    assert secret not in exc.value.message
    assert raw.decode("utf-8") not in exc.value.message


def test_deprecated_flag_and_tags_bounded() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/old": {
                "get": {
                    "deprecated": True,
                    "tags": ["a", "b"],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    assert result.operations[0].deprecated is True
    assert result.operations[0].tags == ["a", "b"]
