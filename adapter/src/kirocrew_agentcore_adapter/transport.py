from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Protocol, cast

from aiohttp import WSMsgType, web
from cryptography.exceptions import InvalidSignature

from kirocrew_agentcore_adapter.generated_protocol import PROTOCOL_VERSION
from kirocrew_agentcore_adapter.protocol import (
    FRAME_PAYLOAD_LIMIT,
    IdempotencyResult,
    IdempotencyWindow,
    ProtocolContractError,
    SequenceTracker,
    chunk_payload,
    sanitize_error,
    validate_envelope,
    validate_schema,
)

_LOGGER = logging.getLogger(__name__)

MAX_REQUEST_BYTES: Final = 1024 * 1024
MAX_WEBSOCKET_FRAME_BYTES: Final = 32 * 1024
EVENT_CHUNK_BYTES: Final = 16 * 1024
_ULID_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class BindingVerificationError(ValueError):
    """The signed runtime binding is invalid or stale."""


class LeaseAuthorizationError(ValueError):
    """The subject does not own the bound sandbox/session lease."""


class TransientBackendError(RuntimeError):
    """A documented transient backend failure may be retried."""


class SignatureVerifier(Protocol):
    def verify(self, message: bytes, signature: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class BindingClaims:
    sandbox_id: str
    runtime_session_id: str
    expires_at: int


class KmsBindingVerifier:
    def __init__(
        self,
        verifier: SignatureVerifier,
        audience: str,
        issuer: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not audience or not issuer:
            raise ValueError("Binding audience and issuer are required.")
        self._verifier = verifier
        self._audience = audience
        self._issuer = issuer
        self._clock = clock

    def verify(self, token: str, cognito_subject: str) -> BindingClaims:
        try:
            payload_encoded, signature_encoded = token.split(".", 1)
            payload = _unbase64url(payload_encoded)
            signature = _unbase64url(signature_encoded)
            self._verifier.verify(payload_encoded.encode(), signature)
            value = cast(object, json.loads(payload))
            if not isinstance(value, dict):
                raise ValueError
            claims = cast(dict[str, object], value)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, InvalidSignature) as error:
            raise BindingVerificationError("Binding token is invalid.") from error
        sandbox_id = claims.get("sandboxId")
        session_id = claims.get("runtimeSessionId")
        expires = claims.get("exp")
        expected_subject = hashlib.sha256(
            f"{self._issuer}\x00{cognito_subject}".encode()
        ).hexdigest()
        if (
            claims.get("type") != "binding"
            or claims.get("aud") != self._audience
            or claims.get("subjectHash") != expected_subject
            or not isinstance(sandbox_id, str)
            or not isinstance(session_id, str)
            or type(expires) is not int
            or expires <= int(self._clock().timestamp())
        ):
            raise BindingVerificationError("Binding token does not match this runtime request.")
        return BindingClaims(sandbox_id, session_id, expires)


class LeaseAuthorizer(Protocol):
    def authorize(self, cognito_subject: str, sandbox_id: str, runtime_session_id: str) -> None: ...


class RuntimeBackend(Protocol):
    def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]: ...

    async def cancel(self, request_id: str) -> bool: ...


class SessionInitializationError(RuntimeError):
    """The bound sandbox could not be restored and started safely."""


class SessionInitializer(Protocol):
    async def initialize(
        self,
        cognito_subject: str,
        binding_token: str,
        claims: BindingClaims,
    ) -> None: ...


class NoopSessionInitializer:
    async def initialize(
        self,
        cognito_subject: str,
        binding_token: str,
        claims: BindingClaims,
    ) -> None:
        del cognito_subject, binding_token, claims


@dataclass(slots=True)
class RuntimeReadiness:
    restore_complete: bool = False
    loopback_ready: bool = False
    read_only: bool = False
    initializing: bool = False

    @property
    def healthy(self) -> bool:
        return self.restore_complete and self.loopback_ready and not self.read_only


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: int
    request_id: str | None
    operation: str
    payload: Mapping[str, object]
    correlation_id: str
    timestamp: str

    def envelope(self) -> dict[str, object]:
        return {
            "correlationId": self.correlation_id,
            "messageId": _new_ulid(),
            "operation": self.operation,
            "payload": dict(self.payload),
            "requestId": self.request_id,
            "sequence": self.event_id,
            "timestamp": self.timestamp,
            "version": PROTOCOL_VERSION,
        }


class EventBuffer:
    def __init__(self, max_events: int = 512) -> None:
        if max_events <= 0:
            raise ValueError("Event buffer size must be positive.")
        self._events: dict[str, deque[RuntimeEvent]] = {}
        self._next: dict[str, int] = {}
        self._max_events = max_events

    def append(
        self,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        correlation_id: str,
        timestamp: str,
    ) -> RuntimeEvent:
        sequence = self._next.get(request_id, 0)
        event = RuntimeEvent(
            sequence, request_id, operation, dict(payload), correlation_id, timestamp
        )
        self._next[request_id] = sequence + 1
        self._events.setdefault(request_id, deque(maxlen=self._max_events)).append(event)
        return event

    def after(self, request_id: str, event_id: int) -> tuple[RuntimeEvent, ...]:
        return tuple(
            event for event in self._events.get(request_id, ()) if event.event_id > event_id
        )

    def all(self, request_id: str) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events.get(request_id, ()))


class EchoRuntimeBackend:
    """Deterministic backend for transport tests and isolated diagnostics."""

    def __init__(self) -> None:
        self.cancelled: set[str] = set()
        self.execute_count = 0

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        self.execute_count += 1
        if operation == "ping":
            yield "pong", {}
            return
        yield "request.accepted", {"operation": operation}
        if operation == "chat.submit":
            text = payload.get("text", "")
            yield "output.delta", {"text": str(text)}
        elif operation == "sandbox.prepare_stop":
            yield "checkpoint.committed", {"generation": 1}
        yield "request.completed", {"cancelled": request_id in self.cancelled}

    async def cancel(self, request_id: str) -> bool:
        self.cancelled.add(request_id)
        return True


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    binding_audience: str
    cognito_issuer: str
    heartbeat_timeout_seconds: float = 40.0
    max_backend_attempts: int = 3

    def __post_init__(self) -> None:
        if (
            not self.binding_audience
            or not self.cognito_issuer
            or self.heartbeat_timeout_seconds <= 0
            or self.max_backend_attempts <= 0
        ):
            raise ValueError("Adapter runtime configuration is invalid.")


class AgentCoreAdapter:
    def __init__(
        self,
        config: AdapterConfig,
        binding_verifier: KmsBindingVerifier,
        lease_authorizer: LeaseAuthorizer,
        backend: RuntimeBackend,
        readiness: RuntimeReadiness,
        *,
        session_initializer: SessionInitializer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[float], float] = lambda cap: secrets.randbelow(1_000_001)
        / 1_000_000
        * cap,
    ) -> None:
        self._config = config
        self._binding_verifier = binding_verifier
        self._lease_authorizer = lease_authorizer
        self._backend = backend
        self._readiness = readiness
        self._session_initializer = session_initializer or NoopSessionInitializer()
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self._idempotency = IdempotencyWindow()
        self._events = EventBuffer()
        self._application = web.Application(client_max_size=MAX_REQUEST_BYTES * 2)
        self._application.add_routes(
            [
                web.get("/ping", self._ping),
                web.post("/invocations", self._invocations),
                web.get("/invocations", self._websocket),
                web.get("/ws", self._websocket),
            ]
        )

    @property
    def application(self) -> web.Application:
        return self._application

    async def _ping(self, _request: web.Request) -> web.Response:
        """Answer the AgentCore health contract.

        The platform interprets any non-200 as an unhealthy compute and
        replaces it - even while a legitimate initialization is running -
        and it suspends the microVM between invocations unless the status
        is HealthyBusy. A long restore or gateway startup therefore must
        report HealthyBusy, never an error.

        Beyond initialization, HealthyBusy is driven by the backend's
        background-activity capability (an optional async ``background_busy``
        method): a sandbox whose task runner, subagents, or workflows are
        still working stays alive past the idle timeout even with no browser
        connected, and returns to Healthy - and normal idle reclaim - once
        the work finishes.
        """
        busy = self._readiness.initializing
        if not busy:
            probe = getattr(self._backend, "background_busy", None)
            if probe is not None:
                busy = bool(await probe())
        return web.json_response(
            {"status": "HealthyBusy" if busy else "Healthy"},
            status=200,
        )

    async def _invocations(self, request: web.Request) -> web.StreamResponse:
        correlation_id = _correlation_id(request.headers.get("x-correlation-id"))
        try:
            raw = await request.read()
            if len(raw) > MAX_REQUEST_BYTES:
                raise AdapterRequestError(
                    413, "FRAME_TOO_LARGE", "TRANSPORT", "Invocation payload is too large."
                )
            try:
                value = cast(object, json.loads(raw))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise AdapterRequestError(
                    400, "INVALID_MESSAGE", "TRANSPORT", "Invocation payload is invalid."
                ) from error
            validate_schema("http-invocation.schema.json", value)
            invocation = cast(dict[str, object], value)
            binding_token = cast(str, invocation["bindingToken"])
            subject, binding = self._authorize(request, binding_token)
            await self._session_initializer.initialize(subject, binding_token, binding)
            self._assert_ready()
            request_id = cast(str, invocation["requestId"])
            operation = cast(str, invocation["operation"])
            payload = cast(dict[str, object], invocation["payload"])
            disposition = self._idempotency.check(request_id, invocation)
            last_event_id = self._last_event_id(request)
            accepts_sse = "text/event-stream" in request.headers.get("accept", "")
            if disposition is IdempotencyResult.NEW:
                if operation == "chat.cancel":
                    target = payload.get("requestId")
                    if not isinstance(target, str):
                        raise AdapterRequestError(
                            400,
                            "INVALID_MESSAGE",
                            "TRANSPORT",
                            "Cancellation request ID is required.",
                        )
                    cancelled = await self._backend.cancel(target)
                    self._append(
                        request_id,
                        "request.completed",
                        {"cancelled": cancelled, "targetRequestId": target},
                        correlation_id,
                    )
                elif accepts_sse:
                    # Stream events to the client AS the backend produces them.
                    # Draining the generator into the buffer first would hold
                    # the response headers until the request finishes — which
                    # buffers chat token streaming and never completes for the
                    # long-lived upstream WebSocket tunnel.
                    del subject, binding
                    return await self._stream_live(
                        request, operation, request_id, payload, correlation_id, last_event_id
                    )
                else:
                    await self._execute(operation, request_id, payload, correlation_id)
            del subject, binding
            events = self._events.after(request_id, last_event_id)
            if accepts_sse:
                return await self._sse(request, events)
            return web.json_response({"events": [event.envelope() for event in events]})
        except ProtocolContractError as error:
            return self._error_response(400, error.code, "TRANSPORT", str(error), correlation_id)
        except AdapterRequestError as error:
            return self._error_response(
                error.status,
                error.code,
                error.category,
                error.message,
                correlation_id,
                retryable=error.retryable,
            )
        except (BindingVerificationError, LeaseAuthorizationError):
            return self._error_response(
                403,
                "BINDING_MISMATCH",
                "BINDING",
                "Runtime binding is invalid.",
                correlation_id,
            )
        except SessionInitializationError as error:
            # The client sees an opaque envelope; the log carries the cause.
            _LOGGER.warning("Invocation rejected during initialization: %s", error)
            return self._error_response(
                503,
                "PERSISTENCE_RESTORE_FAILED",
                "PERSISTENCE",
                "The authoritative sandbox checkpoint could not be restored.",
                correlation_id,
            )
        except Exception:
            _LOGGER.exception("Invocation failed with an unhandled error.")
            return self._error_response(
                500,
                "INTERNAL_ERROR",
                "INTERNAL",
                "The request could not be completed.",
                correlation_id,
            )

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        websocket = web.WebSocketResponse(max_msg_size=MAX_REQUEST_BYTES)
        await websocket.prepare(request)
        correlation_id = _correlation_id(request.headers.get("x-correlation-id"))
        sequence = SequenceTracker()
        authorized = False
        try:
            while True:
                try:
                    message = await websocket.receive(
                        timeout=self._config.heartbeat_timeout_seconds
                    )
                except TimeoutError:
                    await websocket.close(code=1001, message=b"heartbeat timeout")
                    break
                if message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                    break
                if message.type != WSMsgType.TEXT:
                    await self._ws_error(
                        websocket,
                        "INVALID_MESSAGE",
                        "TRANSPORT",
                        "Text frames are required.",
                        correlation_id,
                    )
                    continue
                if len(message.data.encode()) > MAX_WEBSOCKET_FRAME_BYTES:
                    await self._ws_error(
                        websocket,
                        "FRAME_TOO_LARGE",
                        "TRANSPORT",
                        "WebSocket frame is too large.",
                        correlation_id,
                    )
                    continue
                try:
                    raw = cast(object, json.loads(message.data))
                    envelope = validate_envelope(raw)
                    sequence.accept("connection", envelope["sequence"])
                    request_id = envelope.get("requestId")
                    operation = envelope["operation"]
                    payload = envelope["payload"]
                    if not authorized:
                        if operation != "connection.hello":
                            raise BindingVerificationError("Connection hello is required.")
                        binding_token = payload.get("bindingToken")
                        if not isinstance(binding_token, str):
                            raise BindingVerificationError("Binding token is required.")
                        subject, binding = self._authorize(request, binding_token)
                        await self._session_initializer.initialize(subject, binding_token, binding)
                        self._assert_ready()
                        authorized = True
                        await self._send_ws_event(
                            websocket,
                            RuntimeEvent(
                                0,
                                None,
                                "connection.ready",
                                {},
                                envelope["correlationId"],
                                self._timestamp(),
                            ),
                        )
                        continue
                    if operation == "ping":
                        await self._send_ws_event(
                            websocket,
                            RuntimeEvent(
                                envelope["sequence"],
                                request_id,
                                "pong",
                                {},
                                envelope["correlationId"],
                                self._timestamp(),
                            ),
                        )
                        continue
                    if request_id is None:
                        raise ProtocolContractError(
                            "INVALID_MESSAGE", "Request ID is required for commands."
                        )
                    command_payload = {
                        key: value for key, value in payload.items() if key != "lastEventId"
                    }
                    invocation = {
                        "operation": operation,
                        "payload": command_payload,
                        "requestId": request_id,
                    }
                    disposition = self._idempotency.check(request_id, invocation)
                    if disposition is IdempotencyResult.NEW:
                        if operation == "chat.cancel":
                            target = payload.get("requestId")
                            if not isinstance(target, str):
                                raise ProtocolContractError(
                                    "INVALID_MESSAGE", "Cancellation request ID is required."
                                )
                            cancelled = await self._backend.cancel(target)
                            self._append(
                                request_id,
                                "request.completed",
                                {"cancelled": cancelled, "targetRequestId": target},
                                envelope["correlationId"],
                            )
                        else:
                            await self._execute(
                                operation,
                                request_id,
                                command_payload,
                                envelope["correlationId"],
                            )
                    last_event_id = payload.get("lastEventId", -1)
                    if type(last_event_id) is not int:
                        raise ProtocolContractError(
                            "INVALID_MESSAGE", "Last event ID must be an integer."
                        )
                    for event in self._events.after(request_id, last_event_id):
                        await self._send_ws_event(websocket, event)
                except ProtocolContractError as error:
                    await self._ws_error(
                        websocket, error.code, "TRANSPORT", str(error), correlation_id
                    )
                except (BindingVerificationError, LeaseAuthorizationError):
                    await self._ws_error(
                        websocket,
                        "BINDING_MISMATCH",
                        "BINDING",
                        "Runtime binding is invalid.",
                        correlation_id,
                    )
                    await websocket.close(code=1008)
                    break
                except SessionInitializationError:
                    await self._ws_error(
                        websocket,
                        "PERSISTENCE_RESTORE_FAILED",
                        "PERSISTENCE",
                        "The authoritative sandbox checkpoint could not be restored.",
                        correlation_id,
                    )
                    await websocket.close(code=1011)
                    break
                except AdapterRequestError as error:
                    await self._ws_error(
                        websocket,
                        error.code,
                        error.category,
                        error.message,
                        correlation_id,
                    )
                    await websocket.close(code=1013 if error.retryable else 1008)
                    break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await self._ws_error(
                        websocket,
                        "INVALID_MESSAGE",
                        "TRANSPORT",
                        "WebSocket message is invalid.",
                        correlation_id,
                    )
        finally:
            await websocket.close()
        return websocket

    def _assert_ready(self) -> None:
        if not self._readiness.healthy:
            raise AdapterRequestError(
                503,
                "KIROCREW_UNAVAILABLE",
                "KIROCREW",
                "The sandbox is not ready.",
                retryable=True,
            )

    def _authorize(self, request: web.Request, binding_token: str) -> tuple[str, BindingClaims]:
        subject = _cognito_subject(request.headers.get("authorization"))
        claims = self._binding_verifier.verify(binding_token, subject)
        self._lease_authorizer.authorize(subject, claims.sandbox_id, claims.runtime_session_id)
        return subject, claims

    async def _execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
        correlation_id: str,
    ) -> None:
        attempt = 0
        while True:
            try:
                async for event_operation, event_payload in self._backend.execute(
                    operation, request_id, payload
                ):
                    self._append(
                        request_id,
                        event_operation,
                        event_payload,
                        correlation_id,
                    )
                return
            except TransientBackendError as error:
                attempt += 1
                if attempt == self._config.max_backend_attempts:
                    raise AdapterRequestError(
                        503,
                        "KIROCREW_UNAVAILABLE",
                        "KIROCREW",
                        "The runtime backend is temporarily unavailable.",
                        retryable=True,
                    ) from error
                await self._sleep(self._jitter(min(0.1 * (2 ** (attempt - 1)), 1.0)))

    def _append(
        self,
        request_id: str,
        operation: str,
        payload: Mapping[str, object],
        correlation_id: str,
    ) -> None:
        encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode()
        chunks = chunk_payload(encoded, EVENT_CHUNK_BYTES)
        if len(chunks) == 1:
            self._events.append(request_id, operation, payload, correlation_id, self._timestamp())
            return
        for chunk in chunks:
            self._events.append(
                request_id,
                operation,
                {
                    "chunkData": base64.b64encode(chunk.data).decode(),
                    "chunkIndex": chunk.index,
                    "chunkTotal": chunk.total,
                },
                correlation_id,
                self._timestamp(),
            )

    async def _sse(
        self, request: web.Request, events: tuple[RuntimeEvent, ...]
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "text/event-stream",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        for event in events:
            await self._write_sse(response, event)
        await response.write_eof()
        return response

    @staticmethod
    async def _write_sse(response: web.StreamResponse, event: RuntimeEvent) -> None:
        data = json.dumps(event.envelope(), sort_keys=True, separators=(",", ":"))
        await response.write(
            f"id: {event.event_id}\nevent: {event.operation}\ndata: {data}\n\n".encode()
        )

    async def _stream_live(
        self,
        request: web.Request,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
        correlation_id: str,
        last_event_id: int,
    ) -> web.StreamResponse:
        response = web.StreamResponse(
            status=200,
            headers={
                "Cache-Control": "no-cache",
                "Content-Type": "text/event-stream",
                "X-Accel-Buffering": "no",
            },
        )
        await response.prepare(request)
        cursor = last_event_id
        attempt = 0
        try:
            while True:
                try:
                    async for event_operation, event_payload in self._backend.execute(
                        operation, request_id, payload
                    ):
                        self._append(request_id, event_operation, event_payload, correlation_id)
                        for event in self._events.after(request_id, cursor):
                            await self._write_sse(response, event)
                            cursor = event.event_id
                    break
                except TransientBackendError as error:
                    attempt += 1
                    if cursor > last_event_id or attempt == self._config.max_backend_attempts:
                        # Bytes already sent or retries exhausted: report the
                        # failure in-band so the client sees a typed error
                        # instead of a silently truncated stream.
                        self._append(
                            request_id,
                            "error",
                            self._error_value(
                                "KIROCREW_UNAVAILABLE",
                                "KIROCREW",
                                "The runtime backend is temporarily unavailable.",
                                correlation_id,
                                retryable=True,
                            ),
                            correlation_id,
                        )
                        for event in self._events.after(request_id, cursor):
                            await self._write_sse(response, event)
                            cursor = event.event_id
                        del error
                        break
                    await self._sleep(self._jitter(min(0.1 * (2 ** (attempt - 1)), 1.0)))
        except (ConnectionResetError, asyncio.CancelledError):
            # Client went away: stop relaying; the loopback generator's
            # finally-block releases its upstream connection.
            return response
        await response.write_eof()
        return response

    async def _send_ws_event(self, websocket: web.WebSocketResponse, event: RuntimeEvent) -> None:
        encoded = json.dumps(event.envelope(), sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > FRAME_PAYLOAD_LIMIT:
            raise ProtocolContractError(
                "FRAME_TOO_LARGE", "Encoded event exceeds the WebSocket frame limit."
            )
        await websocket.send_str(encoded)

    async def _ws_error(
        self,
        websocket: web.WebSocketResponse,
        code: str,
        category: str,
        message: str,
        correlation_id: str,
    ) -> None:
        error = self._error_value(code, category, message, correlation_id)
        event = RuntimeEvent(0, None, "error", error, correlation_id, self._timestamp())
        await self._send_ws_event(websocket, event)

    def _last_event_id(self, request: web.Request) -> int:
        value = request.headers.get("Last-Event-ID", "-1")
        try:
            event_id = int(value)
        except ValueError as error:
            raise AdapterRequestError(
                400, "INVALID_MESSAGE", "TRANSPORT", "Last-Event-ID is invalid."
            ) from error
        if event_id < -1:
            raise AdapterRequestError(
                400, "INVALID_MESSAGE", "TRANSPORT", "Last-Event-ID is invalid."
            )
        return event_id

    def _timestamp(self) -> str:
        return self._clock().isoformat().replace("+00:00", "Z")

    def _error_response(
        self,
        status: int,
        code: str,
        category: str,
        message: str,
        correlation_id: str,
        *,
        retryable: bool = False,
    ) -> web.Response:
        return web.json_response(
            self._error_value(
                code,
                category,
                message,
                correlation_id,
                retryable=retryable,
            ),
            status=status,
        )

    @staticmethod
    def _error_value(
        code: str,
        category: str,
        message: str,
        correlation_id: str,
        *,
        retryable: bool = False,
    ) -> dict[str, object]:
        return dict(
            sanitize_error(
                {
                    "category": category,
                    "code": code,
                    "correlationId": correlation_id,
                    "message": message,
                    "retryable": retryable,
                }
            )
        )


class AdapterRequestError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        category: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.category = category
        self.message = message
        self.retryable = retryable


def run_adapter(adapter: AgentCoreAdapter) -> None:
    # AgentCore container ingress is public; only the separate KiroCrew port is loopback-only.
    web.run_app(
        adapter.application,
        host="0.0.0.0",  # noqa: S104  # nosec B104
        port=8080,
    )


def _new_ulid() -> str:
    value = int.from_bytes(secrets.token_bytes(16))
    return "".join(_ULID_ALPHABET[(value >> (5 * shift)) & 31] for shift in range(25, -1, -1))


def _correlation_id(value: str | None) -> str:
    if (
        value is not None
        and len(value) == 26
        and all(character in _ULID_ALPHABET for character in value)
    ):
        return value
    return _new_ulid()


def _cognito_subject(authorization: str | None) -> str:
    if authorization is None:
        raise BindingVerificationError("Forwarded Cognito authorization is missing.")
    scheme, separator, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not separator or not token or " " in token:
        raise BindingVerificationError("Forwarded Cognito authorization is invalid.")
    segments = token.split(".")
    if len(segments) != 3:
        raise BindingVerificationError("Forwarded Cognito authorization is invalid.")
    try:
        payload = json.loads(_unbase64url(segments[1]))
    except (TypeError, ValueError) as error:
        raise BindingVerificationError("Forwarded Cognito authorization is invalid.") from error
    if not isinstance(payload, dict):
        raise BindingVerificationError("Forwarded Cognito authorization is invalid.")
    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise BindingVerificationError("Forwarded Cognito subject is missing.")
    return subject


def _unbase64url(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
