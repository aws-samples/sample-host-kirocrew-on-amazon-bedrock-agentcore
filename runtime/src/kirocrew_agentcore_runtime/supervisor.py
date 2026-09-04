from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess  # nosec B404
import tempfile
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Protocol, cast
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger(__name__)

_READY_PREFIX: Final = "KIROCREW_READY:"
_TOKEN_PATTERN: Final = re.compile(r"(?:^|[?&])token=([^&\s]+)")
_DEFAULT_TOKEN_TTL_SECONDS: Final = 19 * 60 * 60
_DASHBOARD_PORT: Final = 5476
_HEALTH_PROBE_INTERVAL_SECONDS: Final = 1.0
# How long the dashboard must keep answering before we stop waiting for the
# readiness line and mint a token ourselves. The upstream gateway prints
# KIROCREW_READY right after the dashboard starts listening; if it has not
# shown up by then, the line was lost and will never come.
_READY_LINE_GRACE_SECONDS: Final = 3.0


class GatewayError(RuntimeError):
    """The upstream gateway could not satisfy its public process contract."""


class GatewayExitedError(GatewayError):
    """The supervised upstream gateway exited unexpectedly."""

    def __init__(
        self,
        message: str,
        diagnostic_code: str = "UNKNOWN",
        exception_type: str = "UNKNOWN",
    ) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code
        self.exception_type = exception_type


class ProcessHandle(Protocol):
    pid: int
    stdout: IO[str] | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


class ProcessFactory(Protocol):
    def __call__(self, command: list[str], env: Mapping[str, str]) -> ProcessHandle: ...


class TokenCommand(Protocol):
    def __call__(self, executable: str, env: Mapping[str, str], timeout: float) -> str: ...


class HealthProbe(Protocol):
    def __call__(self, port: int, timeout: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class RuntimeMetadata:
    kirocrew_version: str
    kirocrew_artifact_sha256: str
    protocol_version: str

    def __post_init__(self) -> None:
        if not self.kirocrew_version or not re.fullmatch(
            r"[0-9a-f]{64}", self.kirocrew_artifact_sha256
        ):
            raise ValueError("Pinned runtime metadata is invalid.")


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    root: Path

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def kirocrew_home(self) -> Path:
        return self.home / ".kiro" / "crew"

    @property
    def project_root(self) -> Path:
        return self.root / "projects" / "default"

    @property
    def runtime_directory(self) -> Path:
        return self.root / ".agentcore"

    def create(self, metadata: RuntimeMetadata) -> None:
        for directory in (
            self.home,
            self.kirocrew_home,
            self.project_root,
            self.root / "artifacts",
            self.root / "knowledge",
            self.root / "memory",
            self.runtime_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
        metadata_path = self.runtime_directory / "runtime.json"
        temporary_path = metadata_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(
                {
                    "kirocrewArtifactSha256": metadata.kirocrew_artifact_sha256,
                    "kirocrewVersion": metadata.kirocrew_version,
                    "protocolVersion": metadata.protocol_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary_path.chmod(0o600)
        temporary_path.replace(metadata_path)


@dataclass(frozen=True, slots=True)
class DashboardToken:
    value: str
    renew_at: float


@dataclass(frozen=True, slots=True)
class GatewayReady:
    pid: int
    port: int
    home: Path


_WAL_PROBE_NAME: Final = ".kirocrew-wal-probe.db"
_SQLITE_SIDECAR_SUFFIXES: Final = ("-wal", "-shm")
_STAGING_DIRECTORY_NAME: Final = "crew"


def supports_sqlite_wal(directory: Path) -> bool:
    """Whether *directory* can host the SQLite WAL journal the upstream gateway requires.

    Upstream's ``VectorMemoryStore.init`` issues ``PRAGMA journal_mode=WAL``, which needs
    ``fcntl`` POSIX byte-range locks and shared memory. A managed mount can satisfy
    ``flock`` (so upstream's own persistence preflight passes) yet still fail here, so the
    capability is measured directly instead of assumed.
    """
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / _WAL_PROBE_NAME
    try:
        with contextlib.closing(sqlite3.connect(probe, timeout=5.0)) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).casefold() != "wal":
                return False
            connection.execute("CREATE TABLE probe (value INTEGER NOT NULL)")
            connection.execute("INSERT INTO probe VALUES (1)")
            connection.commit()
        return True
    except sqlite3.Error:
        return False
    finally:
        for suffix in ("", *_SQLITE_SIDECAR_SUFFIXES):
            with contextlib.suppress(OSError):
                probe.with_name(probe.name + suffix).unlink()


def _ignore_shared_memory(_directory: str, names: list[str]) -> set[str]:
    """Exclude SQLite ``-shm`` scratch files, which are rebuilt when a database is opened."""
    return {name for name in names if name.endswith("-shm")}


def _mirror_directory(source: Path, destination: Path) -> None:
    """Replace *destination* with a copy of *source*, excluding shared-memory scratch files."""
    staging = destination.with_name(f"{destination.name}.mirror-tmp")
    if staging.exists():
        shutil.rmtree(staging)
    source.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, staging, symlinks=True, ignore=_ignore_shared_memory)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging.replace(destination)
    destination.chmod(0o700)


@dataclass(frozen=True, slots=True)
class GatewayStateStaging:
    """A local, lock-capable copy of the authoritative gateway state directory.

    The authoritative copy remains inside the checkpointed workspace, so encrypted
    checkpoints stay the source of truth; only the live SQLite working set is relocated.
    """

    authoritative: Path
    staged: Path

    def materialize(self) -> None:
        _mirror_directory(self.authoritative, self.staged)

    def synchronize(self) -> None:
        _mirror_directory(self.staged, self.authoritative)


class KiroCrewSupervisor:
    def __init__(
        self,
        layout: WorkspaceLayout,
        metadata: RuntimeMetadata,
        *,
        executable: str = "kirocrew",
        process_factory: ProcessFactory | None = None,
        token_command: TokenCommand | None = None,
        health_probe: HealthProbe | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        effective_uid: Callable[[], int] = os.geteuid,
        sleep: Callable[[float], None] = time.sleep,
        state_root: Path | None = None,
        wal_probe: Callable[[Path], bool] = supports_sqlite_wal,
        chdir: Callable[[Path], None] = os.chdir,
    ) -> None:
        self._layout = layout
        self._metadata = metadata
        self._executable = executable
        self._process_factory = process_factory or _start_process
        self._token_command = token_command or _run_token_command
        self._health_probe = health_probe or _probe_dashboard_health
        self._monotonic = monotonic
        self._chdir = chdir
        self._effective_uid = effective_uid
        self._sleep = sleep
        self._state_root = state_root
        self._wal_probe = wal_probe
        self._staging: GatewayStateStaging | None = None
        self._state_home = layout.kirocrew_home
        self._process: ProcessHandle | None = None
        self._token: DashboardToken | None = None
        self._ready = False
        self._environment: dict[str, str] | None = None

    @property
    def state_home(self) -> Path:
        """The directory the gateway is told to use as its data home."""
        return self._state_home

    @property
    def staging(self) -> GatewayStateStaging | None:
        """The active staging pair, or ``None`` when the workspace hosts state directly."""
        return self._staging

    @property
    def ready(self) -> bool:
        if not self._ready or self._process is None:
            return False
        if self._process.poll() is not None:
            self._ready = False
            self._token = None
            return False
        return True

    @property
    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def start(self, *, timeout_seconds: float = 60.0) -> GatewayReady:
        if self._effective_uid() == 0:
            raise GatewayError("The KiroCrew gateway must run as a non-root user.")
        if timeout_seconds <= 0:
            raise ValueError("Startup timeout must be positive.")
        if self._process is not None and self._process.poll() is None:
            raise GatewayError("Exactly one KiroCrew gateway may run in a sandbox.")
        self._layout.create(self._metadata)
        # A restore swaps workspace entries out from under this process; a
        # stale working directory would hand every child a deleted cwd and
        # crash the gateway on its first os.getcwd(). Re-anchor first.
        self._chdir(self._layout.root)
        self._state_home = self._resolve_state_home()
        self._scrub_runtime_residue(self._state_home)
        environment = self._build_environment()
        command = [
            self._executable,
            "gateway",
            "--no-open",
            "--port",
            str(_DASHBOARD_PORT),
            "--json-ready",
            "--approval",
            "reads",
        ]
        process = self._process_factory(command, environment)
        self._process = process
        self._environment = environment
        try:
            ready, token = self._await_ready(process, timeout_seconds, environment)
        except Exception:
            self._terminate_process(process, grace_seconds=1.0)
            self._process = None
            self._environment = None
            raise
        if (
            ready.port != _DASHBOARD_PORT
            or ready.pid != process.pid
            or ready.home != self._state_home
        ):
            self._terminate_process(process, grace_seconds=1.0)
            self._process = None
            self._environment = None
            raise GatewayError("KiroCrew readiness metadata does not match the sandbox contract.")
        self._token = DashboardToken(token, self._monotonic() + _DEFAULT_TOKEN_TTL_SECONDS)
        self._ready = True
        return ready

    @staticmethod
    def _scrub_runtime_residue(state_home: Path) -> None:
        """Remove single-instance markers a checkpoint captured mid-flight.

        A restored home carries the previous gateway's lock and pid files;
        a fresh process would mistake them for a live instance and exit.
        """
        for name in ("gateway.lock", ".crons.lock", "kiro_pids.lock"):
            candidate = state_home / name
            if candidate.is_file():
                candidate.unlink(missing_ok=True)
        run_directory = state_home / "run"
        if run_directory.is_dir():
            for candidate in run_directory.iterdir():
                if candidate.name.startswith("gateway-") and candidate.is_file():
                    candidate.unlink(missing_ok=True)

    def token(self, *, renewal_window_seconds: float = 300.0) -> str:
        self.assert_running()
        if renewal_window_seconds < 0:
            raise ValueError("Token renewal window cannot be negative.")
        token = self._token
        if token is None:
            raise GatewayError("The loopback dashboard token is unavailable.")
        if token.renew_at - self._monotonic() <= renewal_window_seconds:
            if self._environment is None:
                raise GatewayError("The gateway environment is unavailable.")
            output = self._token_command(self._executable, self._environment, 10.0)
            value = _parse_token_output(output)
            self._token = DashboardToken(value, self._monotonic() + _DEFAULT_TOKEN_TTL_SECONDS)
        return cast(DashboardToken, self._token).value

    def pause(self) -> None:
        process = self.assert_running()
        os.killpg(process.pid, signal.SIGSTOP)

    def resume(self) -> None:
        process = self.assert_running()
        os.killpg(process.pid, signal.SIGCONT)

    def terminate(self, *, grace_seconds: float = 10.0) -> None:
        if grace_seconds < 0:
            raise ValueError("Termination grace period cannot be negative.")
        process = self._process
        if process is not None and process.poll() is None:
            self._terminate_process(process, grace_seconds)
        self._process = None
        self._environment = None
        self._token = None
        self._ready = False

    def assert_running(self) -> ProcessHandle:
        process = self._process
        if process is None or process.poll() is not None:
            self._ready = False
            self._token = None
            raise GatewayExitedError("The upstream KiroCrew gateway is not running.")
        return process

    def _resolve_state_home(self) -> Path:
        """Choose the gateway data home, staging locally only when the workspace cannot host it.

        The workspace is preferred so a filesystem that supports WAL keeps today's exact
        behaviour with no copying. When it cannot, the authoritative copy is mirrored onto a
        lock-capable local directory and that directory becomes the live data home.
        """
        authoritative = self._layout.kirocrew_home
        self._staging = None
        if self._wal_probe(authoritative):
            return authoritative
        staged = self._staging_root() / _STAGING_DIRECTORY_NAME
        staging = GatewayStateStaging(authoritative, staged)
        staging.materialize()
        if not self._wal_probe(staged):
            raise GatewayError(
                "No filesystem available to the sandbox can host the gateway state database."
            )
        self._staging = staging
        return staged

    def _staging_root(self) -> Path:
        root = self._state_root or Path(tempfile.gettempdir()) / "kirocrew-state"
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        return root

    def synchronize_state(self) -> None:
        """Copy staged gateway state back into the checkpointed workspace.

        A no-op when the workspace hosts state directly. Callers must invoke this while the
        gateway is quiesced and before a manifest is built, so the captured tree is
        consistent; a previously committed generation stays authoritative until it is.
        """
        staging = self._staging
        if staging is not None:
            staging.synchronize()

    def _build_environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
        }
        environment.update(
            {
                "HOME": str(self._layout.home),
                "KIROCREW_HOME": str(self._state_home),
                "KIROCREW_PORT": "5476",
                "KIROCREW_HOST": "127.0.0.1",
                "PROJECT_ROOT": str(self._layout.project_root),
                "KIROCREW_VERSION": self._metadata.kirocrew_version,
                "KIROCREW_ARTIFACT_SHA256": self._metadata.kirocrew_artifact_sha256,
                "KIROCREW_AGENTCORE_PROTOCOL": self._metadata.protocol_version,
            }
        )
        return environment

    def _await_ready(
        self, process: ProcessHandle, timeout_seconds: float, environment: Mapping[str, str]
    ) -> tuple[GatewayReady, str]:
        """Wait for the gateway to become ready and return its readiness metadata.

        The primary signal is the ``KIROCREW_READY:{...}`` line the upstream gateway
        prints with ``--json-ready``. That line can be lost: the vendored llama-cpp
        runtime redirects the whole process's stdout to ``/dev/null`` while it loads
        the embedding model, and on a restored home the model is already present, so
        the load overlaps the print. When the dashboard is answering on its port but
        the line has not arrived, the gateway is up and we mint the token through
        the upstream CLI instead of waiting for output that will never come.
        """
        stdout = process.stdout
        if stdout is None:
            raise GatewayError("KiroCrew stdout is required for readiness discovery.")
        lines: queue.Queue[str | None] = queue.Queue()
        diagnostic_codes: list[str] = []
        exception_types: list[str] = []
        tail: deque[str] = deque(maxlen=5)

        def read_lines() -> None:
            for line in stdout:
                code = _classify_startup_line(line)
                if code is not None:
                    diagnostic_codes.append(code)
                exception_type = _classify_exception_type(line)
                if exception_type is not None:
                    exception_types.append(exception_type)
                tail.append(line.strip())
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()
        deadline = self._monotonic() + timeout_seconds
        next_probe = self._monotonic() + _HEALTH_PROBE_INTERVAL_SECONDS
        healthy_since: float | None = None
        while self._monotonic() < deadline:
            if process.poll() is not None:
                reader.join(timeout=0.2)
                _LOGGER.error(
                    "KiroCrew exited before readiness. Output tail: %s",
                    " | ".join(tail),
                )
                raise GatewayExitedError(
                    "KiroCrew exited before readiness.",
                    _best_diagnostic(diagnostic_codes),
                    _last_exception_type(exception_types),
                )
            now = self._monotonic()
            if now >= next_probe:
                next_probe = now + _HEALTH_PROBE_INTERVAL_SECONDS
                if self._health_probe(_DASHBOARD_PORT, _HEALTH_PROBE_INTERVAL_SECONDS):
                    healthy_since = now if healthy_since is None else healthy_since
                else:
                    healthy_since = None
                if healthy_since is not None and now - healthy_since >= _READY_LINE_GRACE_SECONDS:
                    token = self._mint_token_without_ready_line(environment)
                    if token is not None:
                        _LOGGER.warning(
                            "KiroCrew is serving on port %d but never printed its readiness "
                            "line; minted the dashboard token through the CLI instead.",
                            _DASHBOARD_PORT,
                        )
                        return GatewayReady(process.pid, _DASHBOARD_PORT, self._state_home), token
            try:
                line = lines.get(timeout=min(0.1, max(deadline - self._monotonic(), 0.001)))
            except queue.Empty:
                continue
            if line is None:
                _LOGGER.error(
                    "KiroCrew closed stdout before readiness. Output tail: %s",
                    " | ".join(tail),
                )
                raise GatewayExitedError(
                    "KiroCrew closed stdout before readiness.",
                    _best_diagnostic(diagnostic_codes),
                    _last_exception_type(exception_types),
                )
            if not line.startswith(_READY_PREFIX):
                continue
            try:
                value = json.loads(line.removeprefix(_READY_PREFIX))
                if not isinstance(value, dict):
                    raise ValueError
                port = value["port"]
                token = value["token"]
                pid = value["pid"]
                home = value["home"]
                if (
                    type(port) is not int
                    or type(pid) is not int
                    or not isinstance(token, str)
                    or not token
                    or not isinstance(home, str)
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise GatewayError("KiroCrew emitted invalid readiness metadata.") from error
            return GatewayReady(pid, port, Path(home)), token
        _LOGGER.error("KiroCrew readiness timed out. Output tail: %s", " | ".join(tail))
        raise GatewayError("KiroCrew readiness timed out.")

    def _mint_token_without_ready_line(self, environment: Mapping[str, str]) -> str | None:
        """Ask the upstream CLI for a dashboard token; ``None`` when it is not ready yet."""
        try:
            output = self._token_command(self._executable, environment, 10.0)
            return _parse_token_output(output)
        except (subprocess.SubprocessError, OSError, GatewayError) as error:
            _LOGGER.info("Dashboard token mint not possible yet: %s", error)
            return None

    def _terminate_process(self, process: ProcessHandle, grace_seconds: float) -> None:
        if process.poll() is not None:
            return
        os.killpg(process.pid, signal.SIGTERM)
        deadline = self._monotonic() + grace_seconds
        while process.poll() is None and self._monotonic() < deadline:
            self._sleep(min(0.05, max(deadline - self._monotonic(), 0.0)))
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=1.0)


_DIAGNOSTIC_PATTERNS: Final = (
    ("SANDBOX_UNAVAILABLE", ("allow_unsandboxed", "user namespace", "sandbox backend unavailable")),
    ("PERMISSION_DENIED", ("permission denied",)),
    ("READ_ONLY_FILESYSTEM", ("read-only file system",)),
    ("STORAGE_EXHAUSTED", ("no space left on device",)),
    (
        "PERSISTENCE_UNAVAILABLE",
        ("cannot persist state", "persistence preflight failed"),
    ),
    ("DATABASE_IO_ERROR", ("disk i/o error",)),
    ("DATABASE_LOCKED", ("database is locked", "database table is locked")),
    ("DATABASE_UNOPENABLE", ("unable to open database file",)),
    ("DATABASE_READ_ONLY", ("attempt to write a readonly database",)),
    ("PORT_IN_USE", ("address already in use",)),
    (
        "NODE_UNAVAILABLE",
        ("node.js is required", "node installation failed", "could not install node"),
    ),
    ("EXECUTABLE_MISSING", ("filenotfounderror", "command not found")),
    ("UPSTREAM_IMPORT_ERROR", ("modulenotfounderror", "importerror:")),
    ("SANDBOX_WARMUP_FAILED", ("in warm_backend",)),
    ("SERVICE_INITIALIZATION_FAILED", ("in _init_services",)),
    ("EMBEDDING_INITIALIZATION_FAILED", ("in _start_embeddings",)),
    ("MCP_GATEWAY_INITIALIZATION_FAILED", ("in _init_mcp_gateway",)),
    ("CRON_INITIALIZATION_FAILED", ("in _init_cron",)),
    ("HEARTBEAT_INITIALIZATION_FAILED", ("in _init_heartbeat",)),
    (
        "DASHBOARD_INITIALIZATION_FAILED",
        ("in _init_dashboard", "in start_dashboard"),
    ),
    ("UPSTREAM_EXCEPTION", ("traceback (most recent call last)",)),
)
_DIAGNOSTIC_PRIORITY: Final = tuple(code for code, _patterns in _DIAGNOSTIC_PATTERNS)
_EXCEPTION_TYPE_PATTERN: Final = re.compile(
    r"^(?P<type>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,4}"
    r"(?:Error|Exception|Exit|Interrupt|Warning))(?::|$)"
)
_EXCEPTION_TYPE_MAX_LENGTH: Final = 64


def _classify_exception_type(line: str) -> str | None:
    """Return only a structurally validated exception class path, never message text.

    Traceback terminators are formatted as ``<dotted.ClassName>: <message>``. Only the
    class path is extracted, and solely when it matches a strict dotted-identifier
    shape, so runtime data present in the message can never be captured or persisted.
    """
    match = _EXCEPTION_TYPE_PATTERN.match(line.strip())
    if match is None:
        return None
    exception_type = match.group("type")
    return exception_type if len(exception_type) <= _EXCEPTION_TYPE_MAX_LENGTH else None


def _classify_startup_line(line: str) -> str | None:
    normalized = line.casefold()
    for code, patterns in _DIAGNOSTIC_PATTERNS:
        if any(pattern in normalized for pattern in patterns):
            return code
    return None


def _best_diagnostic(codes: list[str]) -> str:
    for candidate in _DIAGNOSTIC_PRIORITY:
        if candidate in codes:
            return candidate
    return "UNKNOWN"


def _last_exception_type(exception_types: list[str]) -> str:
    """Return the most recent validated exception class, which is the fatal one."""
    return exception_types[-1] if exception_types else "UNKNOWN"


def _start_process(command: list[str], env: Mapping[str, str]) -> ProcessHandle:
    process = subprocess.Popen(  # noqa: S603  # nosec B603
        command,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    return cast(ProcessHandle, process)


def _run_token_command(executable: str, env: Mapping[str, str], timeout: float) -> str:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [executable, "token"],
        env=dict(env),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def _probe_dashboard_health(port: int, timeout: float) -> bool:
    """Whether the upstream dashboard answers its unauthenticated liveness route."""
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # nosec B310
            return bool(200 <= response.status < 300)
    except (OSError, http.client.HTTPException, ValueError):
        return False


def _parse_token_output(output: str) -> str:
    for candidate in output.split():
        parsed = urlsplit(candidate)
        values = parse_qs(parsed.query).get("token")
        if values and values[0]:
            return values[0]
    match = _TOKEN_PATTERN.search(output)
    if match is None:
        raise GatewayError("KiroCrew token renewal returned no dashboard token.")
    return match.group(1)
