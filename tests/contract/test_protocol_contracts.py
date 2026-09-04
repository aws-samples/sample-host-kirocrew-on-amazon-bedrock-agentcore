from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml
from jsonschema import Draft202012Validator
from kirocrew_agentcore_adapter.protocol import (
    ProtocolContractError,
    sanitize_error,
    validate_envelope,
)

ROOT = Path(__file__).parents[2]
CONTRACTS = ROOT / "contracts"
SCHEMAS = CONTRACTS / "schemas"
FIXTURES = CONTRACTS / "fixtures"


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.contract
def test_json_schemas_are_valid_and_accept_interoperability_fixtures() -> None:
    for schema_path in sorted(SCHEMAS.glob("*.json")):
        schema = cast(dict[str, object], load_json(schema_path))
        Draft202012Validator.check_schema(schema)
    validate_envelope(load_json(FIXTURES / "valid-client-message.json"))
    validate_envelope(load_json(FIXTURES / "valid-server-message.json"))
    with pytest.raises(ProtocolContractError) as raised:
        validate_envelope(load_json(FIXTURES / "invalid-version-message.json"))
    assert raised.value.code == "UNSUPPORTED_PROTOCOL"


@pytest.mark.contract
def test_openapi_and_asyncapi_publish_required_surfaces() -> None:
    openapi = cast(
        dict[str, object], yaml.safe_load((CONTRACTS / "openapi.yaml").read_text(encoding="utf-8"))
    )
    assert openapi["openapi"] == "3.1.0"
    paths = cast(dict[str, object], openapi["paths"])
    assert set(paths) == {
        "/control/v1/config",
        "/control/v1/sandbox",
        "/control/v1/sandbox/start",
        "/control/v1/sandbox/stop",
        "/control/v1/sandbox/checkpoints",
        "/invocations",
    }
    asyncapi = cast(
        dict[str, object], yaml.safe_load((CONTRACTS / "asyncapi.yaml").read_text(encoding="utf-8"))
    )
    assert asyncapi["asyncapi"] == "3.0.0"
    assert set(cast(dict[str, object], asyncapi["operations"])) == {
        "sendCommand",
        "receiveEvent",
    }


@pytest.mark.contract
def test_generated_models_are_current() -> None:
    subprocess.run(
        [sys.executable, "tools/generate_protocol_models.py", "--check"],
        cwd=ROOT,
        check=True,
    )


@pytest.mark.contract
def test_typescript_serialization_is_accepted_by_python_validator() -> None:
    script = """
import { serializeEnvelope } from './frontend-shell/dist/src/protocol.js';
const envelope = {
  version: 'kirocrew-agentcore.v1',
  messageId: '01J00000000000000000000001',
  requestId: '01J00000000000000000000002',
  operation: 'chat.submit',
  sequence: 0,
  timestamp: '2026-08-17T16:00:00Z',
  correlationId: '01J00000000000000000000003',
  payload: { text: 'cross-language' },
};
console.log(serializeEnvelope(envelope));
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = validate_envelope(json.loads(completed.stdout))
    assert envelope["payload"] == {"text": "cross-language"}


@pytest.mark.contract
def test_secret_bearing_error_fixture_is_sanitized_and_schema_valid() -> None:
    fixture = load_json(FIXTURES / "secret-bearing-error-input.json")
    assert isinstance(fixture, dict)
    sanitized = sanitize_error(fixture)
    assert "authorization" not in json.dumps(sanitized).lower()


@pytest.mark.contract
def test_control_handler_route_table_matches_openapi() -> None:
    from kirocrew_agentcore_control.api import ROUTES

    document = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text(encoding="utf-8"))
    declared = {
        (method.upper(), path): operation["operationId"]
        for path, methods in document["paths"].items()
        if path.startswith("/control/v1/")
        for method, operation in methods.items()
        if method in {"get", "post", "delete"}
    }
    assert ROUTES == declared
