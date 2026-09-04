from __future__ import annotations

import asyncio
import io
import json
import os
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, cast

import pytest
from kirocrew_agentcore_adapter import identity as module
from kirocrew_agentcore_adapter.identity import (
    CommandResult,
    DeviceFlowParser,
    DeviceProcess,
    KiroAuthState,
    KiroIdentityConfig,
    KiroIdentityError,
    KiroIdentityManager,
    LocalKiroCommandRunner,
    OrganizationLogin,
    PtyDeviceProcess,
    SecretsManagerSecretProvider,
    redact_kiro_output,
)


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 17, 17, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class FakeDeviceProcess:
    def __init__(self, lines: list[str], returncode: int = 0) -> None:
        self._lines = lines
        self.returncode = returncode
        self.terminated = False
        self.written: list[bytes] = []

    async def write(self, data: bytes) -> None:
        self.written.append(data)

    async def lines(self) -> AsyncIterator[str]:
        for line in self._lines:
            yield line

    async def wait(self) -> int:
        return self.returncode

    async def terminate(self) -> None:
        self.terminated = True


class FakeRunner:
    def __init__(
        self,
        results: list[CommandResult] | None = None,
        process: FakeDeviceProcess | None = None,
    ) -> None:
        self.results = results or []
        self.process = process or FakeDeviceProcess([])
        self.run_calls: list[tuple[list[str], dict[str, str], float]] = []
        self.spawn_calls: list[tuple[list[str], dict[str, str]]] = []

    def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        self.run_calls.append((command, dict(env), timeout_seconds))
        return self.results.pop(0)

    def spawn_pty(self, command: list[str], env: Mapping[str, str]) -> DeviceProcess:
        self.spawn_calls.append((command, dict(env)))
        return self.process


class FakeSecret:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def get_secret(self) -> str:
        self.calls += 1
        return self.value


def config(tmp_path: Path, mode: str = "device_flow", **kwargs: object) -> KiroIdentityConfig:
    return KiroIdentityConfig(
        mode,
        tmp_path / "home",
        tmp_path / "api-key-home",
        **kwargs,  # type: ignore[arg-type]
    )


async def collect_events(
    stream: AsyncIterator[tuple[str, Mapping[str, object]]],
) -> list[tuple[str, Mapping[str, object]]]:
    return [event async for event in stream]


def test_device_flow_parser_required_expired_and_redaction() -> None:
    with pytest.raises(ValueError):
        DeviceFlowParser(default_ttl_seconds=0)
    clock = MutableClock()
    parser = DeviceFlowParser(clock=clock, default_ttl_seconds=600)
    assert parser.authorization is None
    assert parser.feed("Waiting for authorization") is None
    assert parser.feed("Visit https:///bad and use Code: ABCD-EFGH") is None
    authorization = parser.feed(
        "Open https://device.example/verify. User code: ABCD-EFGH; expires in 2 minutes"
    )
    assert authorization is not None
    assert authorization.verification_url == "https://device.example/verify"
    assert authorization.user_code == "ABCD-EFGH"
    assert authorization.expires_at == clock.value + timedelta(minutes=2)
    assert authorization.payload() == {
        "expiresAt": "2026-08-17T17:02:00Z",
        "userCode": "ABCD-EFGH",
        "verificationUrl": "https://device.example/verify",
    }
    assert not parser.expired()
    clock.value += timedelta(minutes=2)
    assert parser.expired()

    seconds = DeviceFlowParser(clock=clock)
    defaulted = seconds.feed("https://device.example/start code=QWER-TYUI")
    assert defaulted is not None
    assert defaulted.expires_at == clock.value + timedelta(minutes=10)
    value = seconds.feed("https://device.example/start code=ZXCV-1234 valid after 30 seconds")
    assert value is not None
    assert value.expires_at == clock.value + timedelta(seconds=30)

    redacted = redact_kiro_output(
        "Code: ABCD-EFGH KIRO_API_KEY=secret Bearer abcdefghijklmnopqrstuvwxyz refresh_token:reuse"
    )
    assert "ABCD-EFGH" not in redacted
    assert "secret" not in redacted
    assert "abcdefghijklmnopqrstuvwxyz" not in redacted
    assert "reuse" not in redacted
    assert redacted.count("[REDACTED]") == 4


def test_identity_config_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        config(tmp_path, "unknown")
    with pytest.raises(ValueError):
        config(tmp_path, executable="")
    with pytest.raises(ValueError):
        config(tmp_path, license="pro")
    with pytest.raises(ValueError):
        config(
            tmp_path,
            license="free",
            identity_provider="https://identity.example/start",
            region="us-east-1",
        )
    with pytest.raises(ValueError):
        config(tmp_path, trusted_tools=("bad,name",))
    assert (
        config(
            tmp_path,
            license="pro",
            identity_provider="https://identity.example/start",
            region="us-east-1",
            trusted_tools=("fs_read",),
        ).license
        == "pro"
    )


def test_manager_constructor_and_whoami_states(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        KiroIdentityManager(config(tmp_path, "api_key"), FakeRunner())
    with pytest.raises(ValueError):
        KiroIdentityManager(
            config(tmp_path), FakeRunner(), secret_provider=FakeSecret("must-not-load")
        )

    os.environ["KIRO_API_KEY"] = "ambient-key"
    os.environ["AWS_ACCESS_KEY_ID"] = "ambient-aws-key"
    try:
        runner = FakeRunner(
            [
                CommandResult(1, "not logged in"),
                CommandResult(0, "not json"),
                CommandResult(0, "{}"),
                CommandResult(0, json.dumps({"user": "present"})),
            ]
        )
        manager = KiroIdentityManager(config(tmp_path), runner)
        statuses = [manager.status() for _ in range(4)]
        assert [status.state for status in statuses] == [
            KiroAuthState.REQUIRED,
            # Zero exit code is authoritative even when stdout is not JSON.
            KiroAuthState.AUTHENTICATED,
            KiroAuthState.REQUIRED,
            KiroAuthState.AUTHENTICATED,
        ]
        assert all(status.interactive_supported for status in statuses)
        for command, environment, timeout in runner.run_calls:
            assert command[-3:] == ["whoami", "--format", "json"]
            assert timeout == 30
            assert environment["HOME"] == str(tmp_path / "home")
            assert "KIRO_API_KEY" not in environment
            assert "AWS_ACCESS_KEY_ID" not in environment
    finally:
        os.environ.pop("KIRO_API_KEY")
        os.environ.pop("AWS_ACCESS_KEY_ID")


def test_secrets_manager_provider_boundary() -> None:
    class Client:
        def __init__(self, response: Mapping[str, object]) -> None:
            self.response = response
            self.identifiers: list[str] = []

        def get_secret_value(self, *, SecretId: str) -> Mapping[str, object]:  # noqa: N803
            self.identifiers.append(SecretId)
            return self.response

    with pytest.raises(ValueError):
        SecretsManagerSecretProvider(Client({}), "not-an-arn")
    locator = "arn:aws:secretsmanager:us-east-1:123456789012:secret:kiro-test"
    client = Client({"SecretString": "runtime-value"})
    provider = SecretsManagerSecretProvider(client, locator)
    assert provider.get_secret() == "runtime-value"
    assert client.identifiers == [locator]
    for response in ({}, {"SecretString": ""}, {"SecretString": b"binary"}):
        with pytest.raises(KiroIdentityError, match="unavailable"):
            SecretsManagerSecretProvider(Client(response), locator).get_secret()


def test_device_flow_success_and_organization_command(tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeDeviceProcess(
            [
                "Starting login\n",
                "Visit https://device.example/verify and enter code: ABCD-EFGH; "
                "expires in 10 minutes\n",
            ]
        )
        runner = FakeRunner([CommandResult(0, '{"user":"ok"}')], process)
        manager = KiroIdentityManager(
            config(
                tmp_path,
                license="pro",
                identity_provider="https://identity.example/start",
                region="us-west-2",
            ),
            runner,
        )
        events = await collect_events(manager.device_flow_events())
        assert [operation for operation, _payload in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        assert events[0][1]["userCode"] == "ABCD-EFGH"
        command, environment = runner.spawn_calls[0]
        assert command == [
            "kiro-cli",
            "login",
            "--use-device-flow",
            "--license",
            "pro",
            "--identity-provider",
            "https://identity.example/start",
            "--region",
            "us-west-2",
        ]
        assert "KIRO_API_KEY" not in environment
        assert not await manager.cancel_device_flow()

    asyncio.run(scenario())


def test_device_flow_free_login_pins_license(tmp_path: Path) -> None:
    async def scenario() -> None:
        process = FakeDeviceProcess(
            [
                "Confirm the following code in the browser\n",
                "Code: ABCD-EFGH\n",
                "Open this URL: https://view.awsapps.com/start/#/device?user_code=ABCD-EFGH\n",
            ]
        )
        runner = FakeRunner([CommandResult(0, '{"user":"ok"}')], process)
        manager = KiroIdentityManager(config(tmp_path), runner)
        events = await collect_events(manager.device_flow_events())
        assert [operation for operation, _payload in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        command, _environment = runner.spawn_calls[0]
        assert command == [
            "kiro-cli",
            "login",
            "--use-device-flow",
            "--license",
            "free",
        ]
        assert process.written == []

    asyncio.run(scenario())


def test_organization_login_validation_and_sso_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="start URL"):
        OrganizationLogin("http://example.awsapps.com/start", "us-east-1")
    with pytest.raises(ValueError, match="region"):
        OrganizationLogin("https://example.awsapps.com/start", "US East 1")

    async def scenario() -> None:
        process = FakeDeviceProcess(
            ["Code: ABCD-EFGH\n", "Open this URL: https://example.awsapps.com/verify\n"]
        )
        runner = FakeRunner([CommandResult(0, '{"user":"ok"}')], process)
        manager = KiroIdentityManager(config(tmp_path), runner)
        organization = OrganizationLogin("https://example.awsapps.com/start", "us-east-1")
        events = await collect_events(manager.device_flow_events(organization))
        assert [operation for operation, _payload in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        command, _environment = runner.spawn_calls[0]
        assert command == [
            "kiro-cli",
            "login",
            "--use-device-flow",
            "--license",
            "pro",
            "--identity-provider",
            "https://example.awsapps.com/start",
            "--region",
            "us-east-1",
        ]
        # The pre-filled Start URL and Region prompts are confirmed with Enter.
        assert process.written == [b"\n\n"]

    asyncio.run(scenario())


def test_logout_cancels_flow_and_reports_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = FakeRunner(
            [
                CommandResult(0, "logged out"),
                CommandResult(1, "not logged in"),
            ]
        )
        manager = KiroIdentityManager(config(tmp_path), runner)
        status = await manager.logout()
        assert status.state is KiroAuthState.REQUIRED
        logout_command, logout_environment, timeout = runner.run_calls[0]
        assert logout_command == ["kiro-cli", "logout"]
        assert timeout == 30
        assert "KIRO_API_KEY" not in logout_environment
        assert runner.run_calls[1][0] == ["kiro-cli", "whoami", "--format", "json"]

    asyncio.run(scenario())


def test_logout_unavailable_in_api_key_mode(tmp_path: Path) -> None:
    manager = KiroIdentityManager(
        config(tmp_path, mode="api_key"),
        FakeRunner(),
        secret_provider=FakeSecret("secret-value"),
    )
    with pytest.raises(KiroIdentityError, match="unavailable"):
        asyncio.run(manager.logout())


def test_status_survives_noisy_output_and_slow_whoami(tmp_path: Path) -> None:
    # Warnings or update notices around the JSON document must not read as
    # a sign-in failure; the zero exit code is authoritative.
    noisy = 'Update available: 2.19\n{"account": {"id": "user"}}\nDone'
    manager = KiroIdentityManager(config(tmp_path), FakeRunner([CommandResult(0, noisy)]))
    assert manager.status().state is KiroAuthState.AUTHENTICATED

    garbage = KiroIdentityManager(
        config(tmp_path), FakeRunner([CommandResult(0, "signed in as user")])
    )
    assert garbage.status().state is KiroAuthState.AUTHENTICATED

    broken_json = KiroIdentityManager(
        config(tmp_path), FakeRunner([CommandResult(0, "note {not json} tail")])
    )
    assert broken_json.status().state is KiroAuthState.AUTHENTICATED

    empty = KiroIdentityManager(config(tmp_path), FakeRunner([CommandResult(0, "{}")]))
    assert empty.status().state is KiroAuthState.REQUIRED

    class TimeoutRunner(FakeRunner):
        def run(
            self,
            command: list[str],
            env: Mapping[str, str],
            *,
            timeout_seconds: float,
        ) -> CommandResult:
            raise subprocess.TimeoutExpired(command, timeout_seconds)

    slow = KiroIdentityManager(config(tmp_path), TimeoutRunner())
    assert slow.status().state is KiroAuthState.FAILED


def test_parser_prefers_the_deep_verification_link() -> None:
    parser = DeviceFlowParser()
    parser.feed("Enter Start URL \u2023 https://amzn.awsapps.com/start\n")
    parser.feed("Code: ABCD-EFGH\n")
    authorization = parser.feed(
        "Open this URL: https://amzn.awsapps.com/start/#/device?user_code=ABCD-EFGH\n"
    )
    assert authorization is not None
    assert (
        authorization.verification_url
        == "https://amzn.awsapps.com/start/#/device?user_code=ABCD-EFGH"
    )


def test_device_flow_expired_failed_duplicate_and_cancel(tmp_path: Path) -> None:
    async def expired_scenario() -> None:
        clock = MutableClock()
        parser = DeviceFlowParser(clock=clock, default_ttl_seconds=1)
        process = FakeDeviceProcess(
            ["https://device.example/verify code: ABCD-EFGH valid after 1 seconds"],
            returncode=1,
        )
        manager = KiroIdentityManager(
            config(tmp_path),
            FakeRunner([CommandResult(1, "not logged in")], process),
            parser=parser,
        )
        stream = manager.device_flow_events()
        required = await anext(stream)
        assert required[0] == "kiro.auth_required"
        clock.value += timedelta(seconds=1)
        remaining = [event async for event in stream]
        assert remaining == [
            (
                "kiro.auth_status",
                {"interactiveSupported": True, "state": "expired"},
            )
        ]

    async def failed_scenario() -> None:
        process = FakeDeviceProcess(["login failed"], returncode=1)
        manager = KiroIdentityManager(
            config(tmp_path),
            FakeRunner([CommandResult(1, "not logged in")], process),
        )
        assert await collect_events(manager.device_flow_events()) == [
            ("kiro.auth_status", {"interactiveSupported": True, "state": "failed"})
        ]

    async def duplicate_cancel_scenario() -> None:
        process = FakeDeviceProcess(
            ["https://device.example/verify code: ABCD-EFGH expires in 10 minutes"]
        )
        manager = KiroIdentityManager(
            config(tmp_path),
            FakeRunner([CommandResult(1, "not logged in")], process),
        )
        stream = manager.device_flow_events()
        assert (await anext(stream))[0] == "kiro.auth_required"
        with pytest.raises(KiroIdentityError, match="already active"):
            await anext(manager.device_flow_events())
        assert await manager.cancel_device_flow()
        assert process.terminated
        await cast(AsyncGenerator[tuple[str, Mapping[str, object]], None], stream).aclose()

    asyncio.run(expired_scenario())
    asyncio.run(failed_scenario())
    asyncio.run(duplicate_cancel_scenario())


def test_api_key_mode_headless_only_and_secret_scoping(tmp_path: Path) -> None:
    secret = FakeSecret("api-key-secret-value")
    runner = FakeRunner(
        [
            CommandResult(0, "headless-result"),
        ]
    )
    manager = KiroIdentityManager(
        config(tmp_path, "api_key", trusted_tools=("fs_read", "fs_write")),
        runner,
        secret_provider=secret,
    )
    status = manager.status()
    assert status.state is KiroAuthState.AUTHENTICATED
    assert not status.interactive_supported
    assert status.payload() == {"interactiveSupported": False, "state": "authenticated"}
    result = manager.run_headless("deterministic prompt")
    assert result.stdout == "headless-result"
    command, environment, timeout = runner.run_calls[0]
    assert command == [
        "kiro-cli",
        "chat",
        "--no-interactive",
        "--trust-tools=fs_read,fs_write",
        "--require-mcp-startup",
        "deterministic prompt",
    ]
    assert environment["HOME"] == str(tmp_path / "api-key-home")
    assert environment["KIRO_API_KEY"] == "api-key-secret-value"
    assert timeout == 300
    assert (tmp_path / "api-key-home").stat().st_mode & 0o777 == 0o700
    assert not any(path.is_file() for path in (tmp_path / "api-key-home").rglob("*"))
    with pytest.raises(KiroIdentityError, match="Interactive ACP"):
        manager.require_interactive_identity()

    async def start_api_key_device_flow() -> None:
        await anext(manager.device_flow_events())

    with pytest.raises(KiroIdentityError, match="unavailable"):
        asyncio.run(start_api_key_device_flow())


def test_api_key_and_device_mode_failure_guards(tmp_path: Path) -> None:
    empty_secret = FakeSecret("")
    api_manager = KiroIdentityManager(
        config(tmp_path, "api_key"),
        FakeRunner(),
        secret_provider=empty_secret,
    )
    assert api_manager.status().state is KiroAuthState.FAILED
    with pytest.raises(KiroIdentityError, match="unavailable"):
        api_manager.run_headless("prompt")
    with pytest.raises(ValueError):
        KiroIdentityManager(
            config(tmp_path, "api_key"), FakeRunner(), secret_provider=FakeSecret("key")
        ).run_headless("")

    required = KiroIdentityManager(
        config(tmp_path), FakeRunner([CommandResult(1, "not logged in")])
    )
    with pytest.raises(KiroIdentityError, match="disabled"):
        required.run_headless("prompt")
    with pytest.raises(KiroIdentityError, match="required"):
        required.require_interactive_identity()
    authenticated = KiroIdentityManager(
        config(tmp_path), FakeRunner([CommandResult(0, '{"user":"ok"}')])
    )
    authenticated.require_interactive_identity()


def test_local_runner_and_real_pty_public_contract(tmp_path: Path) -> None:
    async def scenario() -> None:
        runner = LocalKiroCommandRunner()
        environment = module._base_environment()
        result = runner.run(
            [sys.executable, "-c", "print('runner-ok')"],
            environment,
            timeout_seconds=10,
        )
        assert result.returncode == 0
        assert result.stdout == "runner-ok\n"
        process = runner.spawn_pty(
            [sys.executable, "-c", "print('pty-ok')"],
            environment,
        )
        assert [line async for line in process.lines()] == ["pty-ok\r\n"]
        assert await process.wait() == 0
        await process.terminate()

        # write() feeds the PTY: the child reads the confirmation keystrokes
        # the organization login flow sends for its pre-filled prompts.
        echoing = runner.spawn_pty(
            [
                sys.executable,
                "-c",
                "import sys\n"
                "line = sys.stdin.readline().strip()\n"
                "sys.stdout.write('got:' + line + '\\n')\n"
                "sys.stdout.flush()\n",
            ],
            environment,
        )
        await echoing.write(b"confirm\n")
        received = [line async for line in echoing.lines()]
        assert any("got:confirm" in line for line in received)
        await echoing.wait()
        await echoing.terminate()

        sleeping = runner.spawn_pty(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            environment,
        )
        await sleeping.terminate()

    asyncio.run(scenario())


def test_parser_stays_quiet_when_the_authorization_is_unchanged() -> None:
    parser = DeviceFlowParser()
    first = parser.feed("Open https://device.example/verify?user_code=ABCD-EFGH Code: ABCD-EFGH")
    assert first is not None
    # The same code and URL again must not re-emit: callers forward every
    # returned authorization straight to the client.
    assert parser.feed("still waiting for Code: ABCD-EFGH") is None
    assert parser.authorization is first


def test_pty_decode_oserror_and_kill_escalation(monkeypatch: pytest.MonkeyPatch) -> None:
    class ErrorStream(io.BytesIO):
        def readline(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise OSError("closed pty")

    class FakePopen:
        pid = 9876

        def __init__(self) -> None:
            self.waits = 0
            self.running = True

        def poll(self) -> int | None:
            return None if self.running else 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if timeout is not None:
                raise subprocess.TimeoutExpired("kiro-cli", timeout)
            self.running = False
            return -9

    async def scenario() -> None:
        error_process = PtyDeviceProcess(
            cast(subprocess.Popen[bytes], FakePopen()), cast(IO[bytes], ErrorStream())
        )
        assert [line async for line in error_process.lines()] == []

        stream = io.BytesIO(b"\xff\n")
        fake = FakePopen()
        process = PtyDeviceProcess(cast(subprocess.Popen[bytes], fake), cast(IO[bytes], stream))
        assert [line async for line in process.lines()] == ["�\n"]
        sent: list[int] = []

        def killpg(_pid: int, sent_signal: int) -> None:
            sent.append(sent_signal)

        monkeypatch.setattr(os, "killpg", killpg)
        await process.terminate()
        assert sent == [signal.SIGTERM, signal.SIGKILL]
        assert stream.closed

    asyncio.run(scenario())
