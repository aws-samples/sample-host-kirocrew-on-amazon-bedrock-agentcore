from __future__ import annotations

import asyncio
import io
import json
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from typing import Any

import pytest
from kirocrew_agentcore_adapter.transport import BindingClaims, SessionInitializationError
from kirocrew_agentcore_persistence.durability import BrokerRefusalError
from kirocrew_agentcore_runtime import aws_runtime, bearer_mcp_proxy
from kirocrew_agentcore_runtime.aws_runtime import (
    BrokeredSandboxStateStore,
    write_tool_token,
)
from kirocrew_agentcore_runtime.supervisor import KiroCrewSupervisor, tool_token_file
from test_aws_runtime import SANDBOX, FakeBrokerLambda, StubAwsSessionInitializer

ENDPOINT = "https://gw-1.gateway.bedrock-agentcore.ap-southeast-1.amazonaws.com/mcp"


class _Response(io.BytesIO):
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        super().__init__(body)
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _token_file(tmp_path: Path, value: str = "tok-1") -> Path:
    path = tmp_path / "tool.token"
    path.write_text(value, encoding="ascii")
    return path


# --- bearer bridge -----------------------------------------------------------


def test_bridge_sends_the_current_token_and_keeps_the_mcp_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _token_file(tmp_path)
    seen: list[Any] = []
    replies = [
        _Response(b'{"jsonrpc":"2.0","id":1,"result":{}}', {"Mcp-Session-Id": "s-1"}),
        _Response(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}'),
    ]

    def urlopen(request: Any, timeout: int) -> _Response:
        seen.append(request)
        return replies.pop(0)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    forwarder = bearer_mcp_proxy.BearerForwarder(ENDPOINT, token)
    forwarder.forward(b'{"id":1}')
    token.write_text("tok-2", encoding="ascii")  # refreshed between requests
    forwarder.forward(b'{"id":2}')

    assert seen[0].get_header("Authorization") == "Bearer tok-1"
    assert seen[0].get_header("Mcp-session-id") is None
    assert seen[1].get_header("Authorization") == "Bearer tok-2"
    assert seen[1].get_header("Mcp-session-id") == "s-1"


def test_bridge_refuses_plain_http_endpoints(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="https"):
        bearer_mcp_proxy.BearerForwarder("http://gw.example/mcp", _token_file(tmp_path))


@pytest.mark.parametrize("value", ["", "two words", "\n"])
def test_bridge_refuses_a_missing_or_malformed_token(tmp_path: Path, value: str) -> None:
    forwarder = bearer_mcp_proxy.BearerForwarder(ENDPOINT, _token_file(tmp_path, value))
    with pytest.raises(RuntimeError, match="empty or malformed"):
        forwarder.forward(b"{}")
    missing = bearer_mcp_proxy.BearerForwarder(ENDPOINT, tmp_path / "absent")
    with pytest.raises(RuntimeError, match="not available yet"):
        missing.forward(b"{}")


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            urllib.error.HTTPError(ENDPOINT, 401, "no", Message(), io.BytesIO(b"expired")),
            "401 means the tool token was rejected",
        ),
        (
            urllib.error.HTTPError(ENDPOINT, 500, "no", Message(), io.BytesIO(b"boom")),
            "HTTP 500 from the tool Gateway: boom",
        ),
        (urllib.error.URLError("dns"), "Cannot reach the tool Gateway"),
    ],
)
def test_bridge_reports_gateway_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, expected: str
) -> None:
    def urlopen(_request: Any, timeout: int) -> _Response:
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(RuntimeError, match=expected):
        bearer_mcp_proxy.BearerForwarder(ENDPOINT, _token_file(tmp_path)).forward(b"{}")


class _Forwarder:
    def __init__(self, replies: list[bytes | Exception]) -> None:
        self.replies = replies

    def forward(self, _payload: bytes) -> bytes:
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_serve_answers_requests_skips_empty_notification_replies_and_blank_lines() -> None:
    stdout = io.StringIO()
    forwarder = _Forwarder(
        [
            b'{"id":1,"result":{}}',
            b"",
            RuntimeError("down"),
            RuntimeError("quiet"),
            RuntimeError("bad"),
            RuntimeError("bad"),
        ]
    )
    stdin = io.StringIO(
        '{"id":1,"method":"initialize"}\n\n'
        '{"method":"notifications/initialized"}\n'
        '{"id":3,"method":"tools/list"}\n'
        '{"method":"notifications/x"}\n'
        "not json\n"
        "[1]\n"
    )
    bearer_mcp_proxy.serve(forwarder, stdin, stdout)  # type: ignore[arg-type]
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0] == {"id": 1, "result": {}}
    assert lines[1]["id"] == 3 and lines[1]["error"]["message"] == "down"
    assert len(lines) == 2


def test_main_wires_the_bridge_from_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []
    monkeypatch.setattr(
        bearer_mcp_proxy, "serve", lambda forwarder, _in, _out: captured.append(forwarder)
    )
    token = _token_file(tmp_path)
    assert bearer_mcp_proxy.main(["--endpoint", ENDPOINT, "--token-file", str(token)]) == 0
    assert isinstance(captured[0], bearer_mcp_proxy.BearerForwarder)


# --- MCP store pin -----------------------------------------------------------


def test_tool_gateway_is_pinned_next_to_other_servers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"mine": {"command": "x"}}}), encoding="utf-8"
    )
    monkeypatch.setenv("TOOL_GATEWAY_URL", ENDPOINT)
    monkeypatch.delenv("TOOL_GATEWAY_TOKEN_FILE", raising=False)

    KiroCrewSupervisor._pin_tool_gateway_mcp(tmp_path)

    servers = json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers["mine"] == {"command": "x"}
    assert servers["tool-gateway"]["args"] == [
        "-m",
        "kirocrew_agentcore_runtime.bearer_mcp_proxy",
        "--endpoint",
        ENDPOINT,
        "--token-file",
        "/tmp/kirocrew-agentcore/tool-gateway.token",  # noqa: S108 - asserted default
    ]


def test_tool_gateway_pin_is_skipped_without_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TOOL_GATEWAY_URL", raising=False)
    KiroCrewSupervisor._pin_tool_gateway_mcp(tmp_path)
    assert not (tmp_path / "mcp.json").exists()


def test_token_file_location_is_overridable_but_never_empty() -> None:
    assert tool_token_file({"TOOL_GATEWAY_TOKEN_FILE": "/run/t"}) == "/run/t"
    assert tool_token_file({"TOOL_GATEWAY_TOKEN_FILE": "  "}).endswith("tool-gateway.token")


# --- runtime refresh ---------------------------------------------------------


def test_store_requests_and_validates_a_tool_token() -> None:
    lambda_client = FakeBrokerLambda({"accessToken": "tok", "expiresIn": 900})
    store = BrokeredSandboxStateStore(lambda_client, "arn:broker")
    store.session_token = "session"  # noqa: S105 - opaque fixture
    assert store.tool_token(SANDBOX, "session-1") == ("tok", 900)
    assert lambda_client.operations() == [("toolToken", "session")]

    bad_responses: list[dict[str, object]] = [
        {"accessToken": "", "expiresIn": 900},
        {"accessToken": "t", "expiresIn": "9"},
    ]
    for bad in bad_responses:
        broken = BrokeredSandboxStateStore(FakeBrokerLambda(bad), "arn:broker")
        broken.session_token = "session"  # noqa: S105 - opaque fixture
        with pytest.raises(SessionInitializationError):
            broken.tool_token(SANDBOX, "session-1")


def test_token_file_is_replaced_atomically_and_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "tool.token"
    write_tool_token(path, "first")
    write_tool_token(path, "second")
    assert path.read_text(encoding="ascii") == "second"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["tool.token"]


class _StopLoopError(Exception):
    pass


@pytest.mark.parametrize(
    ("outcome", "expected_delay", "written"),
    [
        (("tok", 900), 600, "tok"),
        (("tok", 30), 60, "tok"),
        (BrokerRefusalError("not enrolled"), 600, None),
        (RuntimeError("network"), 60, None),
    ],
)
def test_refresh_writes_the_token_and_schedules_the_next_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: tuple[str, int] | Exception,
    expected_delay: int,
    written: str | None,
) -> None:
    path = tmp_path / "tool.token"
    monkeypatch.setenv("TOOL_GATEWAY_TOKEN_FILE", str(path))
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        raise _StopLoopError

    class _Store:
        def tool_token(self, sandbox_id: str, runtime_session_id: str) -> tuple[str, int]:
            assert (sandbox_id, runtime_session_id) == (SANDBOX, "session-1")
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    initializer = object.__new__(aws_runtime.AwsSessionInitializer)
    initializer._state_store = _Store()  # type: ignore[assignment]
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(_StopLoopError):
        asyncio.run(initializer._tool_token_refresh(SANDBOX, "session-1"))

    assert delays == [expected_delay]
    if written is None:
        assert not path.exists()
    else:
        assert path.read_text(encoding="ascii") == written


def test_initializer_starts_the_refresher_only_with_a_tool_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(url: str | None) -> object:
        if url is None:
            monkeypatch.delenv("TOOL_GATEWAY_URL", raising=False)
        else:
            monkeypatch.setenv("TOOL_GATEWAY_URL", url)
        initializer = StubAwsSessionInitializer()
        claims = BindingClaims(SANDBOX, "session-1", 2_000_000_000)
        await initializer.initialize("subject", "binding-1", claims)
        task = initializer._tool_token_task
        for background in (task, initializer._lease_heartbeat_task):
            if background is not None:
                background.cancel()
        return task

    assert asyncio.run(scenario(ENDPOINT)) is not None
    assert asyncio.run(scenario(None)) is None
