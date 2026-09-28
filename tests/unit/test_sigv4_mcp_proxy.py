from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest
from kirocrew_agentcore_runtime import sigv4_mcp_proxy as proxy

ENDPOINT = "https://agent-registry.us-east-1.api.aws/registry/example/mcp"


class FakeCredentials:
    def get_frozen_credentials(self) -> Any:
        return SimpleNamespace(access_key="AKIDEXAMPLE", secret_key="example", token="token")  # noqa: S106


class FakeSession:
    def __init__(self, credentials: object | None = None, **_kwargs: object) -> None:
        self._credentials = FakeCredentials() if credentials is None else credentials

    def get_credentials(self) -> object | None:
        return self._credentials


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@pytest.mark.parametrize(
    ("endpoint", "region"),
    [
        (ENDPOINT, "us-east-1"),
        ("https://gw.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com/mcp", "ap-southeast-1"),
        ("https://example.com/mcp", ""),
        ("https://us-east-1.eu-west-1.example.com/mcp", ""),
        ("https://eu-west-1-backup.example.com/mcp", ""),
    ],
)
def test_region_is_derived_only_when_unambiguous(endpoint: str, region: str) -> None:
    assert proxy.region_from_endpoint(endpoint) == region


def test_forward_signs_request_and_returns_body(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr("boto3.Session", FakeSession)

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
        captured["headers"] = {key.lower(): value for key, value in request.header_items()}
        captured["timeout"] = timeout
        return FakeResponse(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    forwarder = proxy.SigningForwarder(ENDPOINT, "agent-registry", "us-east-1", profile="dev")

    assert forwarder.forward(b'{"id":1}') == b'{"jsonrpc":"2.0","id":1,"result":{}}'
    authorization = captured["headers"]["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256")
    assert "/us-east-1/agent-registry/aws4_request" in authorization
    assert captured["headers"]["x-amz-security-token"] == "token"
    assert captured["timeout"] == 120


def test_forward_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("boto3.Session", lambda **_kw: FakeSession(credentials=False))
    session = proxy.SigningForwarder(ENDPOINT, "agent-registry", "us-east-1")
    session._session = SimpleNamespace(get_credentials=lambda: None)

    with pytest.raises(RuntimeError, match="No AWS credentials"):
        session.forward(b"{}")


@pytest.mark.parametrize(("code", "hint"), [(403, True), (500, False)])
def test_forward_reports_http_errors(
    monkeypatch: pytest.MonkeyPatch, code: int, hint: bool
) -> None:
    monkeypatch.setattr("boto3.Session", FakeSession)

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
        raise urllib.error.HTTPError(ENDPOINT, code, "denied", {}, io.BytesIO(b"AccessDenied"))  # type: ignore[arg-type]

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    forwarder = proxy.SigningForwarder(ENDPOINT, "agent-registry", "us-east-1")

    with pytest.raises(RuntimeError) as error:
        forwarder.forward(b"{}")
    assert f"HTTP {code}" in str(error.value)
    assert "AccessDenied" in str(error.value)
    assert ("IAM denial" in str(error.value)) is hint


def test_forward_reports_unreachable_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("boto3.Session", FakeSession)

    def fake_urlopen(request: urllib.request.Request, timeout: int) -> FakeResponse:
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    forwarder = proxy.SigningForwarder(ENDPOINT, "agent-registry", "us-east-1")

    with pytest.raises(RuntimeError, match="Cannot reach"):
        forwarder.forward(b"{}")


def run_main(
    monkeypatch: pytest.MonkeyPatch, argv: list[str], stdin: str, forward: Any = None
) -> tuple[int, str, str]:
    stdout, stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "argv", ["sigv4_mcp_proxy", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    if forward is not None:
        monkeypatch.setattr("boto3.Session", FakeSession)
        monkeypatch.setattr(proxy.SigningForwarder, "forward", forward)
    code = proxy.main()
    return code, stdout.getvalue(), stderr.getvalue()


def test_main_refuses_without_signing_region(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = run_main(
        monkeypatch,
        ["--endpoint", "https://example.com/mcp", "--service", "agent-registry"],
        "",
    )
    assert code == 2
    assert stdout == ""
    assert "Pass --region" in stderr


def test_main_forwards_lines_and_answers_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def forward(_self: object, payload: bytes) -> bytes:
        if b"fail" in payload:
            raise RuntimeError("boom")
        return payload + b"\n"

    code, stdout, stderr = run_main(
        monkeypatch,
        ["--endpoint", ENDPOINT, "--service", "agent-registry"],
        '\n{"id":1,"method":"tools/list"}\nnot-json\n{"id":2,"method":"fail"}\n{"method":"fail"}\n',
        forward,
    )

    lines = stdout.splitlines()
    assert code == 0
    assert lines[0] == '{"id":1,"method":"tools/list"}'
    assert lines[1] == "not-json"
    assert json.loads(lines[2]) == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {"code": -32603, "message": "boom"},
    }
    assert len(lines) == 3
    assert "signing as agent-registry in us-east-1" in stderr
    assert stderr.count("boom") == 2
