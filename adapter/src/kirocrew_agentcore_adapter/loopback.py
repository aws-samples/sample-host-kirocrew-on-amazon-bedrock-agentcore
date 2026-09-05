from __future__ import annotations

import base64
import binascii
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Final, Protocol, cast
from urllib.parse import urlsplit

from aiohttp import (
    ClientConnectionError,
    ClientResponse,
    ClientSession,
    ClientTimeout,
    ClientWebSocketResponse,
    ClientWSTimeout,
    WSMsgType,
    WSServerHandshakeError,
)

from kirocrew_agentcore_adapter.transport import (
    AdapterRequestError,
    TransientBackendError,
)

_LOOPBACK_ORIGIN: Final = "http://127.0.0.1:5476"
_LOOPBACK_PORT: Final = 5476
_MAX_LOOPBACK_BODY_BYTES: Final = 1024 * 1024
_MAX_LOOPBACK_RESPONSE_BYTES: Final = 8 * 1024 * 1024
# A 503 error envelope is a short JSON body; cap the classification read well
# below the streaming limit so a mislabelled stream can never be buffered whole.
_MAX_LOOPBACK_ERROR_BODY_BYTES: Final = 64 * 1024
_RESPONSE_CHUNK_BYTES: Final = 16 * 1024
_SAFE_REQUEST_HEADERS: Final = frozenset(
    {"accept", "content-type", "if-match", "if-none-match", "range"}
)
_SAFE_RESPONSE_HEADERS: Final = frozenset(
    {"cache-control", "content-range", "content-type", "etag", "last-modified"}
)


class TokenProvider(Protocol):
    def token(self, *, renewal_window_seconds: float = 300.0) -> str: ...


class RouteDisposition(Enum):
    ALLOWED = "allowed"
    SYNTHETIC = "synthetic"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"


@dataclass(frozen=True, slots=True)
class RouteRule:
    methods: frozenset[str]
    path: str
    prefix: bool = True

    def matches(self, method: str, path: str) -> bool:
        path_matches = path == self.path or (self.prefix and path.startswith(f"{self.path}/"))
        return method in self.methods and path_matches


class KiroCrewRoutePolicy:
    """Tunnel every upstream KiroCrew route except a small, justified deny set.

    The sandbox is a single-tenant microVM. AgentCore validates the Cognito
    authorization header and the transport verifies a binding token against the
    Cognito subject hash on every invocation, so tenancy is enforced there — not
    by route filtering. `/api/chat` and `/api/terminal` already grant arbitrary
    in-sandbox execution, so filtering feature routes buys no isolation while
    breaking most of the product. Upstream 0.3.0 registers hundreds of `/api/*` routes
    and its browser bundle references 308 route families; an enumerated
    allowlist cannot track that surface, and every gap shows up as a 403 on a
    working feature.

    So the default disposition is ALLOWED, and only two things stay blocked:
    routes that hand out the gateway's own credential, and routes that seize
    gateway lifecycle from the supervisor. Neither is reachable from the browser
    bundle, so denying them removes no functionality.
    """

    VERSION: Final = "0.3.0"
    _SYNTHETIC: Final = (
        RouteRule(frozenset({"GET"}), "/api/auth/status", False),
        RouteRule(frozenset({"GET"}), "/api/auth/local-token", False),
        RouteRule(frozenset({"POST"}), "/api/auth/refresh", False),
        RouteRule(frozenset({"POST"}), "/api/auth/logout", False),
    )
    # Upstream does not register these families at all, so answering 501
    # "unavailable here" is both truthful and friendlier than a bare 404. They
    # are host-native by nature and can never work inside a microVM.
    _UNAVAILABLE_PREFIXES: Final = (
        "/api/desktop",
        "/api/files/pick",
    )
    _DENIED_PREFIXES: Final = (
        # Upstream registers `GET /api/token/local`, which mints a dashboard
        # session token for any same-host caller that can read the local secret.
        # The supervisor already holds that token and puts it on every tunneled
        # request; the browser must never receive it (upstream contract pins
        # browserVisibleKiroCrewToken == false). The adapter also strips
        # `X-Local-Secret` from forwarded headers, so this is defence in depth.
        "/api/token",
        # Not registered upstream today; kept so a future rename of the token
        # family cannot silently become reachable.
        "/api/auth/token",
        "/api/secrets",
        "/api/config/export",
        # Upstream registers `POST /api/shutdown`. The supervisor owns gateway
        # lifecycle: a browser-initiated exit is indistinguishable from a crash
        # and races checkpoint/restore.
        "/api/shutdown",
        # Upstream 0.3.0 adds `POST /api/restart` (and the dev-fleet app's
        # `/api/restart-gateway`): the same lifecycle seizure as /api/shutdown
        # with a reconnect race on top.
        "/api/restart",
    )

    def classify(self, method: str, path_with_query: str) -> RouteDisposition:
        method = method.upper()
        parsed = urlsplit(path_with_query)
        path = parsed.path
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not path.startswith("/api/")
            or "//" in path
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            return RouteDisposition.DENIED
        if any(path == prefix or path.startswith(f"{prefix}/") for prefix in self._DENIED_PREFIXES):
            return RouteDisposition.DENIED
        if any(
            path == prefix or path.startswith(f"{prefix}/") for prefix in self._UNAVAILABLE_PREFIXES
        ):
            return RouteDisposition.UNAVAILABLE
        if any(rule.matches(method, path) for rule in self._SYNTHETIC):
            return RouteDisposition.SYNTHETIC
        return RouteDisposition.ALLOWED


@dataclass(frozen=True, slots=True)
class LoopbackRequest:
    method: str
    path: str
    transport: str
    headers: Mapping[str, str]
    body: bytes
    frames: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> LoopbackRequest:
        method = payload.get("method", "GET")
        path = payload.get("path")
        transport = payload.get("transport", "http")
        headers_value = payload.get("headers", {})
        body_value = payload.get("body", "")
        frames_value = payload.get("frames", [])
        if (
            not isinstance(method, str)
            or not isinstance(path, str)
            or not isinstance(transport, str)
            or transport not in {"http", "sse", "websocket"}
            or not isinstance(headers_value, dict)
            or not isinstance(body_value, str)
            or not isinstance(frames_value, list)
        ):
            raise _invalid_request()
        headers: dict[str, str] = {}
        for key, value in cast(dict[object, object], headers_value).items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise _invalid_request()
            if key.lower() in _SAFE_REQUEST_HEADERS:
                headers[key.lower()] = value
        if not all(isinstance(frame, str) for frame in frames_value):
            raise _invalid_request()
        encoding = payload.get("bodyEncoding", "utf8")
        try:
            if encoding == "base64":
                body = base64.b64decode(body_value, validate=True)
            elif encoding == "utf8":
                body = body_value.encode()
            else:
                raise ValueError
        except (ValueError, UnicodeEncodeError) as error:
            raise _invalid_request() from error
        if len(body) > _MAX_LOOPBACK_BODY_BYTES:
            raise AdapterRequestError(
                413, "FRAME_TOO_LARGE", "TRANSPORT", "Loopback request body is too large."
            )
        return cls(
            method.upper(),
            path,
            transport,
            headers,
            body,
            tuple(cast(list[str], frames_value)),
        )


class LoopbackKiroCrewBackend:
    def __init__(
        self,
        session: ClientSession,
        token_provider: TokenProvider,
        *,
        policy: KiroCrewRoutePolicy | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Loopback timeout must be positive.")
        self._session = session
        self._token_provider = token_provider
        self._policy = policy or KiroCrewRoutePolicy()
        self._timeout = ClientTimeout(total=timeout_seconds)
        # Server-sent event streams (`/api/stream`, chat deltas) stay open for as
        # long as the dashboard is; a total deadline would cut every one of them
        # off mid-stream and surface as a spurious KIROCREW_UNAVAILABLE. Bound
        # only the connect phase; the upstream close or the browser aborting the
        # invocation ends the stream.
        self._stream_timeout = ClientTimeout(total=None, sock_connect=timeout_seconds)
        self._websocket_timeout = ClientWSTimeout(ws_receive=timeout_seconds)
        self._cancellations: dict[str, Callable[[], Awaitable[None]]] = {}
        self._tunnels: dict[str, ClientWebSocketResponse] = {}

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        if operation == "kirocrew.ws.send":
            await self._tunnel_send(payload)
            yield "request.completed", {"delivered": True}
            return
        if operation == "kirocrew.ws.close":
            tunnel = payload.get("tunnelId")
            closed = False
            if isinstance(tunnel, str):
                websocket = self._tunnels.pop(tunnel, None)
                if websocket is not None:
                    await websocket.close(code=1000, message=b"client closed")
                    closed = True
            yield "request.completed", {"closed": closed}
            return
        if operation != "kirocrew.http":
            raise AdapterRequestError(
                400,
                "INVALID_MESSAGE",
                "TRANSPORT",
                "The loopback operation is unsupported.",
            )
        request = LoopbackRequest.from_payload(payload)
        disposition = self._policy.classify(request.method, request.path)
        if disposition is RouteDisposition.SYNTHETIC:
            for event in _synthetic_response(request.path):
                yield event
            return
        if disposition is RouteDisposition.UNAVAILABLE:
            raise AdapterRequestError(
                501,
                "FEATURE_UNAVAILABLE_IN_AGENTCORE",
                "KIROCREW",
                "This host-native feature is unavailable in AgentCore.",
            )
        if disposition is RouteDisposition.DENIED:
            raise AdapterRequestError(
                403,
                "AUTHORIZATION_FAILED",
                "AUTHORIZATION",
                "The upstream route is not permitted.",
            )
        headers = dict(request.headers)
        # The gateway's auth middleware accepts the dashboard token from the
        # `token` query parameter or the `mc_token_<port>` cookie. The query
        # parameter is validated against the 5-minute one-time LINK window
        # (`exp`), while the cookie is validated against the 20-hour session
        # window (`session_exp`) — so a long-lived machine-to-machine bridge
        # must authenticate with the cookie. The query parameter must NOT be
        # sent: middleware prefers it over the cookie, and once the link
        # window lapses every request would fail with "token expired".
        token = self._token_provider.token()
        headers["authorization"] = f"Bearer {token}"
        headers["cookie"] = f"mc_token_{_LOOPBACK_PORT}={token}"
        try:
            if request.transport == "websocket":
                async for event in self._websocket(request_id, request, headers):
                    yield event
            else:
                async for event in self._http(request_id, request, headers):
                    yield event
        except (TimeoutError, ClientConnectionError, WSServerHandshakeError) as error:
            raise TransientBackendError("Loopback KiroCrew is temporarily unavailable.") from error
        finally:
            self._cancellations.pop(request_id, None)

    async def fetch_json(self, path: str, *, timeout_seconds: float = 2.0) -> object:
        """Directly GET a gateway JSON endpoint for internal probes.

        This bypasses the envelope machinery: the runtime itself asks the
        gateway questions (background activity, health) that never originate
        from a browser. Authentication mirrors the tunnel: the dashboard
        token as the long-lived session cookie.
        """
        token = self._token_provider.token()
        headers = {
            "authorization": f"Bearer {token}",
            "cookie": f"mc_token_{_LOOPBACK_PORT}={token}",
        }
        try:
            async with self._session.get(
                f"{_LOOPBACK_ORIGIN}{path}",
                headers=headers,
                timeout=ClientTimeout(total=timeout_seconds),
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise TransientBackendError(f"Loopback probe answered {response.status}.")
                return cast(object, await response.json())
        except (TimeoutError, ClientConnectionError) as error:
            raise TransientBackendError("Loopback KiroCrew is temporarily unavailable.") from error

    async def cancel(self, request_id: str) -> bool:
        cancellation = self._cancellations.get(request_id)
        if cancellation is None:
            return False
        await cancellation()
        return True

    async def _http(
        self, request_id: str, request: LoopbackRequest, headers: Mapping[str, str]
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        response = await self._session.request(
            request.method,
            f"{_LOOPBACK_ORIGIN}{request.path}",
            headers=headers,
            data=request.body or None,
            timeout=self._stream_timeout if request.transport == "sse" else self._timeout,
            allow_redirects=False,
        )

        async def cancel_response() -> None:
            response.close()

        self._cancellations[request_id] = cancel_response
        # 502/504 are reverse-proxy semantics the upstream app never emits itself:
        # a gateway that is up enough to answer HTTP does not return them, so they
        # only appear while the process is still coming up. Retry those.
        if response.status in {502, 504}:
            response.close()
            raise TransientBackendError("Loopback KiroCrew returned a transient status.")
        # 503 is ambiguous: the upstream gateway returns it as a real, structured
        # answer ("Kiro CLI sign-in is required before starting a session.") on
        # every session-bearing route until the user signs in. That is the normal
        # pre-login state, not an outage, and it will never succeed on retry.
        # Escalating it to KIROCREW_UNAVAILABLE takes the whole sandbox down right
        # after it becomes ready. So a 503 that carries an application error body
        # (a JSON object with a "code") is tunnelled through unchanged; a bodyless
        # or opaque 503 keeps the transient-retry behaviour.
        if response.status == 503:
            body = await self._read_bounded_body(response)
            if not _is_application_response(body):
                response.close()
                raise TransientBackendError("Loopback KiroCrew returned a transient status.")
            safe_headers = {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in _SAFE_RESPONSE_HEADERS
            }
            yield (
                "request.accepted",
                {
                    "contentType": response.headers.get("content-type", "application/octet-stream"),
                    "headers": safe_headers,
                    "status": response.status,
                    "transport": request.transport,
                },
            )
            # An application response always carries a non-empty JSON body
            # (verified above), so the delta is unconditional.
            yield (
                "output.delta",
                {
                    "data": base64.b64encode(body).decode(),
                    "encoding": "base64",
                    "transport": request.transport,
                },
            )
            response.release()
            yield "request.completed", {"status": response.status}
            return
        safe_headers = {
            key.lower(): value
            for key, value in response.headers.items()
            if key.lower() in _SAFE_RESPONSE_HEADERS
        }
        yield (
            "request.accepted",
            {
                "contentType": response.headers.get("content-type", "application/octet-stream"),
                "headers": safe_headers,
                "status": response.status,
                "transport": request.transport,
            },
        )
        total_bytes = 0
        async for chunk in response.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
            total_bytes += len(chunk)
            if total_bytes > _MAX_LOOPBACK_RESPONSE_BYTES:
                response.close()
                raise AdapterRequestError(
                    413,
                    "FRAME_TOO_LARGE",
                    "TRANSPORT",
                    "Loopback response body is too large.",
                )
            yield (
                "output.delta",
                {
                    "data": base64.b64encode(chunk).decode(),
                    "encoding": "base64",
                    "transport": request.transport,
                },
            )
        response.release()
        yield "request.completed", {"status": response.status}

    async def _read_bounded_body(self, response: ClientResponse) -> bytes:
        """Read a small error body in full so a 503 can be classified.

        Capped well below the streaming limit: this only ever runs on a 503,
        whose body is a short JSON error envelope, never a payload stream.
        """
        buffer = bytearray()
        async for chunk in response.content.iter_chunked(_RESPONSE_CHUNK_BYTES):
            buffer.extend(chunk)
            if len(buffer) > _MAX_LOOPBACK_ERROR_BODY_BYTES:
                break
        return bytes(buffer)

    async def _tunnel_send(self, payload: Mapping[str, object]) -> None:
        tunnel = payload.get("tunnelId")
        data = payload.get("data")
        encoding = payload.get("encoding", "utf8")
        if (
            not isinstance(tunnel, str)
            or not isinstance(data, str)
            or encoding not in {"utf8", "base64"}
            or len(data) > _MAX_LOOPBACK_BODY_BYTES
        ):
            raise _invalid_request()
        websocket = self._tunnels.get(tunnel)
        if websocket is None or websocket.closed:
            raise AdapterRequestError(
                409,
                "INVALID_MESSAGE",
                "TRANSPORT",
                "The WebSocket tunnel is not open.",
            )
        if encoding == "base64":
            try:
                decoded = base64.b64decode(data, validate=True)
            except (ValueError, binascii.Error) as error:
                raise _invalid_request() from error
            await websocket.send_bytes(decoded)
        else:
            await websocket.send_str(data)

    async def _websocket(
        self, request_id: str, request: LoopbackRequest, headers: Mapping[str, str]
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        websocket = await self._session.ws_connect(
            f"ws://127.0.0.1:5476{request.path}",
            headers=headers,
            timeout=self._websocket_timeout,
            max_msg_size=_MAX_LOOPBACK_BODY_BYTES,
        )

        async def cancel_websocket() -> None:
            await websocket.close(code=1000, message=b"cancelled")

        self._cancellations[request_id] = cancel_websocket
        self._tunnels[request_id] = websocket
        yield "request.accepted", {"status": 101, "transport": "websocket"}
        for frame in request.frames:
            await websocket.send_str(frame)
        async for message in websocket:
            if message.type is WSMsgType.TEXT:
                yield (
                    "output.delta",
                    {
                        "data": message.data,
                        "encoding": "utf8",
                        "transport": "websocket",
                    },
                )
            elif message.type is WSMsgType.BINARY:
                yield (
                    "output.delta",
                    {
                        "data": base64.b64encode(message.data).decode(),
                        "encoding": "base64",
                        "transport": "websocket",
                    },
                )
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
        self._tunnels.pop(request_id, None)
        yield "request.completed", {"status": 101}


def _synthetic_response(path: str) -> tuple[tuple[str, Mapping[str, object]], ...]:
    if path == "/api/auth/logout":
        body: Mapping[str, object] = {"disconnected": True, "remoteSession": True}
    else:
        body = {
            "authenticated": True,
            "mode": "agentcore",
            "remoteSession": True,
            "token": None,
        }
    encoded = base64.b64encode(json.dumps(body, separators=(",", ":")).encode()).decode()
    return (
        (
            "request.accepted",
            {
                "contentType": "application/json",
                "headers": {"content-type": "application/json"},
                "status": 200,
                "transport": "http",
            },
        ),
        (
            "output.delta",
            {"data": encoded, "encoding": "base64", "transport": "http"},
        ),
        ("request.completed", {"status": 200}),
    )


def _is_application_response(body: bytes) -> bool:
    """Whether a 503 body is the gateway's own structured answer, not an outage.

    The upstream gateway returns 503 with a JSON error envelope carrying a
    ``code`` (for example ``kiro_prerequisite_required``) when a session-bearing
    route is hit before Kiro is signed in. That is a real answer the browser
    must see, so it is tunnelled through rather than retried and escalated.
    """
    if not body:
        return False
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("code"), str)


def _invalid_request() -> AdapterRequestError:
    return AdapterRequestError(
        400, "INVALID_MESSAGE", "TRANSPORT", "The loopback request is invalid."
    )
