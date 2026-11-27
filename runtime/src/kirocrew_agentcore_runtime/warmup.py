"""Build warmup hook for AgentCore Runtime platform version V2.

On V2 every session starts from a memory snapshot taken after the platform
drops the page cache, so the files a session reads at start come back one
lazy disk fetch at a time. The platform calls the warmup hook twice while it
builds the runtime: ``memory`` before the snapshot and ``disk`` on a microVM
restored from it. Reads made during the ``disk`` call tell the platform which
files new sessions need first.

The reads that matter are the KiroCrew gateway's own start, which is hard to
list by hand: thousands of Python modules, the embedding model and the Kiro
CLI. So the ``disk`` call starts a throwaway gateway in a scratch home, waits
for it to report ready, and stops it, while a second thread reads the large
binaries the first chat needs. The ``disk`` microVM is not snapshotted, so
nothing the throwaway gateway writes reaches a session.

The hook never fails the build: the platform does not retry a failed hook and
fails the whole create or update, which is worse than a slower first start.
Every outcome answers 200 and is logged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess  # nosec B404
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

from aiohttp import web

_LOGGER = logging.getLogger(__name__)

WARMUP_PATH: Final = "/warmup"
WARMUP_TYPE_HEADER: Final = "X-Amzn-Bedrock-AgentCore-Runtime-Warmup-Type"
#: The platform gives both calls 120 s together. The memory call waits for the
#: gateway fork server to finish importing; the disk call keeps the rest, less
#: a margin for its own shutdown and the response.
MEMORY_BUDGET_SECONDS: Final = 45.0
DISK_BUDGET_SECONDS: Final = 60.0
#: After the gateway reports ready it still loads lazily imported modules.
SETTLE_SECONDS: Final = 5.0
_READ_BLOCK: Final = 1024 * 1024
_SCRATCH_PORT: Final = 5599
_CREDENTIAL_KEYS: Final = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)


class GatewayProcess(Protocol):
    @property
    def stdout(self) -> Iterable[str] | None: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessStarter = Callable[[list[str], Mapping[str, str]], GatewayProcess]
ProcessStopper = Callable[[GatewayProcess], None]


def _start(command: list[str], environment: Mapping[str, str]) -> GatewayProcess:
    return subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argv, no shell.
        command,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )


def _stop(process: GatewayProcess) -> None:
    pid = getattr(process, "pid", None)
    if pid is None or process.poll() is not None:
        return
    try:
        os.killpg(pid, 15)
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        try:
            os.killpg(pid, 9)
        except OSError:
            _LOGGER.warning("Warmup gateway did not stop cleanly.", exc_info=True)


def read_files(paths: Iterable[Path], deadline: float, clock: Callable[[], float]) -> int:
    """Read each file (or every file under a directory) once; return bytes read."""
    total = 0
    for root in paths:
        candidates = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in candidates:
            if clock() >= deadline:
                return total
            if not path.is_file() or path.is_symlink():
                continue
            try:
                with path.open("rb") as source:
                    while block := source.read(_READ_BLOCK):
                        total += len(block)
            except OSError:
                continue
    return total


@dataclass
class WarmupHook:
    """Serves ``POST /warmup``; holds the result of the last call for tests and logs."""

    executable: str = "kirocrew"
    large_files: tuple[Path, ...] = (
        Path("/usr/local/bin/kiro-cli"),
        Path("/usr/local/bin/kiro-cli-chat"),
        Path("/opt/kirocrew/models"),
    )
    environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    starter: ProcessStarter = _start
    stopper: ProcessStopper = _stop
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    scratch_parent: Path | None = None
    budget_seconds: float = DISK_BUDGET_SECONDS
    settle_seconds: float = SETTLE_SECONDS
    in_service: Callable[[], bool] = lambda: False
    #: Called by the memory call with its budget; True once what the snapshot
    #: should hold (the fork server's imported modules) is in memory.
    prepare_memory: Callable[[float], bool] = lambda _budget: False
    memory_budget_seconds: float = MEMORY_BUDGET_SECONDS
    last: dict[str, object] = field(default_factory=dict)

    async def handle(self, request: web.Request) -> web.Response:
        warmup_type = request.headers.get(WARMUP_TYPE_HEADER, "").strip().lower()
        if self.in_service():
            # A session is already being served: this is not a build call.
            self.last = {"type": warmup_type, "outcome": "ignored-in-service"}
        elif warmup_type == "disk":
            self.last = await asyncio.to_thread(self.warm_disk)
        elif warmup_type == "memory":
            # The gateway cannot start before a user's home is restored, but its
            # imported modules can be in the snapshot: they live in the fork
            # server, which forks each session's gateway (forkserver.py).
            started = self.clock()
            ready = await asyncio.to_thread(self.prepare_memory, self.memory_budget_seconds)
            self.last = {
                "type": "memory",
                "outcome": "forkserver-ready" if ready else "forkserver-unavailable",
                "seconds": round(self.clock() - started, 1),
            }
        else:
            self.last = {"type": warmup_type or "missing", "outcome": "noop"}
        _LOGGER.info("Warmup %s", " ".join(f"{k}={v}" for k, v in self.last.items()))
        return web.Response(status=200)

    def warm_disk(self) -> dict[str, object]:
        started = self.clock()
        deadline = started + self.budget_seconds
        result: dict[str, object] = {"type": "disk"}
        read: dict[str, int] = {}
        reader = threading.Thread(
            target=lambda: read.setdefault(
                "bytes", read_files(self.large_files, deadline, self.clock)
            ),
            daemon=True,
        )
        reader.start()
        try:
            result["gateway"] = self._replay_gateway_start(deadline)
        except Exception as error:
            result["gateway"] = f"error:{type(error).__name__}"
        reader.join(max(0.0, deadline - self.clock()))
        result["bytesRead"] = read.get("bytes", -1)
        result["seconds"] = round(self.clock() - started, 1)
        result["outcome"] = "done"
        return result

    def _replay_gateway_start(self, deadline: float) -> str:
        scratch = Path(tempfile.mkdtemp(prefix="kc-warmup-", dir=self.scratch_parent))
        home = scratch / "home"
        project = scratch / "project"
        home.mkdir()
        project.mkdir()
        environment = {k: v for k, v in self.environment.items() if k not in _CREDENTIAL_KEYS}
        environment.update(
            {
                "HOME": str(home),
                "KIROCREW_HOME": str(home / ".kiro" / "crew"),
                "KIROCREW_PORT": str(_SCRATCH_PORT),
                "KIROCREW_HOST": "127.0.0.1",
                "PROJECT_ROOT": str(project),
            }
        )
        command = [
            self.executable,
            "gateway",
            "--no-open",
            "--port",
            str(_SCRATCH_PORT),
            "--json-ready",
            "--approval",
            "reads",
        ]
        process = self.starter(command, environment)
        ready = threading.Event()

        def watch() -> None:
            for line in process.stdout or ():
                if line.startswith("KIROCREW_READY"):
                    ready.set()

        threading.Thread(target=watch, daemon=True).start()
        try:
            while not ready.is_set() and process.poll() is None:
                if self.clock() >= deadline:
                    return "timeout"
                self.sleep(0.5)
            if not ready.is_set():
                return f"exited:{process.poll()}"
            self.sleep(max(0.0, min(self.settle_seconds, deadline - self.clock())))
            return "ready"
        finally:
            self.stopper(process)
            shutil.rmtree(scratch, ignore_errors=True)


def install_warmup_route(application: web.Application, hook: WarmupHook) -> None:
    application.router.add_post(WARMUP_PATH, hook.handle)
