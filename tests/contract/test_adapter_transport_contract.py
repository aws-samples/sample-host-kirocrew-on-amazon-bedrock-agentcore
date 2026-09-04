from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestServer
from kirocrew_agentcore_adapter.generated_protocol import PROTOCOL_VERSION
from kirocrew_agentcore_adapter.transport import (
    AdapterConfig,
    AgentCoreAdapter,
    EchoRuntimeBackend,
    KmsBindingVerifier,
    RuntimeReadiness,
)
from kirocrew_agentcore_control.api import (
    Identity,
    LocalAsymmetricKmsSigner,
    SignedControlTokens,
)
from kirocrew_agentcore_control.sandbox import InMemorySandboxRegistry

SUBJECT = "contract-subject"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_contract"
AUDIENCE = "contract-runtime"
ULID = "01J00000000000000000000000"


class ContractLease:
    def __init__(self, expected: tuple[str, str, str]) -> None:
        self._expected = expected

    def authorize(self, subject: str, sandbox_id: str, runtime_session_id: str) -> None:
        assert (subject, sandbox_id, runtime_session_id) == self._expected


@asynccontextmanager
async def running_adapter(
    adapter: AgentCoreAdapter,
) -> AsyncIterator[tuple[ClientSession, str]]:
    server = TestServer(adapter.application)
    await server.start_server()
    session = ClientSession()
    try:
        yield session, str(server.make_url("/"))[:-1]
    finally:
        await session.close()
        await server.close()


@pytest.mark.contract
def test_agentcore_http_sse_websocket_contract_over_real_sockets() -> None:
    async def scenario() -> None:
        def clock() -> datetime:
            return datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

        signer = LocalAsymmetricKmsSigner.generate()
        registry = InMemorySandboxRegistry(
            sandbox_id_factory=lambda: "sbx_01J00000000000000000000000",
            runtime_session_id_factory=lambda: "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
            clock=clock,
        )
        record = registry.get_or_create(SUBJECT)
        token_service = SignedControlTokens(
            signer, AUDIENCE, clock=clock, nonce_factory=lambda: "contract-nonce"
        )
        binding, _ = token_service.issue_binding(Identity(SUBJECT, ISSUER, False), record)
        adapter = AgentCoreAdapter(
            AdapterConfig(AUDIENCE, ISSUER, heartbeat_timeout_seconds=1),
            KmsBindingVerifier(signer, AUDIENCE, ISSUER, clock=clock),
            ContractLease((SUBJECT, record.sandbox_id, record.runtime_session_id)),
            EchoRuntimeBackend(),
            RuntimeReadiness(restore_complete=True, loopback_ready=True),
            clock=clock,
        )
        payload = (
            base64.urlsafe_b64encode(json.dumps({"sub": SUBJECT}).encode()).rstrip(b"=").decode()
        )
        headers = {"Authorization": f"Bearer header.{payload}.signature"}
        invocation = {
            "bindingToken": binding,
            "operation": "chat.submit",
            "payload": {"text": "contract"},
            "requestId": ULID,
            "version": PROTOCOL_VERSION,
        }
        async with running_adapter(adapter) as (session, base):
            health = await session.get(f"{base}/ping")
            assert health.status == 200
            stream = await session.post(
                f"{base}/invocations",
                json=invocation,
                headers={**headers, "Accept": "text/event-stream"},
            )
            assert stream.status == 200
            assert "event: request.completed" in await stream.text()
            websocket = await session.ws_connect(f"{base}/ws", headers=headers)
            await websocket.send_json(
                {
                    "correlationId": ULID,
                    "messageId": ULID,
                    "operation": "connection.hello",
                    "payload": {"bindingToken": binding},
                    "requestId": None,
                    "sequence": 0,
                    "timestamp": "2026-08-17T16:00:00Z",
                    "version": PROTOCOL_VERSION,
                }
            )
            ready = await websocket.receive()
            assert ready.type == WSMsgType.TEXT
            assert json.loads(ready.data)["operation"] == "connection.ready"
            await websocket.close()

        source = Path("adapter/src/kirocrew_agentcore_adapter/transport.py").read_text(
            encoding="utf-8"
        )
        assert "port=8080" in source
        assert "port=5476" not in source

    asyncio.run(scenario())
