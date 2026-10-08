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


def test_missing_info_rejected() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes({"openapi": "3.0.0", "paths": {}}))
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID
    assert "info" in exc.value.message.lower()


def test_info_requires_title_and_version() -> None:
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(
            _json_bytes({"openapi": "3.0.0", "info": {"title": "t"}, "paths": {}})
        )
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID


def test_unused_component_external_http_ref_rejected() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Unused": {"$ref": "https://evil.example/unused.json"},
            }
        },
    }
    with (
        patch("socket.create_connection") as mock_conn,
        pytest.raises(FactoryAnalysisError) as exc,
    ):
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_EXTERNAL_REF_REJECTED
    mock_conn.assert_not_called()
    assert "evil.example" not in exc.value.message


def test_unused_component_file_ref_rejected() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {"Unused": {"$ref": "../shared/pet.yaml#/Pet"}},
        },
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_EXTERNAL_REF_REJECTED
    assert "../shared" not in exc.value.message


def test_unresolved_internal_ref_outside_request_response() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {},
        "components": {
            "schemas": {
                "Broken": {"$ref": "#/components/schemas/DoesNotExist"},
            }
        },
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_OPENAPI_STRUCTURE_INVALID
    assert "DoesNotExist" not in exc.value.message


def test_path_item_internal_ref_resolves_operations() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/ping": {"$ref": "#/components/pathItems/Ping"},
        },
        "components": {
            "pathItems": {
                "Ping": {
                    "get": {
                        "operationId": "getPing",
                        "responses": {"200": {"description": "ok"}},
                    }
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    assert len(result.operations) == 1
    assert result.operations[0].operation_key == "GET /ping"
    assert result.operations[0].operation_id == "getPing"


def test_path_item_external_ref_rejected() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/ping": {"$ref": "https://evil.example/paths/ping.json"},
        },
    }
    with pytest.raises(FactoryAnalysisError) as exc:
        analyze_openapi(_json_bytes(doc))
    assert exc.value.code == FACTORY_EXTERNAL_REF_REJECTED
    assert "evil.example" not in exc.value.message


def test_yaml_alias_recursive_traversal_bounded() -> None:
    # YAML anchors can create shared/recursive objects; traversal must terminate.
    yaml_src = """\
openapi: "3.0.0"
info:
  title: Recursive
  version: "1"
paths: {}
x-loop: &loop
  marker: 1
  nest: *loop
"""
    result = analyze_openapi(yaml_src.encode("utf-8"), filename="recursive.yaml")
    assert result.source_format == "YAML"
    assert result.operations == []


def test_path_and_query_parameters_extracted() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/pets/{petId}": {
                "parameters": [
                    {
                        "name": "petId",
                        "in": "path",
                        "required": False,
                        "schema": {"type": "string"},
                    }
                ],
                "get": {
                    "parameters": [
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    params = result.operations[0].parameters
    assert [(p.name, p.location, p.required) for p in params] == [
        ("petId", "path", True),  # path params always required
        ("limit", "query", False),
    ]
    assert params[0].schema == {"type": "string"}
    assert params[1].schema == {"type": "integer"}


def test_parameter_path_operation_merge_override() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/items": {
                "parameters": [
                    {
                        "name": "q",
                        "in": "query",
                        "required": False,
                        "schema": {"type": "string", "description": "path-level"},
                    },
                    {
                        "name": "x-trace",
                        "in": "header",
                        "required": False,
                        "schema": {"type": "string"},
                    },
                ],
                "get": {
                    "parameters": [
                        {
                            "name": "q",
                            "in": "query",
                            "required": True,
                            "schema": {"type": "string", "description": "op-level"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                },
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    params = result.operations[0].parameters
    assert [p.name for p in params] == ["q", "x-trace"]
    assert params[0].required is True
    assert params[0].schema is not None
    assert params[0].schema["description"] == "op-level"


def test_parameter_internal_ref() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "parameters": [{"$ref": "#/components/parameters/Page"}],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
        "components": {
            "parameters": {
                "Page": {
                    "name": "page",
                    "in": "query",
                    "required": False,
                    "schema": {"type": "integer"},
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    params = result.operations[0].parameters
    assert len(params) == 1
    assert params[0].name == "page"
    assert params[0].location == "query"
    assert params[0].schema == {"type": "integer"}


def test_parameter_examples_defaults_extensions_not_leaked() -> None:
    secret = "param-secret-value-7c2e"
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x": {
                "get": {
                    "parameters": [
                        {
                            "name": "token",
                            "in": "header",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "example": secret,
                                "default": secret,
                                "x-secret": secret,
                            },
                            "example": secret,
                            "x-example": secret,
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    dumped = result.model_dump_json()
    assert secret not in dumped
    param = result.operations[0].parameters[0]
    assert param.schema == {"type": "string"}
    assert "example" not in (param.schema or {})
    assert "default" not in (param.schema or {})
    assert "x-secret" not in (param.schema or {})


def test_parameter_deterministic_ordering() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/x/{b}/{a}": {
                "get": {
                    "parameters": [
                        {"name": "z", "in": "cookie", "schema": {"type": "string"}},
                        {"name": "b", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "h", "in": "header", "schema": {"type": "string"}},
                        {"name": "a", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "q", "in": "query", "schema": {"type": "string"}},
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    keys = [(p.location, p.name) for p in result.operations[0].parameters]
    assert keys == [
        ("path", "a"),
        ("path", "b"),
        ("query", "q"),
        ("header", "h"),
        ("cookie", "z"),
    ]


def test_success_response_prefers_202_over_default() -> None:
    doc = {
        "openapi": "3.0.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/async": {
                "post": {
                    "responses": {
                        "default": {
                            "description": "fallback",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"fallback": {"type": "boolean"}},
                                    }
                                }
                            },
                        },
                        "202": {
                            "description": "accepted",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"accepted": {"type": "boolean"}},
                                    }
                                }
                            },
                        },
                    }
                }
            }
        },
    }
    result = analyze_openapi(_json_bytes(doc))
    schema = result.operations[0].response_schema
    assert schema is not None
    assert "accepted" in schema["properties"]
    assert "fallback" not in schema["properties"]
