from __future__ import annotations

import io
import json
import os
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import IO, cast

import pytest
from kirocrew_agentcore_runtime import supervisor as module
from kirocrew_agentcore_runtime.supervisor import (
    GatewayError,
    GatewayExitedError,
    KiroCrewSupervisor,
    ProcessFactory,
    ProcessHandle,
    RuntimeMetadata,
    WorkspaceLayout,
)

DIGEST = "30bd90fcf5e0adc87541f67162866dcc3b5b2d5a0a44418c6d42022c8c7abbb2"


class FakeProcess:
    def __init__(
        self,
        output: str = "",
        *,
        pid: int = 4217,
        returncode: int | None = None,
        stdout: IO[str] | None = None,
    ) -> None:
        self.pid = pid
        self.stdout: IO[str] | None = io.StringIO(output) if stdout is None else stdout
        self.returncode = returncode
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        return 0 if self.returncode is None else self.returncode


def factory_for(process: FakeProcess) -> ProcessFactory:
    def factory(command: list[str], env: Mapping[str, str]) -> ProcessHandle:
        del command, env
        return process

    return factory


class MutableClock:
    def __init__(self, value: float = 100.0, step: float = 0.0) -> None:
        self.value = value
        self.step = step

    def __call__(self) -> float:
        value = self.value
        self.value += self.step
        return value


class NeverStream:
    def __iter__(self) -> Iterator[str]:
        return self

    def __next__(self) -> str:
        time.sleep(1)
        return "not-ready\n"


def metadata() -> RuntimeMetadata:
    return RuntimeMetadata("0.2.0", DIGEST, "kirocrew-agentcore.v1")


def ready_line(layout: WorkspaceLayout, *, pid: int = 4217, port: int = 5476) -> str:
    return (
        "KIROCREW_READY:"
        + json.dumps(
            {
                "home": str(layout.kirocrew_home),
                "pid": pid,
                "port": port,
                "token": "internal-ready-token",
            }
        )
        + "\n"
    )


def test_runtime_metadata_and_workspace_layout(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        RuntimeMetadata("", DIGEST, "v1")
    with pytest.raises(ValueError):
        RuntimeMetadata("0.2.0", "bad", "v1")

    layout = WorkspaceLayout(tmp_path / "workspace")
    layout.create(metadata())
    layout.create(metadata())
    assert layout.home.is_dir()
    assert layout.kirocrew_home.is_dir()
    assert layout.project_root.is_dir()
    assert (layout.root / "artifacts").is_dir()
    assert (layout.root / "knowledge").is_dir()
    assert (layout.root / "memory").is_dir()
    runtime_file = layout.runtime_directory / "runtime.json"
    assert runtime_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(runtime_file.read_text()) == {
        "kirocrewArtifactSha256": DIGEST,
        "kirocrewVersion": "0.2.0",
        "protocolVersion": "kirocrew-agentcore.v1",
    }


def test_supervisor_start_token_renew_pause_resume_and_terminate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))
    captured: dict[str, object] = {}
    clock = MutableClock()

    def factory(command: list[str], env: Mapping[str, str]) -> FakeProcess:
        captured["command"] = command
        captured["env"] = dict(env)
        return process

    renewals: list[tuple[str, Mapping[str, str], float]] = []

    def renew(executable: str, env: Mapping[str, str], timeout: float) -> str:
        renewals.append((executable, env, timeout))
        return "Dashboard http://127.0.0.1:5476/?token=renewed-token"

    signals: list[int] = []

    def killpg(pid: int, sent_signal: int) -> None:
        assert pid == process.pid
        signals.append(sent_signal)
        if sent_signal == signal.SIGTERM:
            process.returncode = 0

    monkeypatch.setattr(os, "killpg", killpg)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "must-not-propagate")
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        executable="kirocrew",
        process_factory=factory,
        token_command=renew,
        monotonic=clock,
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )

    ready = supervisor.start(timeout_seconds=2)
    assert ready.pid == 4217
    assert ready.port == 5476
    assert ready.home == layout.kirocrew_home
    assert supervisor.pid == 4217
    assert supervisor.ready
    assert supervisor.token() == "internal-ready-token"
    command = cast(list[str], captured["command"])
    assert command == [
        "kirocrew",
        "gateway",
        "--no-open",
        "--port",
        "5476",
        "--json-ready",
        "--approval",
        "reads",
    ]
    environment = cast(dict[str, str], captured["env"])
    assert environment["HOME"] == str(layout.home)
    assert environment["KIROCREW_HOME"] == str(layout.kirocrew_home)
    assert environment["KIROCREW_PORT"] == "5476"
    assert environment["KIROCREW_HOST"] == "127.0.0.1"
    assert environment["PROJECT_ROOT"] == str(layout.project_root)
    assert environment["KIROCREW_VERSION"] == "0.2.0"
    assert environment["KIROCREW_ARTIFACT_SHA256"] == DIGEST
    assert "AWS_ACCESS_KEY_ID" not in environment

    with pytest.raises(GatewayError, match="Exactly one"):
        supervisor.start()
    supervisor.pause()
    supervisor.resume()
    clock.value += 19 * 60 * 60
    assert supervisor.token() == "renewed-token"
    assert renewals and renewals[0][0] == "kirocrew" and renewals[0][2] == 10.0
    supervisor.terminate(grace_seconds=1)
    assert signals == [signal.SIGSTOP, signal.SIGCONT, signal.SIGTERM]
    with pytest.raises(GatewayExitedError):
        supervisor.assert_running()


def test_start_scrubs_stale_gateway_residue_from_a_restored_home(
    tmp_path: Path,
) -> None:
    """A checkpoint captures the running gateway's lock and pid files.

    A fresh gateway would mistake them for a live instance and force-exit,
    so start() removes them before launching the process.
    """
    layout = WorkspaceLayout(tmp_path / "workspace")
    layout.create(metadata())
    state_home = layout.kirocrew_home
    (state_home / "gateway.lock").write_text("9")
    (state_home / ".crons.lock").write_text("")
    (state_home / "kiro_pids.lock").write_text("")
    run_directory = state_home / "run"
    run_directory.mkdir()
    (run_directory / "gateway-5476.pid").write_text("9")
    (run_directory / "gateway-5476.bin").write_text("x")
    (run_directory / "unrelated.txt").write_text("keep")
    (state_home / "config.json").write_text("{}")

    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        executable="kirocrew",
        process_factory=lambda _command, _env: process,
        token_command=lambda *_args: "",
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )
    supervisor.start(timeout_seconds=2)
    assert not (state_home / "gateway.lock").exists()
    assert not (state_home / ".crons.lock").exists()
    assert not (state_home / "kiro_pids.lock").exists()
    assert not (run_directory / "gateway-5476.pid").exists()
    assert not (run_directory / "gateway-5476.bin").exists()
    assert (run_directory / "unrelated.txt").read_text() == "keep"
    assert (state_home / "config.json").exists()


def test_supervisor_rejects_root_bad_timeouts_missing_token_and_crash(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    root = KiroCrewSupervisor(layout, metadata(), effective_uid=lambda: 0)
    with pytest.raises(GatewayError, match="non-root"):
        root.start()

    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    with pytest.raises(ValueError):
        supervisor.start(timeout_seconds=0)
    supervisor.start()
    supervisor._token = None
    with pytest.raises(GatewayError, match="unavailable"):
        supervisor.token()
    process.returncode = 9
    assert not supervisor.ready
    with pytest.raises(GatewayExitedError):
        supervisor.assert_running()
    with pytest.raises(ValueError):
        supervisor.terminate(grace_seconds=-1)
    supervisor.terminate()


def test_token_renewal_requires_environment_and_parses_fallback(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    clock = MutableClock()
    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        token_command=lambda _executable, _env, _timeout: "result&token=fallback-token&next=x",
        monotonic=clock,
        effective_uid=lambda: 10001,
    )
    supervisor.start()
    clock.value += 19 * 60 * 60
    assert supervisor.token() == "fallback-token"
    supervisor._environment = None
    supervisor._token = module.DashboardToken("old", clock.value)
    with pytest.raises(GatewayError, match="environment"):
        supervisor.token()
    with pytest.raises(GatewayError, match="no dashboard token"):
        module._parse_token_output("no credential here")


@pytest.mark.parametrize("renewal_window", [-1.0])
def test_negative_token_renewal_window_is_rejected(tmp_path: Path, renewal_window: float) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    supervisor.start()
    with pytest.raises(ValueError):
        supervisor.token(renewal_window_seconds=renewal_window)


@pytest.mark.parametrize(
    "payload",
    [
        "KIROCREW_READY:not-json\n",
        "KIROCREW_READY:[]\n",
        "KIROCREW_READY:{}\n",
        'KIROCREW_READY:{"port":"5476","pid":1,"token":"x","home":"/x"}\n',
        'KIROCREW_READY:{"port":5476,"pid":1,"token":"","home":"/x"}\n',
    ],
)
def test_invalid_readiness_metadata_is_sanitized_and_process_is_stopped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(payload)

    def killpg(_pid: int, _signal: int) -> None:
        process.returncode = 1

    monkeypatch.setattr(os, "killpg", killpg)
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )
    with pytest.raises(GatewayError, match="invalid readiness"):
        supervisor.start()
    assert supervisor.pid is None


@pytest.mark.parametrize(
    ("pid", "port", "home_suffix"),
    [(4218, 5476, "crew"), (4217, 5477, "crew"), (4217, 5476, "wrong")],
)
def test_readiness_must_match_process_port_and_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pid: int,
    port: int,
    home_suffix: str,
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    reported_home = layout.kirocrew_home if home_suffix == "crew" else layout.root / home_suffix
    output = (
        "KIROCREW_READY:"
        + json.dumps({"home": str(reported_home), "pid": pid, "port": port, "token": "secret"})
        + "\n"
    )
    process = FakeProcess(output)

    def killpg(_pid: int, _signal: int) -> None:
        process.returncode = 1

    monkeypatch.setattr(os, "killpg", killpg)
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )
    with pytest.raises(GatewayError, match="does not match"):
        supervisor.start()


def test_readiness_exit_closed_stdout_missing_stdout_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    processes = [
        FakeProcess("", pid=4217, returncode=3),
        FakeProcess("ignored\n", pid=4218),
        FakeProcess(pid=4219, stdout=cast(IO[str], NeverStream())),
        FakeProcess(pid=4220, stdout=cast(IO[str] | None, None)),
    ]
    # Explicitly restore None because FakeProcess normally creates a StringIO for it.
    processes[-1].stdout = None

    def killpg(pid: int, _signal: int) -> None:
        for process in processes:
            if process.pid == pid and process.returncode is None:
                process.returncode = 1

    monkeypatch.setattr(os, "killpg", killpg)
    for index, expected in enumerate(
        ["exited before readiness", "closed stdout", "timed out", "stdout is required"]
    ):
        clock = MutableClock(step=0.3) if index == 2 else MutableClock()
        selected_process = processes[index]
        supervisor = KiroCrewSupervisor(
            layout,
            metadata(),
            process_factory=factory_for(selected_process),
            health_probe=lambda _port, _timeout: False,
            monotonic=clock,
            effective_uid=lambda: 10001,
            sleep=lambda _delay: None,
        )
        with pytest.raises(GatewayError, match=expected):
            supervisor.start(timeout_seconds=0.5)


def test_readiness_falls_back_to_cli_token_when_ready_line_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upstream gateway can swallow its own READY line (llama-cpp dup2s stdout to
    /dev/null while loading the embedding model). Once the dashboard has answered for
    the grace period, the supervisor mints the token through the CLI instead."""
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(pid=4221, stdout=cast(IO[str], NeverStream()))
    clock = MutableClock(step=0.25)
    # Healthy, then a blip that resets the grace window, then steadily healthy.
    health = iter([True, False] + [True] * 100)
    probes: list[tuple[int, float]] = []

    def probe(port: int, timeout: float) -> bool:
        probes.append((port, timeout))
        return next(health)

    mints: list[Mapping[str, str]] = []
    outcomes: list[str | Exception] = [
        subprocess.CalledProcessError(1, ["kirocrew", "token"]),
        "no token in this output",
        "http://localhost:5476?token=minted-token",
    ]
    outcome_iter = iter(outcomes)

    def token_command(executable: str, env: Mapping[str, str], timeout: float) -> str:
        assert executable == "kirocrew"
        assert timeout == 10.0
        mints.append(env)
        outcome = next(outcome_iter)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(os, "killpg", lambda _pid, _signal: None)
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        token_command=token_command,
        health_probe=probe,
        monotonic=clock,
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )

    ready = supervisor.start(timeout_seconds=60)
    assert ready.pid == 4221
    assert ready.port == 5476
    assert ready.home == layout.kirocrew_home
    assert supervisor.ready
    assert supervisor.token() == "minted-token"
    assert probes and all(port == 5476 for port, _timeout in probes)
    assert len(mints) == 3
    assert mints[0]["KIROCREW_HOME"] == str(layout.kirocrew_home)


def test_probe_dashboard_health_reports_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    seen: list[tuple[str, float]] = []

    def urlopen(request: object, timeout: float) -> Response:
        seen.append((cast(str, getattr(request, "full_url", "")), timeout))
        return Response(200)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    assert module._probe_dashboard_health(5476, 1.5) is True
    assert seen == [("http://127.0.0.1:5476/api/health", 1.5)]

    def refused(request: object, timeout: float) -> Response:
        del request, timeout
        raise ConnectionRefusedError

    monkeypatch.setattr(urllib.request, "urlopen", refused)
    assert module._probe_dashboard_health(5476, 1.5) is False


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Set KIROCREW_ALLOW_UNSANDBOXED=1", "SANDBOX_UNAVAILABLE"),
        ("Permission denied", "PERMISSION_DENIED"),
        ("Read-only file system", "READ_ONLY_FILESYSTEM"),
        ("No space left on device", "STORAGE_EXHAUSTED"),
        ("Cannot persist state: flock unavailable", "PERSISTENCE_UNAVAILABLE"),
        ("sqlite3.OperationalError: disk I/O error", "DATABASE_IO_ERROR"),
        ("sqlite3.OperationalError: database is locked", "DATABASE_LOCKED"),
        ("sqlite3.OperationalError: unable to open database file", "DATABASE_UNOPENABLE"),
        (
            "sqlite3.OperationalError: attempt to write a readonly database",
            "DATABASE_READ_ONLY",
        ),
        ("Address already in use", "PORT_IN_USE"),
        ("Node installation failed", "NODE_UNAVAILABLE"),
        ("FileNotFoundError: missing", "EXECUTABLE_MISSING"),
        ("ModuleNotFoundError: missing", "UPSTREAM_IMPORT_ERROR"),
        ('File "gateway.py", line 1, in warm_backend', "SANDBOX_WARMUP_FAILED"),
        ('File "gateway.py", line 1, in _init_services', "SERVICE_INITIALIZATION_FAILED"),
        (
            'File "gateway.py", line 1, in _start_embeddings',
            "EMBEDDING_INITIALIZATION_FAILED",
        ),
        (
            'File "gateway.py", line 1, in _init_mcp_gateway',
            "MCP_GATEWAY_INITIALIZATION_FAILED",
        ),
        ('File "gateway.py", line 1, in _init_cron', "CRON_INITIALIZATION_FAILED"),
        (
            'File "gateway.py", line 1, in _init_heartbeat',
            "HEARTBEAT_INITIALIZATION_FAILED",
        ),
        (
            'File "gateway.py", line 1, in _init_dashboard',
            "DASHBOARD_INITIALIZATION_FAILED",
        ),
        (
            'File "dashboard.py", line 1, in start_dashboard',
            "DASHBOARD_INITIALIZATION_FAILED",
        ),
        ("Traceback (most recent call last):", "UPSTREAM_EXCEPTION"),
        ("opaque startup output", None),
    ],
)
def test_startup_diagnostics_are_fixed_allowlisted_codes(line: str, expected: str | None) -> None:
    assert module._classify_startup_line(line) == expected


def test_gateway_exit_exposes_only_best_allowlisted_diagnostic(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(
        "Traceback (most recent call last):\n"
        "RuntimeError: user namespace unavailable; set KIROCREW_ALLOW_UNSANDBOXED=1\n",
        returncode=1,
    )
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    with pytest.raises(GatewayExitedError) as captured:
        supervisor.start()
    assert captured.value.diagnostic_code == "SANDBOX_UNAVAILABLE"
    assert "RuntimeError" not in str(captured.value)
    assert (
        module._best_diagnostic(["UPSTREAM_EXCEPTION", "DASHBOARD_INITIALIZATION_FAILED"])
        == "DASHBOARD_INITIALIZATION_FAILED"
    )
    assert module._best_diagnostic([]) == "UNKNOWN"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("sqlite3.OperationalError: unable to open database /mnt/x", "sqlite3.OperationalError"),
        ("kiro_crew.sandbox.SandboxUnavailableError", "kiro_crew.sandbox.SandboxUnavailableError"),
        ("RuntimeError: token=abcdef secret", "RuntimeError"),
        ("SystemExit: 1", "SystemExit"),
        ("KeyboardInterrupt", "KeyboardInterrupt"),
        ("  ValueError: bad value  ", "ValueError"),
        ('  File "gateway.py", line 1, in _init_services', None),
        ("Traceback (most recent call last):", None),
        ("some narrative output: not an exception", None),
        ("a" * 80 + "Error: too long", None),
    ],
)
def test_exception_type_extraction_keeps_only_validated_class_paths(
    line: str, expected: str | None
) -> None:
    extracted = module._classify_exception_type(line)
    assert extracted == expected
    if extracted is not None:
        assert ":" not in extracted
        assert " " not in extracted


def test_gateway_exit_reports_last_exception_class_without_message(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(
        "Traceback (most recent call last):\n"
        '  File "gateway.py", line 1, in _init_services\n'
        "sqlite3.OperationalError: unable to open database file /mnt/workspace/secret.db\n",
        returncode=1,
    )
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    with pytest.raises(GatewayExitedError) as captured:
        supervisor.start()
    assert captured.value.diagnostic_code == "DATABASE_UNOPENABLE"
    assert captured.value.exception_type == "sqlite3.OperationalError"
    assert "secret.db" not in str(captured.value)
    assert "unable to open" not in str(captured.value)
    assert module._last_exception_type([]) == "UNKNOWN"


def test_non_database_service_failure_still_reports_the_stage_code(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(
        "Traceback (most recent call last):\n"
        '  File "gateway.py", line 1, in _init_services\n'
        "RuntimeError: opaque upstream detail\n",
        returncode=1,
    )
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    with pytest.raises(GatewayExitedError) as captured:
        supervisor.start()
    assert captured.value.diagnostic_code == "SERVICE_INITIALIZATION_FAILED"
    assert captured.value.exception_type == "RuntimeError"
    assert "opaque upstream detail" not in str(captured.value)


def test_termination_escalates_and_already_exited_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))
    sent: list[int] = []

    def killpg(_pid: int, sent_signal: int) -> None:
        sent.append(sent_signal)
        if sent_signal == signal.SIGKILL:
            process.returncode = -9

    monkeypatch.setattr(os, "killpg", killpg)
    clock = MutableClock(step=1)
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        monotonic=clock,
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
    )
    supervisor.start()
    supervisor.terminate(grace_seconds=0.5)
    assert sent == [signal.SIGTERM, signal.SIGKILL]
    process.returncode = 0
    supervisor._terminate_process(process, 1)


def test_initial_readiness_pid_and_graceful_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
    )
    assert not supervisor.ready
    assert supervisor.pid is None
    supervisor.start()
    assert supervisor.pid == process.pid

    sent: list[int] = []

    def killpg(_pid: int, sent_signal: int) -> None:
        sent.append(sent_signal)

    def child_exits(_delay: float) -> None:
        process.returncode = 0

    monkeypatch.setattr(os, "killpg", killpg)
    supervisor._sleep = child_exits
    supervisor.terminate(grace_seconds=1)
    assert sent == [signal.SIGTERM]


def test_subprocess_helpers_use_public_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    popen_call: dict[str, object] = {}

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        popen_call["command"] = command
        popen_call.update(kwargs)
        return process

    class Completed:
        stdout = "dashboard output"

    run_call: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        run_call["command"] = command
        run_call.update(kwargs)
        return Completed()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert module._start_process(["kirocrew", "gateway"], {"HOME": "/workspace/home"}) is process
    assert popen_call["start_new_session"] is True
    assert (
        module._run_token_command("kirocrew", {"HOME": "/workspace/home"}, 4) == "dashboard output"
    )
    assert run_call["command"] == ["kirocrew", "token"]
    assert run_call["timeout"] == 4


def test_wal_probe_accepts_a_capable_directory_and_leaves_nothing_behind(tmp_path: Path) -> None:
    directory = tmp_path / "capable"
    assert module.supports_sqlite_wal(directory) is True
    assert sorted(item.name for item in directory.iterdir()) == []


def test_wal_probe_reports_false_when_sqlite_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    assert module.supports_sqlite_wal(tmp_path / "hostile") is False


def test_wal_probe_reports_false_when_journal_mode_is_not_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Cursor:
        def __init__(self, row: tuple[str] | None) -> None:
            self._row = row

        def fetchone(self) -> tuple[str] | None:
            return self._row

    class Connection:
        def __init__(self, row: tuple[str] | None) -> None:
            self._row = row

        def execute(self, _statement: str) -> Cursor:
            return Cursor(self._row)

        def close(self) -> None:
            return None

    for row in (None, ("delete",)):

        def connect(
            *_args: object, bound: tuple[str] | None = row, **_kwargs: object
        ) -> Connection:
            return Connection(bound)

        monkeypatch.setattr(sqlite3, "connect", connect)
        assert module.supports_sqlite_wal(tmp_path / "fallback") is False


def test_mirror_replaces_a_stale_temporary_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    (source / "keep.txt").write_text("kept", encoding="utf-8")
    stale = destination.with_name(f"{destination.name}.mirror-tmp")
    stale.mkdir(parents=True)
    (stale / "leftover.txt").write_text("stale", encoding="utf-8")

    module._mirror_directory(source, destination)
    assert (destination / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert not (destination / "leftover.txt").exists()
    assert not stale.exists()


def test_staging_mirrors_both_directions_and_drops_shared_memory_files(tmp_path: Path) -> None:
    authoritative = tmp_path / "workspace" / "crew"
    staged = tmp_path / "local" / "crew"
    (authoritative / "nested").mkdir(parents=True)
    (authoritative / "memory.db").write_text("original", encoding="utf-8")
    (authoritative / "memory.db-shm").write_text("scratch", encoding="utf-8")
    (authoritative / "nested" / "keep.json").write_text("{}", encoding="utf-8")

    staging = module.GatewayStateStaging(authoritative, staged)
    staging.materialize()
    assert (staged / "memory.db").read_text(encoding="utf-8") == "original"
    assert (staged / "nested" / "keep.json").exists()
    assert not (staged / "memory.db-shm").exists()
    assert staged.stat().st_mode & 0o777 == 0o700

    (staged / "memory.db").write_text("updated", encoding="utf-8")
    (staged / "memory.db-wal").write_text("frames", encoding="utf-8")
    (staged / "nested" / "keep.json").unlink()
    staging.materialize()
    assert (staged / "nested" / "keep.json").exists()
    assert (staged / "memory.db").read_text(encoding="utf-8") == "original"

    (staged / "memory.db").write_text("committed", encoding="utf-8")
    (staged / "nested" / "keep.json").unlink()
    staging.synchronize()
    assert (authoritative / "memory.db").read_text(encoding="utf-8") == "committed"
    assert not (authoritative / "nested" / "keep.json").exists()


def test_supervisor_uses_the_workspace_directly_when_it_supports_wal(tmp_path: Path) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
        state_root=tmp_path / "unused",
        wal_probe=lambda _directory: True,
    )
    supervisor.start()
    assert supervisor.state_home == layout.kirocrew_home
    assert supervisor.staging is None
    supervisor.synchronize_state()
    assert not (tmp_path / "unused").exists()


def test_supervisor_stages_state_locally_when_the_workspace_cannot_host_wal(
    tmp_path: Path,
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    state_root = tmp_path / "local-state"
    staged_home = state_root / "crew"

    def probe(directory: Path) -> bool:
        return directory != layout.kirocrew_home

    ready = (
        "KIROCREW_READY:"
        + json.dumps({"home": str(staged_home), "pid": 4217, "port": 5476, "token": "staged-token"})
        + "\n"
    )
    process = FakeProcess(ready)
    captured: dict[str, object] = {}

    def factory(command: list[str], env: Mapping[str, str]) -> FakeProcess:
        del command
        captured["env"] = dict(env)
        return process

    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory,
        effective_uid=lambda: 10001,
        state_root=state_root,
        wal_probe=probe,
    )
    layout.create(metadata())
    (layout.kirocrew_home / "memory.db").write_text("restored", encoding="utf-8")

    supervisor.start()
    assert supervisor.state_home == staged_home
    assert supervisor.staging == module.GatewayStateStaging(layout.kirocrew_home, staged_home)
    assert cast(dict[str, str], captured["env"])["KIROCREW_HOME"] == str(staged_home)
    assert (staged_home / "memory.db").read_text(encoding="utf-8") == "restored"

    (staged_home / "memory.db").write_text("live", encoding="utf-8")
    supervisor.synchronize_state()
    assert (layout.kirocrew_home / "memory.db").read_text(encoding="utf-8") == "live"


def test_supervisor_fails_closed_when_no_filesystem_can_host_the_state_database(
    tmp_path: Path,
) -> None:
    layout = WorkspaceLayout(tmp_path / "workspace")
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(FakeProcess(ready_line(layout))),
        effective_uid=lambda: 10001,
        state_root=tmp_path / "local-state",
        wal_probe=lambda _directory: False,
    )
    with pytest.raises(GatewayError, match="can host the gateway state database"):
        supervisor.start()
    assert supervisor.pid is None


def test_default_staging_root_is_derived_from_the_platform_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    layout = WorkspaceLayout(tmp_path / "workspace")
    process = FakeProcess(ready_line(layout))

    def killpg(_pid: int, _signal: int) -> None:
        process.returncode = 1

    monkeypatch.setattr(os, "killpg", killpg)
    supervisor = KiroCrewSupervisor(
        layout,
        metadata(),
        process_factory=factory_for(process),
        effective_uid=lambda: 10001,
        sleep=lambda _delay: None,
        wal_probe=lambda directory: directory != layout.kirocrew_home,
    )
    layout.create(metadata())
    with pytest.raises(GatewayError, match="does not match"):
        supervisor.start()
    assert supervisor.state_home == tmp_path / "tmp" / "kirocrew-state" / "crew"
