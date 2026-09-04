from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import pytest
from kirocrew_agentcore_adapter.identity import (
    KiroAuthState,
    KiroIdentityConfig,
    KiroIdentityManager,
    LocalKiroCommandRunner,
)


class ContractSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret(self) -> str:
        return self._value


def fake_kiro_cli(tmp_path: Path) -> Path:
    executable = tmp_path / "fake-kiro-cli"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

home = Path(os.environ["HOME"])
home.mkdir(parents=True, exist_ok=True)
command = sys.argv[1]
if command == "login":
    assert "--use-device-flow" in sys.argv
    print(
        "Open https://device.example/verify and enter Code: TEST-CODE; "
        "expires in 5 minutes",
        flush=True,
    )
    (home / "authenticated").write_text("supported-state")
    raise SystemExit(0)
if command == "whoami":
    if (home / "authenticated").exists():
        print(json.dumps({"authenticated": True}))
        raise SystemExit(0)
    print(json.dumps({"authenticated": False}))
    raise SystemExit(1)
if command == "chat":
    assert "--no-interactive" in sys.argv
    assert "--require-mcp-startup" in sys.argv
    assert os.environ.get("KIRO_API_KEY")
    print(json.dumps({"headless": True, "credentialExposed": False}))
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


@pytest.mark.contract
def test_device_flow_pty_whoami_and_interactive_handshake(tmp_path: Path) -> None:
    async def scenario() -> None:
        executable = fake_kiro_cli(tmp_path)
        workspace = tmp_path / "workspace"
        manager = KiroIdentityManager(
            KiroIdentityConfig(
                "device_flow",
                workspace / "home",
                tmp_path / "transient-api-home",
                executable=str(executable),
            ),
            LocalKiroCommandRunner(),
        )
        assert manager.status().state is KiroAuthState.REQUIRED
        events = [event async for event in manager.device_flow_events()]
        assert [operation for operation, _payload in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        assert events[0][1]["verificationUrl"] == "https://device.example/verify"
        assert events[0][1]["userCode"] == "TEST-CODE"
        assert manager.status().state is KiroAuthState.AUTHENTICATED
        manager.require_interactive_identity()

        durable_files = [path for path in workspace.rglob("*") if path.is_file()]
        assert durable_files
        durable_contents = "\n".join(path.read_text() for path in durable_files)
        assert "TEST-CODE" not in durable_contents
        assert "device.example" not in durable_contents

    asyncio.run(scenario())


@pytest.mark.contract
def test_api_key_is_scoped_to_noninteractive_child_and_not_persisted(tmp_path: Path) -> None:
    executable = fake_kiro_cli(tmp_path)
    workspace = tmp_path / "workspace"
    transient_home = tmp_path / "transient-api-home"
    opaque_value = "contract-" + "service-value"
    manager = KiroIdentityManager(
        KiroIdentityConfig(
            "api_key",
            workspace / "home",
            transient_home,
            executable=str(executable),
            trusted_tools=("fs_read",),
        ),
        LocalKiroCommandRunner(),
        secret_provider=ContractSecret(opaque_value),
    )
    result = manager.run_headless("deterministic contract prompt")
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"headless": True, "credentialExposed": False}
    assert opaque_value not in result.stdout
    assert not workspace.exists()
    persisted = "\n".join(
        path.read_text(errors="ignore") for path in transient_home.rglob("*") if path.is_file()
    )
    assert opaque_value not in persisted
