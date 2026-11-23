from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

import kirocrew_agentcore_runtime.warmup as module
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from kirocrew_agentcore_runtime.warmup import (
    WARMUP_PATH,
    WARMUP_TYPE_HEADER,
    WarmupHook,
    install_warmup_route,
    read_files,
)


class FakeProcess:
    def __init__(self, lines: Iterable[str], exit_code: int | None = None) -> None:
        self.stdout = list(lines)
        self._exit_code = exit_code
        self.pid = 4242

    def poll(self) -> int | None:
        return self._exit_code

    def wait(self, timeout: float | None = None) -> int:
        return self._exit_code or 0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def hook(
    tmp_path: Path, process: FakeProcess, **changes: object
) -> tuple[WarmupHook, dict[str, object]]:
    seen: dict[str, object] = {"stopped": 0}
    clock = Clock()

    def starter(command: list[str], environment: Mapping[str, str]) -> FakeProcess:
        seen["command"] = command
        seen["environment"] = dict(environment)
        seen["home_existed"] = Path(environment["HOME"]).is_dir()
        return process

    def stopper(_process: object) -> None:
        seen["stopped"] = int(seen["stopped"]) + 1  # type: ignore[call-overload]

    model = tmp_path / "models"
    model.mkdir()
    (model / "m.gguf").write_bytes(b"x" * 10)
    options: dict[str, object] = {
        "large_files": (model,),
        "environment": {"PATH": "/bin", "AWS_SESSION_TOKEN": "secret"},
        "starter": starter,
        "stopper": stopper,
        "clock": clock,
        "sleep": clock.sleep,
        "scratch_parent": tmp_path,
    }
    options.update(changes)
    return WarmupHook(**options), seen  # type: ignore[arg-type]


def test_disk_warmup_replays_a_gateway_start_and_reads_large_files(tmp_path: Path) -> None:
    warm, seen = hook(tmp_path, FakeProcess(["booting\n", 'KIROCREW_READY:{"port":5599}\n']))
    result = warm.warm_disk()
    assert result["gateway"] == "ready"
    assert result["bytesRead"] == 10
    assert result["outcome"] == "done"
    assert seen["stopped"] == 1
    assert seen["home_existed"] is True
    environment = seen["environment"]
    assert isinstance(environment, dict)
    assert "AWS_SESSION_TOKEN" not in environment
    assert environment["KIROCREW_PORT"] == "5599"
    command = seen["command"]
    assert isinstance(command, list)
    assert command[:2] == ["kirocrew", "gateway"]
    # The scratch home is removed afterwards.
    assert not list(tmp_path.glob("kc-warmup-*"))


def test_disk_warmup_reports_an_early_exit(tmp_path: Path) -> None:
    warm, seen = hook(tmp_path, FakeProcess(["boom\n"], exit_code=3))
    assert warm.warm_disk()["gateway"] == "exited:3"
    assert seen["stopped"] == 1


def test_disk_warmup_gives_up_at_the_deadline(tmp_path: Path) -> None:
    warm, seen = hook(tmp_path, FakeProcess([]), budget_seconds=2.0)
    assert warm.warm_disk()["gateway"] == "timeout"
    assert seen["stopped"] == 1


def test_disk_warmup_never_raises(tmp_path: Path) -> None:
    def broken(_command: list[str], _environment: Mapping[str, str]) -> FakeProcess:
        raise OSError("no executable")

    warm, _ = hook(tmp_path, FakeProcess([]), starter=broken)
    assert warm.warm_disk()["gateway"] == "error:OSError"


def test_read_files_skips_symlinks_unreadable_files_and_stops_at_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "tree"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_bytes(b"aa")
    (root / "sub" / "b.bin").write_bytes(b"bbb")
    (root / "link").symlink_to(root / "a.bin")
    (root / "locked.bin").write_bytes(b"zz")
    single = tmp_path / "single.bin"
    single.write_bytes(b"s")
    original = Path.open

    def guarded(self: Path, mode: str = "r") -> object:
        if self.name == "locked.bin":
            raise PermissionError("denied")
        return original(self, mode)

    monkeypatch.setattr(Path, "open", guarded)
    assert read_files([root, single, tmp_path / "missing"], 10.0, lambda: 0.0) == 6
    assert read_files([root], 0.0, lambda: 1.0) == 0


def test_route_dispatches_by_warmup_type(tmp_path: Path) -> None:
    asyncio.run(_route_scenario(tmp_path))


async def _route_scenario(tmp_path: Path) -> None:
    warm, seen = hook(tmp_path, FakeProcess(["KIROCREW_READY:{}\n"]))
    application = web.Application()
    install_warmup_route(application, warm)
    async with TestClient(TestServer(application)) as client:
        response = await client.post(WARMUP_PATH, headers={WARMUP_TYPE_HEADER: "memory"})
        assert response.status == 200
        assert warm.last == {"type": "memory", "outcome": "noop"}
        response = await client.post(WARMUP_PATH)
        assert response.status == 200
        assert warm.last == {"type": "missing", "outcome": "noop"}
        response = await client.post(WARMUP_PATH, headers={WARMUP_TYPE_HEADER: "Disk"})
        assert response.status == 200
        assert warm.last["gateway"] == "ready"
        assert seen["stopped"] == 1
        warm.in_service = lambda: True
        response = await client.post(WARMUP_PATH, headers={WARMUP_TYPE_HEADER: "disk"})
        assert response.status == 200
        assert warm.last == {"type": "disk", "outcome": "ignored-in-service"}
        assert seen["stopped"] == 1


def test_default_starter_and_stopper_manage_a_real_process_group() -> None:
    process = module._start(
        [
            sys.executable,
            "-c",
            "import time; print('KIROCREW_READY:{}', flush=True); time.sleep(30)",
        ],
        {"PATH": "/usr/bin:/bin"},
    )
    assert next(iter(process.stdout or ())).startswith("KIROCREW_READY")
    module._stop(process)
    assert process.poll() is not None
    # Stopping an exited process is a no-op.
    module._stop(process)


def test_stopper_escalates_and_tolerates_a_vanished_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signals: list[int] = []

    class Stubborn(FakeProcess):
        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("kirocrew", timeout or 0)

    def killpg(_pid: int, signal: int) -> None:
        signals.append(signal)
        if signal == 9:
            raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", killpg)
    module._stop(Stubborn([]))
    assert signals == [15, 9]

    class NoPid:
        stdout = None

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

    module._stop(NoPid())
    assert signals == [15, 9]
