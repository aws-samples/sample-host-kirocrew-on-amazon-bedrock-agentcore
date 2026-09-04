from __future__ import annotations

import asyncio
import base64
import json
import os
import signal
import time
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest
from aiohttp import ClientSession, WSMsgType, web
from kirocrew_agentcore_adapter.loopback import (
    KiroCrewRoutePolicy,
    LoopbackKiroCrewBackend,
    RouteDisposition,
)
from kirocrew_agentcore_adapter.transport import AdapterRequestError
from kirocrew_agentcore_runtime.supervisor import (
    DashboardToken,
    KiroCrewSupervisor,
    RuntimeMetadata,
    WorkspaceLayout,
)

ROOT = Path(__file__).parents[2]
ARTIFACT = json.loads((ROOT / "runtime" / "kirocrew-artifact.json").read_text())
DIGEST = "329b4b2e271d1253eb9b2115f19950485181934f2ba7ded1a993edfbf48c6f90"


class StaticToken:
    def token(self, *, renewal_window_seconds: float = 300.0) -> str:
        assert renewal_window_seconds == 300.0
        return "contract-loopback-token"


def runtime_metadata() -> RuntimeMetadata:
    return RuntimeMetadata("0.2.0", DIGEST, "kirocrew-agentcore.v1")


def process_state(pid: int) -> str | None:
    status = Path(f"/proc/{pid}/status")
    if not status.exists():
        return None
    for line in status.read_text().splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    return None


def wait_for_state(
    pid: int, predicate: Callable[[str | None], bool], timeout: float = 5.0
) -> str | None:
    deadline = time.monotonic() + timeout
    state = process_state(pid)
    while time.monotonic() < deadline:
        if predicate(state):
            return state
        time.sleep(0.02)
        state = process_state(pid)
    return state


@pytest.mark.contract
def test_pinned_official_kirocrew_start_token_pause_resume_shutdown_and_crash(
    tmp_path: Path,
) -> None:
    assert ARTIFACT == {
        "schemaVersion": 1,
        "distribution": "kirocrew",
        "version": "0.2.0",
        "wheel": "kirocrew-0.2.0-py3-none-any.whl",
        "sha256": DIGEST,
        "entrypoint": "kirocrew",
        "source": "https://github.com/kirodotdev/KiroCrew/releases/tag/v0.2.0",
    }
    assert version("kirocrew") == ARTIFACT["version"]

    layout = WorkspaceLayout(tmp_path / "workspace")
    supervisor = KiroCrewSupervisor(layout, runtime_metadata())
    ready = supervisor.start(timeout_seconds=60)
    try:
        assert ready.port == 5476
        assert ready.home == layout.kirocrew_home
        assert supervisor.ready
        token = supervisor.token()
        assert token
        assert token not in repr(ready)

        listeners: list[str] = []
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                address, port = fields[1].split(":")
                if int(port, 16) == 5476 and fields[3] == "0A":
                    listeners.append(address)
        assert listeners == ["0100007F"]

        supervisor.pause()
        assert wait_for_state(ready.pid, lambda state: state == "T") == "T"
        supervisor.resume()
        assert wait_for_state(ready.pid, lambda state: state not in {None, "T"}) not in {
            None,
            "T",
        }

        # Force the public `kirocrew token` renewal contract without exposing its output.
        supervisor._token = DashboardToken(token, 0)
        renewed = supervisor.token()
        assert renewed
        assert renewed not in (layout.runtime_directory / "runtime.json").read_text()
    finally:
        supervisor.terminate(grace_seconds=10)
    assert process_state(ready.pid) is None

    crashed = KiroCrewSupervisor(WorkspaceLayout(tmp_path / "crash-workspace"), runtime_metadata())
    crash_ready = crashed.start(timeout_seconds=60)
    os.killpg(crash_ready.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while crashed.ready and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not crashed.ready
    crashed.terminate()


@pytest.mark.contract
def test_real_loopback_rest_sse_websocket_allowlist_and_secret_boundary() -> None:
    async def scenario() -> None:
        observed_authorization: list[str | None] = []

        async def status(request: web.Request) -> web.Response:
            observed_authorization.append(request.headers.get("authorization"))
            return web.json_response(
                {"status": "ready"}, headers={"Set-Cookie": "must-not-cross-boundary"}
            )

        async def events(request: web.Request) -> web.StreamResponse:
            observed_authorization.append(request.headers.get("authorization"))
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"event: update\ndata: one\n\n")
            await response.write_eof()
            return response

        async def websocket(request: web.Request) -> web.WebSocketResponse:
            observed_authorization.append(request.headers.get("authorization"))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            message = await ws.receive()
            assert message.type is WSMsgType.TEXT
            await ws.send_str(f"echo:{message.data}")
            await ws.send_bytes(b"binary")
            await ws.close()
            return ws

        application = web.Application()
        application.add_routes(
            [
                web.get("/api/status", status),
                web.get("/api/events", events),
                web.get("/api/ws", websocket),
            ]
        )
        runner = web.AppRunner(application)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 5476)
        await site.start()
        try:
            async with ClientSession() as session:
                backend = LoopbackKiroCrewBackend(session, StaticToken())
                rest = [
                    event
                    async for event in backend.execute(
                        "kirocrew.http",
                        "rest",
                        {"method": "GET", "path": "/api/status", "transport": "http"},
                    )
                ]
                assert json.loads(base64.b64decode(cast(str, rest[1][1]["data"]))) == {
                    "status": "ready"
                }
                assert "set-cookie" not in cast(dict[str, str], rest[0][1]["headers"])

                sse = [
                    event
                    async for event in backend.execute(
                        "kirocrew.http",
                        "sse",
                        {"method": "GET", "path": "/api/events", "transport": "sse"},
                    )
                ]
                assert b"event: update" in base64.b64decode(cast(str, sse[1][1]["data"]))

                ws = [
                    event
                    async for event in backend.execute(
                        "kirocrew.http",
                        "ws",
                        {
                            "frames": ["hello"],
                            "method": "GET",
                            "path": "/api/ws",
                            "transport": "websocket",
                        },
                    )
                ]
                assert ws[1][1]["data"] == "echo:hello"
                assert base64.b64decode(cast(str, ws[2][1]["data"])) == b"binary"

                synthetic = [
                    event
                    async for event in backend.execute(
                        "kirocrew.http",
                        "auth",
                        {"method": "GET", "path": "/api/auth/local-token"},
                    )
                ]
                synthetic_body = base64.b64decode(cast(str, synthetic[1][1]["data"]))
                assert b"contract-loopback-token" not in synthetic_body
                assert json.loads(synthetic_body)["token"] is None

                with pytest.raises(AdapterRequestError) as denied:
                    _ = [
                        event
                        async for event in backend.execute(
                            "kirocrew.http",
                            "shutdown",
                            {"method": "POST", "path": "/api/shutdown"},
                        )
                    ]
                assert denied.value.code == "AUTHORIZATION_FAILED"
                with pytest.raises(AdapterRequestError) as unavailable:
                    _ = [
                        event
                        async for event in backend.execute(
                            "kirocrew.http",
                            "desktop",
                            {"method": "GET", "path": "/api/desktop"},
                        )
                    ]
                assert unavailable.value.code == "FEATURE_UNAVAILABLE_IN_AGENTCORE"

            assert observed_authorization == [
                "Bearer contract-loopback-token",
                "Bearer contract-loopback-token",
                "Bearer contract-loopback-token",
            ]
        finally:
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.contract
def test_route_allowlist_contract_matches_versioned_adapter_boundary() -> None:
    contract = json.loads(
        (ROOT / "contracts" / "kirocrew" / "0.2.0-route-allowlist.json").read_text()
    )
    assert contract["schemaVersion"] == 2
    assert contract["kirocrewVersion"] == "0.2.0"
    assert KiroCrewRoutePolicy.VERSION == contract["kirocrewVersion"]
    # Default-forward: the product must work end to end, so the contract records
    # only the families that stay blocked and why.
    assert contract["defaultDisposition"] == "allowed"
    policy = KiroCrewRoutePolicy()
    denied = {entry["prefix"] for entry in contract["deniedPrefixes"]}
    assert denied == {
        "/api/token",
        "/api/auth/token",
        "/api/secrets",
        "/api/config/export",
        "/api/shutdown",
    }
    for entry in contract["deniedPrefixes"]:
        assert entry["calledByBundle"] is False, (
            f"{entry['prefix']} is denied but the upstream bundle calls it; "
            "denying it would remove product functionality"
        )
        assert policy.classify("GET", entry["prefix"]) is RouteDisposition.DENIED
    for entry in contract["unavailablePrefixes"]:
        assert entry["registeredUpstream"] is False, (
            f"{entry['prefix']} is answered as unavailable but upstream registers it"
        )
        assert policy.classify("GET", entry["prefix"]) is RouteDisposition.UNAVAILABLE

    supervisor_source = (
        ROOT / "runtime" / "src" / "kirocrew_agentcore_runtime" / "supervisor.py"
    ).read_text()
    loopback_source = (
        ROOT / "adapter" / "src" / "kirocrew_agentcore_adapter" / "loopback.py"
    ).read_text()
    assert "from kiro_crew" not in supervisor_source
    assert "from kiro_crew" not in loopback_source
    assert "127.0.0.1:5476" in loopback_source
    assert '"5476"' in supervisor_source
