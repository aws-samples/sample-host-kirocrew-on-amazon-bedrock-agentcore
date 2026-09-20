from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from aiohttp import ClientSession, WSMsgType, web
from aiohttp.test_utils import TestServer
from cryptography.exceptions import InvalidSignature
from kirocrew_agentcore_adapter.generated_protocol import PROTOCOL_VERSION
from kirocrew_agentcore_adapter.protocol import FRAME_PAYLOAD_LIMIT
from kirocrew_agentcore_adapter.transport import (
    EVENT_CHUNK_BYTES,
    MAX_REQUEST_BYTES,
    MAX_WEBSOCKET_FRAME_BYTES,
    AdapterConfig,
    AgentCoreAdapter,
    BindingClaims,
    BindingVerificationError,
    EchoRuntimeBackend,
    EventBuffer,
    KmsBindingVerifier,
    LeaseAuthorizationError,
    RuntimeBackend,
    RuntimeEvent,
    RuntimeReadiness,
    SessionInitializationError,
    SessionInitializer,
    TransientBackendError,
    _cognito_subject,
    run_adapter,
)
from kirocrew_agentcore_control.api import (
    Identity,
    LocalAsymmetricKmsSigner,
    SignedControlTokens,
)
from kirocrew_agentcore_control.sandbox import InMemorySandboxRegistry

SUBJECT = "subject-a"
OTHER_SUBJECT = "subject-b"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
AUDIENCE = "kirocrew-runtime"
CORRELATION_ID = "01J00000000000000000000000"
REQUEST_ID = "01J00000000000000000000001"
SECOND_REQUEST_ID = "01J00000000000000000000002"
THIRD_REQUEST_ID = "01J00000000000000000000004"
MESSAGE_ID = "01J00000000000000000000003"


@dataclass
class MutableClock:
    now: datetime = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeLeaseAuthorizer:
    def __init__(self, subject: str, sandbox_id: str, runtime_session_id: str) -> None:
        self.expected = (subject, sandbox_id, runtime_session_id)
        self.calls: list[tuple[str, str, str]] = []
        self.reject = False

    def authorize(
        self, cognito_subject: str, sandbox_id: str, runtime_session_id: str, binding_token: str
    ) -> None:
        assert binding_token, "the caller's binding token must reach the lease check"
        actual = (cognito_subject, sandbox_id, runtime_session_id)
        self.calls.append(actual)
        if self.reject or actual != self.expected:
            raise LeaseAuthorizationError("lease mismatch")


class FakeSessionInitializer:
    def __init__(self, *, fail: bool = False) -> None:
        self._readiness: RuntimeReadiness | None = None
        self._fail = fail
        self.calls: list[tuple[str, str, BindingClaims]] = []

    def bind(self, readiness: RuntimeReadiness) -> None:
        self._readiness = readiness

    async def initialize(
        self,
        cognito_subject: str,
        binding_token: str,
        claims: BindingClaims,
    ) -> None:
        self.calls.append((cognito_subject, binding_token, claims))
        readiness = self._readiness
        if readiness is None:
            raise AssertionError("initializer readiness was not bound")
        if self._fail:
            readiness.read_only = True
            raise SessionInitializationError("restore failed")
        readiness.restore_complete = True
        readiness.loopback_ready = True


class FlakyBackend:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0
        self.cancelled: list[str] = []

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientBackendError("temporary")
        yield "request.completed", {"ok": True}

    async def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True


class ExplodingBackend:
    def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload
        raise RuntimeError("secret backend failure")

    async def cancel(self, request_id: str) -> bool:
        del request_id
        raise RuntimeError("secret cancellation failure")


class LargeBackend:
    def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload

        async def events() -> AsyncIterator[tuple[str, Mapping[str, object]]]:
            yield "output.delta", {"text": "x" * (EVENT_CHUNK_BYTES * 2)}

        return events()

    async def cancel(self, request_id: str) -> bool:
        del request_id
        return False


class InvalidVerifier:
    def verify(self, message: bytes, signature: bytes) -> None:
        del message, signature
        raise InvalidSignature


@dataclass
class RuntimeFixture:
    adapter: AgentCoreAdapter
    binding: str
    lease: FakeLeaseAuthorizer
    backend: RuntimeBackend
    clock: MutableClock
    signer: LocalAsymmetricKmsSigner
    scheduler: str


async def no_sleep(_delay: float) -> None:
    return None


def runtime_fixture(
    *,
    healthy: bool = True,
    backend: RuntimeBackend | None = None,
    session_initializer: SessionInitializer | None = None,
    heartbeat_timeout: float = 0.1,
    max_attempts: int = 3,
    accepts_scheduler: bool = False,
) -> RuntimeFixture:
    clock = MutableClock()
    signer = LocalAsymmetricKmsSigner.generate()
    registry = InMemorySandboxRegistry(
        sandbox_id_factory=lambda: "sbx_01J00000000000000000000000",
        runtime_session_id_factory=lambda: "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
        clock=clock,
    )
    record = registry.get_or_create(SUBJECT)
    tokens = SignedControlTokens(
        signer,
        AUDIENCE,
        clock=clock,
        nonce_factory=lambda: "fixed-nonce",
    )
    binding, _ = tokens.issue_binding(Identity(SUBJECT, ISSUER, False), record)
    # Minted unconditionally so a test can present it at either door and assert the
    # difference; only the machine door is built to accept it.
    scheduler, _ = tokens.issue_scheduler(Identity(SUBJECT, ISSUER, False), record)
    lease = FakeLeaseAuthorizer(SUBJECT, record.sandbox_id, record.runtime_session_id)
    selected_backend = backend or EchoRuntimeBackend()
    readiness = RuntimeReadiness(healthy, healthy, False)
    if isinstance(session_initializer, FakeSessionInitializer):
        session_initializer.bind(readiness)
    adapter = AgentCoreAdapter(
        AdapterConfig(AUDIENCE, ISSUER, heartbeat_timeout, max_attempts),
        KmsBindingVerifier(
            signer, AUDIENCE, ISSUER, clock=clock, accepts_scheduler=accepts_scheduler
        ),
        lease,
        selected_backend,
        readiness,
        session_initializer=session_initializer,
        clock=clock,
        sleep=no_sleep,
        jitter=lambda cap: cap,
    )
    return RuntimeFixture(adapter, binding, lease, selected_backend, clock, signer, scheduler)


@asynccontextmanager
async def socket_client(adapter: AgentCoreAdapter) -> AsyncIterator[tuple[ClientSession, str]]:
    server = TestServer(adapter.application)
    await server.start_server()
    session = ClientSession()
    try:
        yield session, str(server.make_url("/"))[:-1]
    finally:
        await session.close()
        await server.close()


def invocation(
    binding: str,
    *,
    request_id: str = REQUEST_ID,
    operation: str = "chat.submit",
    payload: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "bindingToken": binding,
        "operation": operation,
        "payload": dict(payload or {"text": "hello"}),
        "requestId": request_id,
        "version": PROTOCOL_VERSION,
    }


def bearer(subject: object = SUBJECT) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode()).rstrip(b"=").decode()
    return f"Bearer {header}.{payload}.signature"


def headers(subject: object = SUBJECT, **extra: str) -> dict[str, str]:
    value = {
        "Authorization": bearer(subject),
        "X-Correlation-Id": CORRELATION_ID,
    }
    value.update(extra)
    return value


def test_forwarded_cognito_authorization_subject_parsing_fails_closed() -> None:
    assert _cognito_subject(bearer()) == SUBJECT
    invalid_authorization = (
        None,
        "",
        "Basic token",
        "Bearer ",
        "Bearer token extra",
        "Bearer one.two",
        "Bearer one.bad$.three",
        "Bearer one.W10.three",
        bearer(None),
        bearer(""),
    )
    for value in invalid_authorization:
        with pytest.raises(BindingVerificationError, match="Cognito"):
            _cognito_subject(value)


def envelope(
    operation: str,
    sequence: int,
    payload: Mapping[str, object],
    *,
    request_id: str | None = None,
) -> dict[str, object]:
    return {
        "correlationId": CORRELATION_ID,
        "messageId": MESSAGE_ID,
        "operation": operation,
        "payload": dict(payload),
        "requestId": request_id,
        "sequence": sequence,
        "timestamp": "2026-08-17T16:00:00Z",
        "version": PROTOCOL_VERSION,
    }


async def receive_json(websocket: object) -> dict[str, object]:
    message = await websocket.receive()  # type: ignore[attr-defined]
    assert message.type == WSMsgType.TEXT
    value = json.loads(message.data)
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def test_ping_readiness_and_config_validation_over_real_http() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(healthy=False)
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.get(f"{base}/ping")
            # The AgentCore contract requires 200 even while unhealthy;
            # anything else makes the platform replace the compute.
            assert response.status == 200
            assert (await response.json())["status"] == "Healthy"
            fixture.adapter._readiness.restore_complete = True
            fixture.adapter._readiness.loopback_ready = True
            response = await session.get(f"{base}/ping")
            assert response.status == 200
            fixture.adapter._readiness.read_only = True
            response = await session.get(f"{base}/ping")
            # The AgentCore contract requires 200 even while unhealthy;
            # anything else makes the platform replace the compute.
            assert response.status == 200
            assert (await response.json())["status"] == "Healthy"
            # While initialization runs, the ping reports HealthyBusy so
            # the platform keeps the microVM alive between invocations.
            fixture.adapter._readiness.initializing = True
            busy = await session.get(f"{base}/ping")
            assert busy.status == 200
            assert (await busy.json())["status"] == "HealthyBusy"
            fixture.adapter._readiness.initializing = False
            # A backend advertising background activity keeps the sandbox
            # alive past the idle timeout; a quiet one lets it reclaim.
            background = {"busy": True}

            async def background_busy() -> bool:
                return background["busy"]

            fixture.backend.background_busy = background_busy  # type: ignore[attr-defined]
            working = await session.get(f"{base}/ping")
            assert (await working.json())["status"] == "HealthyBusy"
            background["busy"] = False
            quiet = await session.get(f"{base}/ping")
            assert (await quiet.json())["status"] == "Healthy"
            # Initialization outranks the probe: it is never consulted then.
            background["busy"] = True
            fixture.adapter._readiness.initializing = True
            initializing = await session.get(f"{base}/ping")
            assert (await initializing.json())["status"] == "HealthyBusy"
            fixture.adapter._readiness.initializing = False

    asyncio.run(scenario())
    with pytest.raises(ValueError, match="configuration"):
        AdapterConfig("", ISSUER)
    with pytest.raises(ValueError, match="buffer"):
        EventBuffer(0)


def test_http_json_exactly_once_conflict_and_reconnect_replay() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture()
        backend = cast(EchoRuntimeBackend, fixture.backend)
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(),
            )
            assert response.status == 200
            events = (await response.json())["events"]
            assert [event["sequence"] for event in events] == [0, 1, 2]
            assert [event["operation"] for event in events] == [
                "request.accepted",
                "output.delta",
                "request.completed",
            ]
            assert backend.execute_count == 1
            duplicate = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(**{"Last-Event-ID": "0"}),
            )
            duplicate_events = (await duplicate.json())["events"]
            assert [event["sequence"] for event in duplicate_events] == [1, 2]
            assert backend.execute_count == 1
            changed = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding, payload={"text": "changed"}),
                headers=headers(),
            )
            assert changed.status == 400
            assert (await changed.json())["code"] == "IDEMPOTENCY_CONFLICT"

    asyncio.run(scenario())


def test_sse_ordering_ids_and_cancellation_over_real_http() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture()
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(Accept="text/event-stream"),
            )
            assert response.status == 200
            body = await response.text()
            assert body.index("id: 0") < body.index("id: 1") < body.index("id: 2")
            assert "event: output.delta" in body
            cancel = await session.post(
                f"{base}/invocations",
                json=invocation(
                    fixture.binding,
                    request_id=SECOND_REQUEST_ID,
                    operation="chat.cancel",
                    payload={"requestId": REQUEST_ID},
                ),
                headers=headers(),
            )
            assert cancel.status == 200
            cancel_events = (await cancel.json())["events"]
            assert cancel_events[0]["payload"]["cancelled"]

    asyncio.run(scenario())


def test_http_authorization_schema_size_readiness_and_last_event_errors() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture()
        async with socket_client(fixture.adapter) as (session, base):
            cases = [
                ({}, invocation(fixture.binding), 403, "BINDING_MISMATCH"),
                (headers(OTHER_SUBJECT), invocation(fixture.binding), 403, "BINDING_MISMATCH"),
                (
                    headers(),
                    {**invocation(fixture.binding), "version": "v0"},
                    400,
                    "UNSUPPORTED_PROTOCOL",
                ),
            ]
            for request_headers, body, status, code in cases:
                response = await session.post(
                    f"{base}/invocations", json=body, headers=request_headers
                )
                assert response.status == status
                assert (await response.json())["code"] == code
            fixture.lease.reject = True
            rejected = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding, request_id=SECOND_REQUEST_ID),
                headers=headers(),
            )
            assert rejected.status == 403
            fixture.lease.reject = False
            malformed = await session.post(
                f"{base}/invocations", data=b"not-json", headers=headers()
            )
            assert malformed.status == 400
            invalid_last = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding, request_id=SECOND_REQUEST_ID),
                headers=headers(**{"Last-Event-ID": "bad"}),
            )
            assert invalid_last.status == 400
            negative_last = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding, request_id=SECOND_REQUEST_ID),
                headers=headers(**{"Last-Event-ID": "-2"}),
            )
            assert negative_last.status == 400
            oversized = await session.post(
                f"{base}/invocations",
                data=b"{" + b"x" * MAX_REQUEST_BYTES + b"}",
                headers=headers(),
            )
            assert oversized.status == 413

        unhealthy = runtime_fixture(healthy=False)
        async with socket_client(unhealthy.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(unhealthy.binding),
                headers=headers(),
            )
            assert response.status == 503
            assert (await response.json())["retryable"]

    asyncio.run(scenario())


class EndlessBackend:
    """Streams forever — models the long-lived upstream WebSocket tunnel."""

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload
        yield "request.accepted", {"status": 101}
        index = 0
        while True:
            yield "output.delta", {"data": f"tick-{index}", "encoding": "utf8"}
            index += 1
            await asyncio.sleep(0)

    async def cancel(self, request_id: str) -> bool:
        del request_id
        return False


def test_sse_live_stream_stops_when_the_client_disconnects() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(backend=EndlessBackend())
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(Accept="text/event-stream"),
            )
            assert response.status == 200
            chunk = await response.content.read(64)
            assert b"request.accepted" in chunk or b"id: 0" in chunk
            response.close()
        # Give the server loop a beat to observe the reset and bail out.
        await asyncio.sleep(0.1)

    asyncio.run(scenario())


class MidStreamFlakyBackend:
    """Yields one event, then fails transiently — a broken live stream."""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload
        self.calls += 1
        yield "request.accepted", {"status": 101}
        raise TransientBackendError("stream interrupted")

    async def cancel(self, request_id: str) -> bool:
        del request_id
        return False


def test_sse_live_streaming_retry_exhaustion_midstream_error_and_replay() -> None:
    async def scenario() -> None:
        # Pre-stream transient failures are retried before any byte is sent.
        flaky = FlakyBackend(2)
        fixture = runtime_fixture(backend=flaky)
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(Accept="text/event-stream"),
            )
            assert response.status == 200
            body = await response.text()
            assert "event: request.completed" in body
            assert flaky.calls == 3
            # Reconnect replay of the SAME request streams from the buffer.
            replay = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(Accept="text/event-stream", **{"Last-Event-ID": "-1"}),
            )
            assert replay.status == 200
            assert "event: request.completed" in await replay.text()
            assert flaky.calls == 3

        # Exhausted retries surface an in-band typed error event.
        exhausted = runtime_fixture(backend=FlakyBackend(3))
        async with socket_client(exhausted.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(exhausted.binding),
                headers=headers(Accept="text/event-stream"),
            )
            assert response.status == 200
            body = await response.text()
            assert "event: error" in body
            assert "KIROCREW_UNAVAILABLE" in body

        # A failure after bytes were sent also ends with an in-band error.
        midstream = MidStreamFlakyBackend()
        partial = runtime_fixture(backend=midstream)
        async with socket_client(partial.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(partial.binding),
                headers=headers(Accept="text/event-stream"),
            )
            assert response.status == 200
            body = await response.text()
            assert body.index("event: request.accepted") < body.index("event: error")
            assert midstream.calls == 1

    asyncio.run(scenario())


def test_http_transient_retry_exhaustion_internal_error_and_chunking() -> None:
    async def scenario() -> None:
        flaky = FlakyBackend(2)
        fixture = runtime_fixture(backend=flaky)
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(),
            )
            assert response.status == 200
            assert flaky.calls == 3

        exhausted = runtime_fixture(backend=FlakyBackend(3))
        async with socket_client(exhausted.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(exhausted.binding),
                headers=headers(),
            )
            assert response.status == 503
            assert (await response.json())["code"] == "KIROCREW_UNAVAILABLE"

        exploding = runtime_fixture(backend=ExplodingBackend())
        async with socket_client(exploding.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(exploding.binding),
                headers=headers(),
            )
            assert response.status == 500
            assert "secret backend failure" not in await response.text()

        large = runtime_fixture(backend=LargeBackend())
        async with socket_client(large.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(large.binding),
                headers=headers(),
            )
            events = (await response.json())["events"]
            assert len(events) == 3
            assert [event["payload"]["chunkIndex"] for event in events] == [0, 1, 2]
            assert all(len(json.dumps(event).encode()) < FRAME_PAYLOAD_LIMIT for event in events)

    asyncio.run(scenario())


def test_websocket_authorization_multiplexing_ping_replay_and_cancel() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(heartbeat_timeout=1)
        async with socket_client(fixture.adapter) as (session, base):
            websocket = await session.ws_connect(
                f"{base}/ws", headers=headers(), max_msg_size=MAX_REQUEST_BYTES
            )
            await websocket.send_json(
                envelope("connection.hello", 0, {"bindingToken": fixture.binding})
            )
            ready = await receive_json(websocket)
            assert ready["operation"] == "connection.ready"
            await websocket.send_json(
                envelope(
                    "chat.submit",
                    1,
                    {"text": "hello", "lastEventId": -1},
                    request_id=REQUEST_ID,
                )
            )
            operations = [(await receive_json(websocket))["operation"] for _ in range(3)]
            assert operations == ["request.accepted", "output.delta", "request.completed"]
            await websocket.send_json(envelope("ping", 2, {}, request_id=SECOND_REQUEST_ID))
            assert (await receive_json(websocket))["operation"] == "pong"
            await websocket.send_json(
                envelope(
                    "chat.submit",
                    3,
                    {"text": "hello", "lastEventId": 1},
                    request_id=REQUEST_ID,
                )
            )
            replay = await receive_json(websocket)
            assert replay["sequence"] == 2
            await websocket.send_json(
                envelope(
                    "chat.cancel",
                    4,
                    {"requestId": REQUEST_ID},
                    request_id=SECOND_REQUEST_ID,
                )
            )
            cancelled = await receive_json(websocket)
            assert cancelled["payload"]["cancelled"]  # type: ignore[index]
            await websocket.close()

    asyncio.run(scenario())


def test_websocket_protocol_errors_oversize_binary_and_binding_close() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(heartbeat_timeout=1)
        async with socket_client(fixture.adapter) as (session, base):
            websocket = await session.ws_connect(f"{base}/ws", headers=headers())
            await websocket.send_bytes(b"binary")
            assert (await receive_json(websocket))["operation"] == "error"
            await websocket.send_str("not-json")
            assert (await receive_json(websocket))["operation"] == "error"
            await websocket.send_str("x" * (MAX_WEBSOCKET_FRAME_BYTES + 1))
            frame_error = await receive_json(websocket)
            assert frame_error["payload"]["code"] == "FRAME_TOO_LARGE"  # type: ignore[index]
            await websocket.send_json(envelope("ping", 0, {}))
            binding_error = await receive_json(websocket)
            assert binding_error["payload"]["code"] == "BINDING_MISMATCH"  # type: ignore[index]
            close = await websocket.receive()
            assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

            invalid_binding = await session.ws_connect(f"{base}/ws", headers=headers())
            await invalid_binding.send_json(
                envelope("connection.hello", 0, {"bindingToken": "invalid-token"})
            )
            assert (await receive_json(invalid_binding))["operation"] == "error"

    asyncio.run(scenario())


def test_websocket_sequence_payload_validation_conflict_and_heartbeat_timeout() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(heartbeat_timeout=0.02)
        async with socket_client(fixture.adapter) as (session, base):
            websocket = await session.ws_connect(f"{base}/ws", headers=headers())
            await websocket.send_json(
                envelope("connection.hello", 0, {"bindingToken": fixture.binding})
            )
            await receive_json(websocket)
            await websocket.send_json(
                envelope("chat.submit", 2, {"text": "gap"}, request_id=REQUEST_ID)
            )
            assert (await receive_json(websocket))["operation"] == "error"
            await websocket.send_json(
                envelope("chat.submit", 1, {"text": "ok"}, request_id=REQUEST_ID)
            )
            for _ in range(3):
                await receive_json(websocket)
            await websocket.send_json(
                envelope("chat.submit", 2, {"text": "changed"}, request_id=REQUEST_ID)
            )
            conflict = await receive_json(websocket)
            assert conflict["payload"]["code"] == "IDEMPOTENCY_CONFLICT"  # type: ignore[index]
            timeout_message = await websocket.receive()
            assert timeout_message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

    asyncio.run(scenario())


def test_binding_verifier_event_buffer_backend_and_runner_defensive_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = MutableClock()
    signer = LocalAsymmetricKmsSigner.generate()
    fixture = runtime_fixture()
    verifier = KmsBindingVerifier(fixture.signer, AUDIENCE, ISSUER, clock=fixture.clock)
    with pytest.raises(BindingVerificationError, match="invalid"):
        verifier.verify("bad", SUBJECT)
    invalid_signature = KmsBindingVerifier(InvalidVerifier(), AUDIENCE, ISSUER, clock=clock)
    with pytest.raises(BindingVerificationError, match="invalid"):
        invalid_signature.verify(fixture.binding, SUBJECT)
    with pytest.raises(BindingVerificationError, match="does not match"):
        verifier.verify(fixture.binding, OTHER_SUBJECT)
    with pytest.raises(ValueError, match="audience"):
        KmsBindingVerifier(signer, "", ISSUER)

    buffer = EventBuffer(2)
    assert buffer.all("missing") == ()
    for index in range(3):
        buffer.append(REQUEST_ID, "output.delta", {"index": index}, CORRELATION_ID, "now")
    assert [event.event_id for event in buffer.all(REQUEST_ID)] == [1, 2]
    assert [event.event_id for event in buffer.after(REQUEST_ID, 1)] == [2]

    backend = EchoRuntimeBackend()

    async def backend_scenario() -> None:
        ping = [item async for item in backend.execute("ping", REQUEST_ID, {})]
        assert ping == [("pong", {})]
        stop = [item async for item in backend.execute("sandbox.prepare_stop", REQUEST_ID, {})]
        assert stop[1][0] == "checkpoint.committed"
        assert await backend.cancel(REQUEST_ID)

    asyncio.run(backend_scenario())

    calls: list[tuple[str, int]] = []

    def fake_run_app(application: web.Application, *, host: str, port: int) -> None:
        assert application is fixture.adapter.application
        calls.append((host, port))

    monkeypatch.setattr(web, "run_app", fake_run_app)
    run_adapter(fixture.adapter)
    assert calls == [("0.0.0.0", 8080)]  # noqa: S104 - required container ingress
    source = Path("adapter/src/kirocrew_agentcore_adapter/transport.py").read_text(encoding="utf-8")
    assert "port=5476" not in source


def test_runtime_event_envelope_and_error_frame_limit() -> None:
    event_value = RuntimeEvent(
        1,
        REQUEST_ID,
        "output.delta",
        {"text": "ok"},
        CORRELATION_ID,
        "2026-08-17T16:00:00Z",
    ).envelope()
    assert event_value["version"] == PROTOCOL_VERSION
    assert event_value["sequence"] == 1

    async def scenario() -> None:
        fixture = runtime_fixture()
        websocket = web.WebSocketResponse()
        oversized = RuntimeEvent(
            0,
            REQUEST_ID,
            "output.delta",
            {"text": "x" * FRAME_PAYLOAD_LIMIT},
            CORRELATION_ID,
            "2026-08-17T16:00:00Z",
        )
        with pytest.raises(Exception, match="frame limit"):
            await fixture.adapter._send_ws_event(websocket, oversized)

    asyncio.run(scenario())


def test_remaining_transport_validation_branches() -> None:
    async def scenario() -> None:
        fixture = runtime_fixture(heartbeat_timeout=1)
        payload = base64.urlsafe_b64encode(b"[]").rstrip(b"=").decode()
        signature = (
            base64.urlsafe_b64encode(fixture.signer.sign(payload.encode())).rstrip(b"=").decode()
        )
        verifier = KmsBindingVerifier(fixture.signer, AUDIENCE, ISSUER, clock=fixture.clock)
        with pytest.raises(BindingVerificationError, match="invalid"):
            verifier.verify(f"{payload}.{signature}", SUBJECT)

        backend = cast(EchoRuntimeBackend, fixture.backend)
        generic = [item async for item in backend.execute("kiro.login.start", REQUEST_ID, {})]
        assert [item[0] for item in generic] == ["request.accepted", "request.completed"]

        async with socket_client(fixture.adapter) as (session, base):
            missing_cancel = await session.post(
                f"{base}/invocations",
                json=invocation(
                    fixture.binding,
                    operation="chat.cancel",
                    payload={},
                ),
                headers=headers(),
            )
            assert missing_cancel.status == 400

            missing_binding = await session.ws_connect(f"{base}/ws", headers=headers())
            await missing_binding.send_json(envelope("connection.hello", 0, {}))
            error = await receive_json(missing_binding)
            assert error["operation"] == "error"
            await missing_binding.close()

            websocket = await session.ws_connect(f"{base}/ws", headers=headers())
            await websocket.send_json(
                envelope("connection.hello", 0, {"bindingToken": fixture.binding})
            )
            await receive_json(websocket)
            await websocket.send_json(envelope("kiro.login.start", 1, {}))
            no_request = await receive_json(websocket)
            assert no_request["operation"] == "error"
            await websocket.send_json(envelope("chat.cancel", 2, {}, request_id=SECOND_REQUEST_ID))
            no_target = await receive_json(websocket)
            assert no_target["operation"] == "error"
            await websocket.send_json(
                envelope(
                    "chat.submit",
                    3,
                    {"text": "hello", "lastEventId": "bad"},
                    request_id=THIRD_REQUEST_ID,
                )
            )
            invalid_cursor = await receive_json(websocket)
            assert invalid_cursor["operation"] == "error"
            await websocket.close()

    asyncio.run(scenario())


def test_bound_session_initializes_before_http_and_websocket_readiness() -> None:
    async def scenario() -> None:
        initializer = FakeSessionInitializer()
        fixture = runtime_fixture(healthy=False, session_initializer=initializer)
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(),
            )
            assert response.status == 200
            assert len(initializer.calls) == 1
            subject, token, claims = initializer.calls[0]
            assert subject == SUBJECT
            assert token == fixture.binding
            assert claims.runtime_session_id == fixture.lease.expected[2]

        websocket_initializer = FakeSessionInitializer()
        websocket_fixture = runtime_fixture(
            healthy=False,
            session_initializer=websocket_initializer,
            heartbeat_timeout=1,
        )
        async with socket_client(websocket_fixture.adapter) as (session, base):
            websocket = await session.ws_connect(f"{base}/invocations", headers=headers())
            await websocket.send_json(
                envelope(
                    "connection.hello",
                    0,
                    {"bindingToken": websocket_fixture.binding},
                )
            )
            assert (await receive_json(websocket))["operation"] == "connection.ready"
            assert len(websocket_initializer.calls) == 1
            await websocket.close()

    asyncio.run(scenario())


def test_session_initialization_is_after_authorization_and_fails_closed() -> None:
    async def scenario() -> None:
        initializer = FakeSessionInitializer()
        fixture = runtime_fixture(healthy=False, session_initializer=initializer)
        fixture.lease.reject = True
        async with socket_client(fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(fixture.binding),
                headers=headers(),
            )
            assert response.status == 403
            assert not initializer.calls

        failing = FakeSessionInitializer(fail=True)
        failed_fixture = runtime_fixture(healthy=False, session_initializer=failing)
        async with socket_client(failed_fixture.adapter) as (session, base):
            response = await session.post(
                f"{base}/invocations",
                json=invocation(failed_fixture.binding),
                headers=headers(),
            )
            assert response.status == 503
            body = await response.json()
            assert body["code"] == "PERSISTENCE_RESTORE_FAILED"
            assert body["category"] == "PERSISTENCE"
            ping = await session.get(f"{base}/ping")
            assert ping.status == 200
            assert (await ping.json())["status"] == "Healthy"

    asyncio.run(scenario())


def test_websocket_initialization_and_readiness_fail_closed() -> None:
    async def scenario() -> None:
        failing_initializer = FakeSessionInitializer(fail=True)
        failed = runtime_fixture(
            session_initializer=failing_initializer,
            heartbeat_timeout=1,
        )
        async with socket_client(failed.adapter) as (session, base):
            websocket = await session.ws_connect(f"{base}/ws", headers=headers())
            await websocket.send_json(
                envelope("connection.hello", 0, {"bindingToken": failed.binding})
            )
            error = await receive_json(websocket)
            assert error["payload"]["code"] == "PERSISTENCE_RESTORE_FAILED"  # type: ignore[index]
            close = await websocket.receive()
            assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

        unready = runtime_fixture(healthy=False, heartbeat_timeout=1)
        async with socket_client(unready.adapter) as (session, base):
            websocket = await session.ws_connect(f"{base}/ws", headers=headers())
            await websocket.send_json(
                envelope("connection.hello", 0, {"bindingToken": unready.binding})
            )
            error = await receive_json(websocket)
            assert error["payload"]["code"] == "KIROCREW_UNAVAILABLE"  # type: ignore[index]
            close = await websocket.receive()
            assert close.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}

    asyncio.run(scenario())


def test_scheduler_token_is_refused_unless_this_runtime_is_the_machine_door() -> None:
    """The env-gated flag is the ONLY thing separating the two front doors.

    Both runtimes run the same image, so nothing in the code can tell which door
    served a request. If the browser-facing runtime accepted scheduler tokens, a
    caller holding one could skip the Cognito subject cross-check entirely -- so
    the default must be refusal, and this test is what keeps it that way.
    """
    clock = MutableClock()
    signer = LocalAsymmetricKmsSigner.generate()
    registry = InMemorySandboxRegistry(
        sandbox_id_factory=lambda: "sbx_01J00000000000000000000000",
        runtime_session_id_factory=lambda: "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
        clock=clock,
    )
    record = registry.get_or_create(SUBJECT)
    tokens = SignedControlTokens(signer, AUDIENCE, clock=clock, nonce_factory=lambda: "n")
    scheduler, _ = tokens.issue_scheduler(Identity(SUBJECT, ISSUER, False), record)
    binding, _ = tokens.issue_binding(Identity(SUBJECT, ISSUER, False), record)

    browser_door = KmsBindingVerifier(signer, AUDIENCE, ISSUER, clock=clock)
    machine_door = KmsBindingVerifier(signer, AUDIENCE, ISSUER, clock=clock, accepts_scheduler=True)
    assert browser_door.accepts_scheduler is False
    assert machine_door.accepts_scheduler is True

    # Refused on the browser door even though the signature is perfectly valid.
    with pytest.raises(BindingVerificationError, match="not accepted on this runtime"):
        browser_door.verify(scheduler, None)
    with pytest.raises(BindingVerificationError, match="not accepted on this runtime"):
        browser_door.verify(scheduler, SUBJECT)

    # Accepted on the machine door, with the claims a binding token would carry.
    claims = machine_door.verify(scheduler, None)
    assert claims.sandbox_id == record.sandbox_id
    assert claims.runtime_session_id == record.runtime_session_id

    # Presenting a subject alongside a scheduler token is a contradiction: it
    # means a browser reached the machine door, so refuse rather than guess.
    with pytest.raises(BindingVerificationError, match="not accepted on this runtime"):
        machine_door.verify(scheduler, SUBJECT)

    # The machine door must not become a way to skip the cross-check for an
    # ordinary binding token: those still require a subject, and still have to
    # match it.
    assert machine_door.verify(binding, SUBJECT).sandbox_id == record.sandbox_id
    with pytest.raises(BindingVerificationError, match="missing"):
        machine_door.verify(binding, None)
    with pytest.raises(BindingVerificationError, match="does not match"):
        machine_door.verify(binding, OTHER_SUBJECT)

    # An unknown type is refused on both doors rather than falling through, and
    # it has to be signed properly to prove the TYPE is what rejects it: a token
    # cannot be forged by editing the string, since the claims are base64 and the
    # signature covers them.
    payload = (
        base64.urlsafe_b64encode(
            json.dumps(
                {
                    "aud": AUDIENCE,
                    "exp": int(clock().timestamp()) + 600,
                    "iat": int(clock().timestamp()),
                    "sandboxId": record.sandbox_id,
                    "runtimeSessionId": record.runtime_session_id,
                    "subjectHash": Identity(SUBJECT, ISSUER, False).subject_hash,
                    "type": "runtime-session",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    signed = (
        f"{payload}.{base64.urlsafe_b64encode(signer.sign(payload.encode())).decode().rstrip('=')}"
    )
    for door in (browser_door, machine_door):
        with pytest.raises(BindingVerificationError, match="does not match"):
            door.verify(signed, SUBJECT)


def test_a_wake_with_no_authorization_header_is_served_only_at_the_machine_door() -> None:
    """An unattended wake has no user token, because nobody is signed in.

    Every other caller must present one, and a missing header has always been a hard
    failure. The machine door tolerates its absence for exactly one case -- a
    scheduler binding, which carries the sandbox and session itself and so has no
    subject to cross-check. The browser door must keep refusing, or a caller could
    present a scheduler binding there to skip the Cognito subject comparison
    entirely.
    """

    async def scenario() -> None:
        machine = runtime_fixture(accepts_scheduler=True)
        # The scheduler path deliberately reaches the lease check with an EMPTY
        # subject: the binding names the sandbox and session, and there is no signed-in
        # user to name. Told to the fake so the assertion is about that contract rather
        # than about the fixture's defaults.
        machine.lease.expected = ("", *machine.lease.expected[1:])
        async with socket_client(machine.adapter) as (session, base):
            accepted = await session.post(
                f"{base}/invocations",
                json=invocation(machine.scheduler),
            )
            assert accepted.status == 200
        assert machine.lease.calls == [("", *machine.lease.expected[1:])]

        browser = runtime_fixture()
        async with socket_client(browser.adapter) as (session, base):
            refused = await session.post(
                f"{base}/invocations",
                json=invocation(browser.scheduler),
            )
            assert refused.status == 403
            assert (await refused.json())["code"] == "BINDING_MISMATCH"
        # Refused BEFORE the lease check: the door decides on the token type alone.
        assert browser.lease.calls == []

    asyncio.run(scenario())
