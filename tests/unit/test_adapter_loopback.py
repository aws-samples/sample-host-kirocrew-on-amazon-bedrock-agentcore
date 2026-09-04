from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping
from types import SimpleNamespace
from typing import cast

import pytest
from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, WSMsgType
from kirocrew_agentcore_adapter.loopback import (
    KiroCrewRoutePolicy,
    LoopbackKiroCrewBackend,
    LoopbackRequest,
    RouteDisposition,
    RouteRule,
)
from kirocrew_agentcore_adapter.transport import AdapterRequestError, TransientBackendError


class FakeTokenProvider:
    def __init__(self) -> None:
        self.calls = 0

    def token(self, *, renewal_window_seconds: float = 300.0) -> str:
        assert renewal_window_seconds == 300.0
        self.calls += 1
        return "loopback-only-secret"


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.requested_size: int | None = None

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        self.requested_size = size
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        *,
        chunks: list[bytes] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.content = FakeContent(chunks or [])
        self.closed = False
        self.released = False

    def close(self) -> None:
        self.closed = True

    def release(self) -> None:
        self.released = True


class FakeWebSocket:
    def __init__(self, messages: list[SimpleNamespace]) -> None:
        self.messages = messages
        self.sent: list[str] = []
        self.sent_bytes: list[bytes] = []
        self.closed: list[tuple[int, bytes]] = []

    def __aiter__(self) -> FakeWebSocket:
        return self

    async def __anext__(self) -> SimpleNamespace:
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)

    async def send_str(self, frame: str) -> None:
        self.sent.append(frame)

    async def send_bytes(self, frame: bytes) -> None:
        self.sent_bytes.append(frame)

    async def close(self, *, code: int = 1000, message: bytes = b"") -> bool:
        self.closed.append((code, message))
        return True


class FakeSession:
    def __init__(
        self,
        *,
        response: FakeResponse | None = None,
        websocket: FakeWebSocket | None = None,
        request_error: BaseException | None = None,
        websocket_error: BaseException | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.websocket = websocket or FakeWebSocket([])
        self.request_error = request_error
        self.websocket_error = websocket_error
        self.request_call: dict[str, object] = {}
        self.websocket_call: dict[str, object] = {}

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.request_call = {"method": method, "url": url, **kwargs}
        if self.request_error is not None:
            raise self.request_error
        return self.response

    async def ws_connect(self, url: str, **kwargs: object) -> FakeWebSocket:
        self.websocket_call = {"url": url, **kwargs}
        if self.websocket_error is not None:
            raise self.websocket_error
        return self.websocket


def backend(
    session: FakeSession, token: FakeTokenProvider | None = None, *, timeout: float = 30
) -> LoopbackKiroCrewBackend:
    return LoopbackKiroCrewBackend(
        cast(ClientSession, session), token or FakeTokenProvider(), timeout_seconds=timeout
    )


async def collect(
    stream: AsyncIterator[tuple[str, Mapping[str, object]]],
) -> list[tuple[str, Mapping[str, object]]]:
    return [event async for event in stream]


def test_route_rules_and_policy_are_closed_by_default() -> None:
    exact = RouteRule(frozenset({"GET"}), "/api/exact", False)
    prefix = RouteRule(frozenset({"GET"}), "/api/prefix")
    assert exact.matches("GET", "/api/exact")
    assert not exact.matches("GET", "/api/exact/child")
    assert not exact.matches("POST", "/api/exact")
    assert prefix.matches("GET", "/api/prefix")
    assert prefix.matches("GET", "/api/prefix/child")

    policy = KiroCrewRoutePolicy()
    assert policy.VERSION == "0.2.0"
    assert policy.classify("get", "/api/auth/status") is RouteDisposition.SYNTHETIC
    assert policy.classify("POST", "/api/auth/logout") is RouteDisposition.SYNTHETIC
    assert policy.classify("GET", "/api/chat?q=1") is RouteDisposition.ALLOWED
    assert policy.classify("DELETE", "/api/conversations/abc") is RouteDisposition.ALLOWED
    assert policy.classify("POST", "/api/desktop") is RouteDisposition.UNAVAILABLE
    assert policy.classify("GET", "/api/files/pick") is RouteDisposition.UNAVAILABLE
    assert policy.classify("GET", "/api/ws/terminal/abc") is RouteDisposition.ALLOWED
    assert policy.classify("POST", "/api/terminal/sessions") is RouteDisposition.ALLOWED
    assert policy.classify("GET", "/api/system") is RouteDisposition.ALLOWED
    assert policy.classify("GET", "/api/file-raw?path=artifact.txt") is RouteDisposition.ALLOWED
    # Regression guard for the narrow allowlist that 403'd most of the product:
    # these are real upstream routes the bundle calls and they must tunnel.
    for method, path in (
        ("GET", "/api/crons"),
        ("POST", "/api/crons"),
        ("GET", "/api/crons/history"),
        ("GET", "/api/cron-folders"),
        ("GET", "/api/voice/config"),
        ("GET", "/api/voice/voices"),
        ("GET", "/api/auth/me"),
        ("GET", "/api/computer-use/config"),
        ("GET", "/api/tunnel/status"),
        ("GET", "/api/sessions"),
        ("GET", "/api/notifications"),
        ("GET", "/api/models"),
        ("GET", "/api/themes"),
        ("POST", "/api/taskrunner/plan"),
        ("GET", "/api/ws/stt"),
        ("DELETE", "/api/terminal/sessions"),
        ("GET", "/api/system/session-storage"),
        ("POST", "/api/file-raw"),
        ("DELETE", "/api/chat"),
        ("POST", "/api/new-upstream-route"),
    ):
        assert policy.classify(method, path) is RouteDisposition.ALLOWED
    for path in (
        "/api/token",
        "/api/token/local",
        "/api/auth/token/new",
        "/api/shutdown",
        "/api/secrets",
        "/api/config/export",
        "/api/chat/../secrets",
        "/api//chat",
        "api/chat",
        "https://example.com/api/chat",
        "/api/chat#fragment",
    ):
        assert policy.classify("GET", path) is RouteDisposition.DENIED


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"path": "/api/chat", "method": 1},
        {"path": "/api/chat", "transport": "smtp"},
        {"path": "/api/chat", "headers": []},
        {"path": "/api/chat", "body": 1},
        {"path": "/api/chat", "frames": "bad"},
        {"path": "/api/chat", "headers": {1: "x"}},
        {"path": "/api/chat", "headers": {"accept": 1}},
        {"path": "/api/chat", "frames": [1]},
        {"path": "/api/chat", "bodyEncoding": "rot13"},
        {"path": "/api/chat", "bodyEncoding": "base64", "body": "***"},
        {"path": "/api/chat", "body": "\ud800"},
    ],
)
def test_loopback_request_rejects_invalid_shapes(payload: Mapping[str, object]) -> None:
    with pytest.raises(AdapterRequestError) as error:
        LoopbackRequest.from_payload(payload)
    assert error.value.code == "INVALID_MESSAGE"


def test_loopback_request_parses_safe_headers_encodings_and_size() -> None:
    request = LoopbackRequest.from_payload(
        {
            "body": base64.b64encode(b"hello").decode(),
            "bodyEncoding": "base64",
            "frames": ["one", "two"],
            "headers": {"Accept": "text/event-stream", "Cookie": "never-forward"},
            "method": "post",
            "path": "/api/chat?mode=x",
            "transport": "sse",
        }
    )
    assert request.method == "POST"
    assert request.body == b"hello"
    assert request.headers == {"accept": "text/event-stream"}
    assert request.frames == ("one", "two")
    with pytest.raises(AdapterRequestError) as error:
        LoopbackRequest.from_payload({"path": "/api/chat", "body": "x" * (1024 * 1024 + 1)})
    assert error.value.code == "FRAME_TOO_LARGE"


def test_backend_constructor_and_operation_validation() -> None:
    with pytest.raises(ValueError):
        backend(FakeSession(), timeout=0)

    async def scenario() -> None:
        with pytest.raises(AdapterRequestError) as error:
            await collect(backend(FakeSession()).execute("chat.submit", "request", {}))
        assert error.value.code == "INVALID_MESSAGE"

    asyncio.run(scenario())


def test_synthetic_auth_responses_never_request_or_expose_real_token() -> None:
    async def scenario() -> None:
        token = FakeTokenProvider()
        session = FakeSession()
        adapter = backend(session, token)
        for path in (
            "/api/auth/status",
            "/api/auth/local-token",
            "/api/auth/refresh",
            "/api/auth/logout",
        ):
            method = "POST" if path in {"/api/auth/refresh", "/api/auth/logout"} else "GET"
            events = await collect(
                adapter.execute(
                    "kirocrew.http",
                    path,
                    {"method": method, "path": path, "transport": "http"},
                )
            )
            assert [operation for operation, _payload in events] == [
                "request.accepted",
                "output.delta",
                "request.completed",
            ]
            body = json.loads(base64.b64decode(cast(str, events[1][1]["data"])))
            assert "loopback-only-secret" not in json.dumps(body)
            assert body.get("token") is None or path == "/api/auth/logout"
            if path == "/api/auth/logout":
                assert body == {"disconnected": True, "remoteSession": True}
        assert token.calls == 0
        assert session.request_call == {}

    asyncio.run(scenario())


def test_denied_and_host_native_routes_map_to_stable_errors() -> None:
    async def scenario() -> None:
        adapter = backend(FakeSession())
        with pytest.raises(AdapterRequestError) as denied:
            await collect(
                adapter.execute("kirocrew.http", "one", {"method": "POST", "path": "/api/shutdown"})
            )
        assert denied.value.code == "AUTHORIZATION_FAILED"
        with pytest.raises(AdapterRequestError) as unavailable:
            await collect(
                adapter.execute("kirocrew.http", "two", {"method": "GET", "path": "/api/desktop"})
            )
        assert unavailable.value.code == "FEATURE_UNAVAILABLE_IN_AGENTCORE"

    asyncio.run(scenario())


def test_http_and_sse_translation_confines_token_and_headers() -> None:
    async def scenario() -> None:
        response = FakeResponse(
            201,
            chunks=[b"first", b"second"],
            headers={
                "Content-Type": "application/json",
                "ETag": "abc",
                "Set-Cookie": "never-return",
            },
        )
        session = FakeSession(response=response)
        token = FakeTokenProvider()
        adapter = backend(session, token)
        events = await collect(
            adapter.execute(
                "kirocrew.http",
                "request",
                {
                    "body": "payload",
                    "headers": {
                        "authorization": "browser-token",
                        "content-type": "application/json",
                        "cookie": "browser-cookie",
                    },
                    "method": "POST",
                    "path": "/api/chat?stream=1",
                    "transport": "sse",
                },
            )
        )
        assert [operation for operation, _payload in events] == [
            "request.accepted",
            "output.delta",
            "output.delta",
            "request.completed",
        ]
        assert events[0][1]["status"] == 201
        assert events[0][1]["headers"] == {
            "content-type": "application/json",
            "etag": "abc",
        }
        assert base64.b64decode(cast(str, events[1][1]["data"])) == b"first"
        assert session.request_call["url"] == "http://127.0.0.1:5476/api/chat?stream=1"
        headers = cast(Mapping[str, str], session.request_call["headers"])
        assert headers == {
            "authorization": "Bearer loopback-only-secret",
            "content-type": "application/json",
            "cookie": "mc_token_5476=loopback-only-secret",
        }
        assert session.request_call["allow_redirects"] is False
        assert response.content.requested_size == 16 * 1024
        assert response.released
        assert token.calls == 1
        assert await adapter.cancel("request") is False

    asyncio.run(scenario())


def test_http_response_total_size_is_bounded() -> None:
    async def scenario() -> None:
        response = FakeResponse(chunks=[b"x" * (8 * 1024 * 1024), b"y"])
        adapter = backend(FakeSession(response=response))
        with pytest.raises(AdapterRequestError) as error:
            await collect(
                adapter.execute(
                    "kirocrew.http",
                    "bounded-file",
                    {"method": "GET", "path": "/api/file-raw?path=artifact.bin"},
                )
            )
        assert error.value.code == "FRAME_TOO_LARGE"
        assert response.closed

    asyncio.run(scenario())


def test_http_cancellation_closes_active_response() -> None:
    async def scenario() -> None:
        response = FakeResponse(chunks=[b"body"])
        adapter = backend(FakeSession(response=response))
        stream = adapter.execute(
            "kirocrew.http", "active", {"method": "GET", "path": "/api/status"}
        )
        assert (await anext(stream))[0] == "request.accepted"
        assert await adapter.cancel("active")
        assert response.closed
        remaining = [event async for event in stream]
        assert remaining[-1][0] == "request.completed"

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [502, 504])
def test_transient_loopback_statuses_are_retryable(status: int) -> None:
    async def scenario() -> None:
        response = FakeResponse(status)
        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(response=response)).execute(
                    "kirocrew.http", "request", {"method": "GET", "path": "/api/status"}
                )
            )
        assert response.closed

    asyncio.run(scenario())


def test_sse_streams_have_no_total_deadline() -> None:
    """`/api/stream` stays open as long as the dashboard does; a total timeout
    would cut every stream at the deadline and surface as KIROCREW_UNAVAILABLE."""

    async def scenario() -> None:
        session = FakeSession(response=FakeResponse(200, chunks=[b"event: x\n\n"]))
        await collect(
            backend(session, timeout=30).execute(
                "kirocrew.http",
                "request",
                {"method": "GET", "path": "/api/stream", "transport": "sse"},
            )
        )
        timeout = cast(ClientTimeout, session.request_call["timeout"])
        assert timeout.total is None
        assert timeout.sock_connect == 30

        plain = FakeSession(response=FakeResponse(200))
        await collect(
            backend(plain, timeout=30).execute(
                "kirocrew.http", "request", {"method": "GET", "path": "/api/status"}
            )
        )
        assert cast(ClientTimeout, plain.request_call["timeout"]).total == 30

    asyncio.run(scenario())


def test_bodyless_503_is_retryable() -> None:
    async def scenario() -> None:
        response = FakeResponse(503)
        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(response=response)).execute(
                    "kirocrew.http", "request", {"method": "GET", "path": "/api/status"}
                )
            )
        assert response.closed

    asyncio.run(scenario())


def test_opaque_503_body_is_retryable() -> None:
    async def scenario() -> None:
        response = FakeResponse(503, chunks=[b"Service Unavailable"])
        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(response=response)).execute(
                    "kirocrew.http", "request", {"method": "GET", "path": "/api/status"}
                )
            )
        assert response.closed

    asyncio.run(scenario())


def test_oversized_503_body_stops_reading_and_is_transient() -> None:
    """A 503 whose body runs past the classification cap cannot be a short error
    envelope; the read stops early and the status stays transient."""

    async def scenario() -> None:
        oversized = b"x" * (64 * 1024 + 1)
        response = FakeResponse(503, chunks=[oversized])
        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(response=response)).execute(
                    "kirocrew.http", "request", {"method": "GET", "path": "/api/models"}
                )
            )
        assert response.closed

    asyncio.run(scenario())


def test_503_with_application_code_is_tunnelled_not_escalated() -> None:
    """A Kiro-prerequisite 503 is the gateway's real answer, not an outage: it
    must reach the browser as a normal proxied 503, never KIROCREW_UNAVAILABLE."""

    async def scenario() -> None:
        body = json.dumps(
            {
                "error": "Kiro CLI setup or sign-in is required before starting a session.",
                "code": "kiro_prerequisite_required",
            }
        ).encode()
        response = FakeResponse(
            503,
            chunks=[body],
            headers={"content-type": "application/json"},
        )
        events = await collect(
            backend(FakeSession(response=response)).execute(
                "kirocrew.http", "request", {"method": "GET", "path": "/api/models"}
            )
        )
        operations = [operation for operation, _ in events]
        assert operations == ["request.accepted", "output.delta", "request.completed"]
        accepted = events[0][1]
        assert accepted["status"] == 503
        delta = events[1][1]
        assert base64.b64decode(cast(str, delta["data"])) == body
        assert events[-1][1]["status"] == 503
        assert response.released

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error", [ClientConnectionError("down"), TimeoutError("late"), TimeoutError()]
)
def test_loopback_connection_failures_are_transient(error: BaseException) -> None:
    async def scenario() -> None:
        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(request_error=error)).execute(
                    "kirocrew.http", "request", {"method": "GET", "path": "/api/status"}
                )
            )

    asyncio.run(scenario())


def test_websocket_translation_text_binary_close_and_frames() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket(
            [
                SimpleNamespace(type=WSMsgType.TEXT, data="hello"),
                SimpleNamespace(type=WSMsgType.BINARY, data=b"bytes"),
                SimpleNamespace(type=WSMsgType.CLOSE, data=None),
            ]
        )
        session = FakeSession(websocket=websocket)
        adapter = backend(session)
        events = await collect(
            adapter.execute(
                "kirocrew.http",
                "ws-request",
                {
                    "frames": ["one", "two"],
                    "method": "GET",
                    "path": "/api/ws?channel=main",
                    "transport": "websocket",
                },
            )
        )
        assert [operation for operation, _payload in events] == [
            "request.accepted",
            "output.delta",
            "output.delta",
            "request.completed",
        ]
        assert events[1][1]["data"] == "hello"
        assert base64.b64decode(cast(str, events[2][1]["data"])) == b"bytes"
        assert websocket.sent == ["one", "two"]
        assert session.websocket_call["url"] == "ws://127.0.0.1:5476/api/ws?channel=main"
        headers = cast(Mapping[str, str], session.websocket_call["headers"])
        assert headers["authorization"] == "Bearer loopback-only-secret"

    asyncio.run(scenario())


def test_live_tunnel_send_and_close_bridge_interactive_terminals() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket([SimpleNamespace(type=WSMsgType.TEXT, data="shell-output")])
        adapter = backend(FakeSession(websocket=websocket))
        stream = adapter.execute(
            "kirocrew.http",
            "term-tunnel",
            {"method": "GET", "path": "/api/ws/terminal/abc", "transport": "websocket"},
        )
        assert (await anext(stream))[0] == "request.accepted"

        # Text and binary frames reach the upstream terminal socket live.
        events = await collect(
            adapter.execute(
                "kirocrew.ws.send",
                "send-1",
                {"tunnelId": "term-tunnel", "data": "ls -la\n", "encoding": "utf8"},
            )
        )
        assert events == [("request.completed", {"delivered": True})]
        events = await collect(
            adapter.execute(
                "kirocrew.ws.send",
                "send-2",
                {
                    "tunnelId": "term-tunnel",
                    "data": base64.b64encode(b"\x1b[A").decode(),
                    "encoding": "base64",
                },
            )
        )
        assert events == [("request.completed", {"delivered": True})]
        assert websocket.sent == ["ls -la\n"]
        assert websocket.sent_bytes == [b"\x1b[A"]

        # Invalid payloads and unknown tunnels are rejected.
        for payload in (
            {"tunnelId": "term-tunnel", "data": "x", "encoding": "hex"},
            {"tunnelId": "term-tunnel", "data": 5},
            {"tunnelId": "term-tunnel", "data": "!!!", "encoding": "base64"},
        ):
            with pytest.raises(AdapterRequestError) as excinfo:
                await collect(adapter.execute("kirocrew.ws.send", "send-x", payload))
            assert excinfo.value.status == 400
        with pytest.raises(AdapterRequestError) as excinfo:
            await collect(
                adapter.execute(
                    "kirocrew.ws.send",
                    "send-y",
                    {"tunnelId": "missing", "data": "x", "encoding": "utf8"},
                )
            )
        assert excinfo.value.status == 409

        # Close tears the upstream socket down exactly once.
        events = await collect(
            adapter.execute("kirocrew.ws.close", "close-1", {"tunnelId": "term-tunnel"})
        )
        assert events == [("request.completed", {"closed": True})]
        assert websocket.closed == [(1000, b"client closed")]
        events = await collect(
            adapter.execute("kirocrew.ws.close", "close-2", {"tunnelId": "term-tunnel"})
        )
        assert events == [("request.completed", {"closed": False})]
        events = await collect(adapter.execute("kirocrew.ws.close", "close-3", {"tunnelId": 42}))
        assert events == [("request.completed", {"closed": False})]

        remaining = [event async for event in stream]
        assert remaining[-1][0] == "request.completed"

    asyncio.run(scenario())


def test_websocket_cancellation_and_error_close_branches() -> None:
    async def scenario() -> None:
        websocket = FakeWebSocket([SimpleNamespace(type=WSMsgType.ERROR, data=None)])
        adapter = backend(FakeSession(websocket=websocket))
        stream = adapter.execute(
            "kirocrew.http",
            "active-ws",
            {"method": "GET", "path": "/api/ws", "transport": "websocket"},
        )
        assert (await anext(stream))[0] == "request.accepted"
        assert await adapter.cancel("active-ws")
        assert websocket.closed == [(1000, b"cancelled")]
        assert [event async for event in stream] == [("request.completed", {"status": 101})]

        with pytest.raises(TransientBackendError):
            await collect(
                backend(FakeSession(websocket_error=TimeoutError())).execute(
                    "kirocrew.http",
                    "failed-ws",
                    {"method": "GET", "path": "/api/ws", "transport": "websocket"},
                )
            )

    asyncio.run(scenario())


def test_websocket_empty_and_ignored_control_frames() -> None:
    async def scenario() -> None:
        empty_events = await collect(
            backend(FakeSession(websocket=FakeWebSocket([]))).execute(
                "kirocrew.http",
                "empty-ws",
                {"method": "GET", "path": "/api/ws", "transport": "websocket"},
            )
        )
        assert [operation for operation, _payload in empty_events] == [
            "request.accepted",
            "request.completed",
        ]

        websocket = FakeWebSocket(
            [
                SimpleNamespace(type=WSMsgType.PING, data=b"ping"),
                SimpleNamespace(type=WSMsgType.CLOSED, data=None),
            ]
        )
        events = await collect(
            backend(FakeSession(websocket=websocket)).execute(
                "kirocrew.http",
                "control-ws",
                {"method": "GET", "path": "/api/ws", "transport": "websocket"},
            )
        )
        assert [operation for operation, _payload in events] == [
            "request.accepted",
            "request.completed",
        ]

    asyncio.run(scenario())
