from __future__ import annotations

import contextlib
import http.client
import http.cookies
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
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from .team_context import SyncResult, TeamContextSync
from .team_context import config_from_environment as team_context_config

_LOGGER = logging.getLogger(__name__)

_READY_PREFIX: Final = "KIROCREW_READY:"
# The browser tool's Chromium, staged read-only in the image (runtime/Dockerfile).
_IMAGE_CHROMIUM: Final = Path("/opt/kirocrew-browser/chromium")
_TOKEN_PATTERN: Final = re.compile(r"(?:^|[?&])token=([^&\s]+)")
_DEFAULT_TOKEN_TTL_SECONDS: Final = 19 * 60 * 60
_DASHBOARD_PORT: Final = 5476
_HEALTH_PROBE_INTERVAL_SECONDS: Final = 1.0
# On 0.13.1 the readiness line is not lost: upstream stopped the embedding
# model load from silencing stdout (kirodotdev/KiroCrew#35401). It arrives
# 4-6 s after the port answers. The old fallback waited 3 s after the port,
# then blocked in the upstream `kirocrew token` CLI for its 10-15 s start-up,
# ignoring the line that came in meanwhile: 24-32 s, against 11-15 s when the
# line beat the 3 s wait (22 live starts). So the CLI now runs in the
# background as soon as the gateway prints anything, and whichever of the two
# arrives first wins; the line is never ignored. The CLI needs the gateway's
# local secret, which exists only once it is serving, so an early attempt that
# runs ahead simply fails and the next one starts.
# The upstream gateway arms a loop-stall watchdog that dumps every thread and
# hard-exits when its event loop stays silent past this budget (upstream default
# 25s, maximum 300s). A checkpoint quiesces the gateway with SIGSTOP; a freeze
# longer than the budget therefore kills the gateway the moment it resumes. The
# snapshot phase is short now, but a large workspace can still exceed 25s of
# hashing, so the sandbox pins the budget at the upstream maximum.
_LOOP_STALL_EXIT_AFTER_SECONDS: Final = 300
# From KiroCrew 0.11 an install with no engine choice defaults to KAS, and that
# is the default here too: the image stages the KAS engine, and it signs in
# through Kiro Crew's own Settings, so the shell needs no Kiro sign-in of its
# own. The default is written to config.json only when no engine was chosen, so
# a user who picks Kiro CLI in Settings keeps that choice across restarts.
# Earlier images pinned kiro-cli (spelled "kiro") in the overlay.
_KIRO_CLI_ACP_BACKEND: Final = "kiro"
_DEFAULT_ACP_BACKEND: Final = "kas"
_CONFIG_FILENAME: Final = "config.json"
# Every open chat keeps its own kiro-cli process plus KiroCrew's MCP servers
# alive, about 0.7 GB each on this image, until the idle sweep ends it (upstream
# default 60 min). Ten chats opened within an hour fill the 8 GB microVM, which
# the platform then kills without a shutdown checkpoint, so idle chats are ended
# after 15 minutes. Reopening one resumes it from its saved history.
_IDLE_SESSION_TIMEOUT_SECONDS: Final = 900
_LOCAL_CONFIG_PINS: Final[tuple[tuple[str, str, object], ...]] = (
    ("dashboard", "loop_stall_exit_after_secs", _LOOP_STALL_EXIT_AFTER_SECONDS),
    # A user who picks KAS gets the engine staged in the image and Kiro Crew's
    # own sign-in, never kiro-cli's relay.
    ("agent", "kas_bundle", True),
    ("session", "timeout_secs", _IDLE_SESSION_TIMEOUT_SECONDS),
    # 0.13.0 Remote Control relays this Crew to the owner's other devices. A
    # sandbox belongs to exactly one Cognito subject and is reached only through
    # AgentCore, so the relay stays off. Re-pinned at every start, so a
    # `kirocrew remote-control on` run inside the sandbox never connects.
    ("tunnel", "relay_enabled", False),
)
#: Overlay keys earlier images pinned, removed so the user's choice in
#: config.json is no longer overridden.
_RETIRED_LOCAL_CONFIG_PINS: Final[tuple[tuple[str, str, object], ...]] = (
    ("agent", "acp_backend", _KIRO_CLI_ACP_BACKEND),
)
_LOCAL_CONFIG_FILENAME: Final = "config.local.json"
_MCP_STORE_FILENAME: Final = "mcp.json"
#: Startup budget for MCP servers before a session's first turn (milliseconds).
_KIRO_CLI_MCP_TIMEOUT_MS: Final = 60_000
_OUTPUT_TAIL_LINES: Final = 20


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


class SessionExchange(Protocol):
    def __call__(self, port: int, link_token: str, timeout: float, /) -> str | None: ...


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


def tool_token_file(environment: Mapping[str, str]) -> str:
    """Where the runtime keeps the current tool-Gateway bearer token.

    Under the container's temporary directory, never under the persisted
    workspace, so a checkpoint cannot carry the owner's token.
    """
    configured = environment.get("TOOL_GATEWAY_TOKEN_FILE", "").strip()
    if configured:
        return configured
    return str(Path(tempfile.gettempdir()) / "kirocrew-agentcore" / "tool-gateway.token")


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
        session_exchange: SessionExchange | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        effective_uid: Callable[[], int] = os.geteuid,
        sleep: Callable[[float], None] = time.sleep,
        state_root: Path | None = None,
        wal_probe: Callable[[Path], bool] = supports_sqlite_wal,
        chdir: Callable[[Path], None] = os.chdir,
        image_chromium: Path = _IMAGE_CHROMIUM,
    ) -> None:
        self._layout = layout
        self._image_chromium = image_chromium
        self._metadata = metadata
        self._executable = executable
        self._process_factory = process_factory or _start_process
        self._token_command = token_command or _run_token_command
        self._health_probe = health_probe or _probe_dashboard_health
        self._session_exchange = session_exchange or _exchange_link_for_session
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
        self._output_tail: deque[str] = deque(maxlen=_OUTPUT_TAIL_LINES)
        self._exit_reported = False

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

    @property
    def output_tail(self) -> tuple[str, ...]:
        """The most recent gateway output lines, kept for the whole process life."""
        return tuple(self._output_tail)

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
        self._scrub_session_leases(self._layout.home)
        self._link_image_chromium(self._state_home)
        self._seed_default_engine(self._state_home)
        self._pin_local_config(self._state_home)
        self._pin_agent_registry_mcp(self._state_home)
        self._pin_tool_gateway_mcp(self._state_home)
        self._pin_kiro_cli_mcp_timeouts(self._layout.home)
        self.sync_team_context()
        self._output_tail.clear()
        self._exit_reported = False
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
        token = self._as_session_token(token)
        self._token = DashboardToken(token, self._monotonic() + _DEFAULT_TOKEN_TTL_SECONDS)
        self._ready = True
        return ready

    def sync_team_context(self) -> SyncResult | None:
        """Mirror the team steering repository, when one is configured.

        Runs before the gateway starts so its first session already sees the
        team's steering, and again from the runtime's refresh loop. Never
        raises: a team repository that cannot be reached must not keep a
        sandbox from starting.
        """
        config = team_context_config(os.environ)
        if config is None:
            return None
        try:
            return TeamContextSync(self._layout.home, config).sync()
        except Exception:
            _LOGGER.warning("Team context sync failed unexpectedly.", exc_info=True)
            return None

    @staticmethod
    def _pin_agent_registry_mcp(state_home: Path) -> None:
        """Register the SigV4 Agent Registry bridge in KiroCrew's own MCP store."""
        endpoint = os.environ.get("AGENT_REGISTRY_MCP_ENDPOINT", "").strip()
        if not endpoint:
            return
        KiroCrewSupervisor._pin_mcp_server(
            state_home,
            "agent-registry",
            [
                "-m",
                "kirocrew_agentcore_runtime.sigv4_mcp_proxy",
                "--endpoint",
                endpoint,
                "--service",
                "agent-registry",
                "--region",
                os.environ.get("AGENT_REGISTRY_REGION", "us-east-1"),
            ],
        )

    @staticmethod
    def _pin_tool_gateway_mcp(state_home: Path) -> None:
        """Register the tool Gateway bridge, which signs as the sandbox owner.

        The bearer token lives in a container-local file the runtime refreshes
        from the persistence broker; it is never written under the persisted
        workspace, so a checkpoint cannot carry it.
        """
        endpoint = os.environ.get("TOOL_GATEWAY_URL", "").strip()
        if not endpoint:
            return
        KiroCrewSupervisor._pin_mcp_server(
            state_home,
            "tool-gateway",
            [
                "-m",
                "kirocrew_agentcore_runtime.bearer_mcp_proxy",
                "--endpoint",
                endpoint,
                "--token-file",
                tool_token_file(os.environ),
            ],
        )

    @staticmethod
    def _pin_kiro_cli_mcp_timeouts(home: Path) -> None:
        """Give MCP servers enough time to start before a session's first turn.

        kiro-cli waits ``mcp.initTimeout`` for every server before the first
        turn and leaves out any server that is not ready by then. On a 2-vCPU
        microVM KiroCrew's own Python servers (kirocrew-core, -cron, ...) can
        miss a short budget, so a fresh chat sometimes has none of their tools.
        Values already set higher are kept; other settings are left untouched.
        """
        path = home / ".kiro" / "settings" / "cli.json"
        document: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _LOGGER.warning("kiro-cli settings are unreadable; leaving them as is.")
                return
            if not isinstance(loaded, dict):
                _LOGGER.warning("kiro-cli settings are not an object; leaving them as is.")
                return
            document = cast(dict[str, object], loaded)
        changed = False
        for key in ("mcp.initTimeout", "mcp.noInteractiveTimeout"):
            current = document.get(key)
            if (
                isinstance(current, int)
                and not isinstance(current, bool)
                and current >= _KIRO_CLI_MCP_TIMEOUT_MS
            ):
                continue
            document[key] = _KIRO_CLI_MCP_TIMEOUT_MS
            changed = True
        if not changed:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _pin_mcp_server(state_home: Path, name: str, args: list[str]) -> None:
        """Pin one stdio MCP server in ``$KIROCREW_HOME/mcp.json``.

        KiroCrew reads user MCP servers from that file (not from
        ``config.local.json``), so the entry must live there for Kiro sessions
        to see it. Other servers in the file are left untouched.
        """
        path = state_home / _MCP_STORE_FILENAME
        document: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _LOGGER.warning("KiroCrew MCP store is unreadable; leaving it as is.")
                return
            if not isinstance(loaded, dict):
                _LOGGER.warning("KiroCrew MCP store is not an object; leaving it as is.")
                return
            document = cast(dict[str, object], loaded)
        servers = document.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        server_section = cast(dict[str, object], servers)
        server_section[name] = {
            "args": args,
            "command": "/usr/local/bin/python",
            "disabled": False,
        }
        document["mcpServers"] = server_section
        rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if path.is_file() and path.read_text(encoding="utf-8") == rendered:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _pin_local_config(state_home: Path) -> None:
        """Pin the gateway settings this image depends on through its local config overlay.

        ``config.local.json`` is deep-merged over ``config.json`` by the upstream
        loader and is never rewritten by its setup flow, so the pins survive
        upgrades and checkpoints. Pinned:

        * ``dashboard.loop_stall_exit_after_secs`` -- the upstream maximum, so a
          checkpoint freeze is not mistaken for a stalled event loop.
        * ``agent.kas_bundle = true`` -- a user who picks KAS runs the engine the
          image stages, signed in through Kiro Crew's own login.
        * ``session.timeout_secs`` -- idle chats end after 15 minutes so their
          processes do not exhaust the microVM's memory.

        Other keys the user placed there are preserved; an unreadable overlay is
        left alone rather than clobbered.
        """
        path = state_home / _LOCAL_CONFIG_FILENAME
        overlay: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _LOGGER.warning("Gateway local config overlay is unreadable; leaving it as is.")
                return
            if not isinstance(loaded, dict):
                _LOGGER.warning("Gateway local config overlay is not an object; leaving it as is.")
                return
            overlay = cast(dict[str, object], loaded)
        changed = False
        for section_name, key, value in _RETIRED_LOCAL_CONFIG_PINS:
            current = overlay.get(section_name)
            if isinstance(current, dict) and cast(dict[str, object], current).get(key) == value:
                del cast(dict[str, object], current)[key]
                changed = True
        for section_name, key, value in _LOCAL_CONFIG_PINS:
            current = overlay.get(section_name)
            if isinstance(current, dict):
                section = cast(dict[str, object], current)
                if section.get(key) == value:
                    continue
            else:
                section = {}
            section[key] = value
            overlay[section_name] = section
            changed = True
        if not changed:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(overlay, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        path.chmod(0o600)

    @staticmethod
    def _seed_default_engine(state_home: Path) -> None:
        """Choose the KAS engine in ``config.json`` when the user has not chosen one.

        Written to ``config.json``, not the overlay, so a later choice made in
        Settings is the one that sticks. While the overlay still carries the
        kiro-cli pin earlier images wrote, any engine in ``config.json`` was never
        in effect (upstream itself may have stored one there), so it is reset to
        the default once; the overlay pin is then retired and later choices are
        kept. An unreadable or non-object file is left alone.
        """
        path = state_home / _CONFIG_FILENAME
        document: dict[str, object] = {}
        if path.is_file():
            try:
                loaded = cast(object, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                _LOGGER.warning("Gateway config is unreadable; not seeding the agent engine.")
                return
            if not isinstance(loaded, dict):
                _LOGGER.warning("Gateway config is not an object; not seeding the agent engine.")
                return
            document = cast(dict[str, object], loaded)
        agent = document.get("agent")
        if (
            isinstance(agent, dict)
            and "acp_backend" in agent
            and not _overlay_pins_kiro_cli(state_home)
        ):
            return
        section = cast(dict[str, object], agent) if isinstance(agent, dict) else {}
        section["acp_backend"] = _DEFAULT_ACP_BACKEND
        document["agent"] = section
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _link_image_chromium(self, state_home: Path) -> None:
        """Point the browser tool's Chromium directory at the image's read-only copy.

        The checkpoint excludes the directory, so a restored home has none (or,
        from an older checkpoint, a downloaded copy, which is replaced). Never
        raises: without the link the browser tool downloads Chromium as before.
        """
        if not self._image_chromium.is_dir():
            return
        link = state_home / "browser-harness" / "chromium"
        try:
            if link.is_symlink() and link.readlink() == self._image_chromium:
                return
            if link.is_symlink() or link.is_file():
                link.unlink()
            elif link.is_dir():
                shutil.rmtree(link)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(self._image_chromium, target_is_directory=True)
        except OSError:
            _LOGGER.warning("Could not link the image's Chromium; the browser tool downloads it.")

    @staticmethod
    def _scrub_session_leases(home: Path) -> None:
        """Remove the chat session leases a checkpoint captured.

        The Kiro engine writes ``~/.kiro/session-leases/<session>.lease`` naming
        the pid that owns each chat and renews it while that process lives. A
        restored home brings back the previous container's leases. A new
        container numbers its processes from the start too, so a lease's pid can
        name an unrelated live process here, and the next message in that chat
        fails with "This session is loaded in another program, kirocrew (pid N)".
        Seen once on the dev deployment, right after a restore. No chat process
        exists yet when the gateway starts (the previous gateway's process
        group was terminated), so every lease on disk is stale.
        """
        leases = home / ".kiro" / "session-leases"
        if not leases.is_dir():
            return
        for candidate in leases.iterdir():
            if candidate.suffix == ".lease" and candidate.is_file():
                candidate.unlink(missing_ok=True)

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
            value = self._as_session_token(_parse_token_output(output))
            self._token = DashboardToken(value, self._monotonic() + _DEFAULT_TOKEN_TTL_SECONDS)
        return cast(DashboardToken, self._token).value

    def _as_session_token(self, link_token: str) -> str:
        """Trade the minted link token for the session token the cookie path accepts.

        Upstream 0.12 mints dashboard URLs as one-time LINK tokens (a 5-minute
        click window) and accepts only ``kind=session`` tokens in the
        ``mc_token_<port>`` cookie, which is how the adapter authenticates every
        forwarded request. A browser makes this trade by opening ``/?token=``
        once and keeping the cookie it is handed; the supervisor does the same
        over loopback, so the session is pinned to the same loopback peer the
        adapter uses. Gateways that hand back no session cookie keep the token
        they minted.
        """
        try:
            session = self._session_exchange(_DASHBOARD_PORT, link_token, 10.0)
        except (OSError, http.client.HTTPException, ValueError) as error:
            _LOGGER.warning("Dashboard session exchange failed: %s", type(error).__name__)
            return link_token
        return session or link_token

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
            if process is not None and not self._exit_reported:
                # Logged once per process: the exit code and the last output
                # lines are the only forensic record of why the gateway died
                # (its stdout is otherwise consumed silently after readiness).
                self._exit_reported = True
                _LOGGER.error(
                    "KiroCrew gateway exited with code %s. Output tail: %s",
                    process.poll(),
                    " | ".join(self._output_tail) or "<none>",
                )
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
        prints with ``--json-ready``. Older upstream releases could lose it: the
        vendored llama-cpp runtime pointed the whole process's stdout at
        ``/dev/null`` while it loaded the embedding model (fixed upstream in
        0.13.1). On 0.13.1 it is only late, 4-6 s after the port answers. So the
        token is also minted through the upstream CLI in the background, and
        whichever arrives first is used.
        """
        stdout = process.stdout
        if stdout is None:
            raise GatewayError("KiroCrew stdout is required for readiness discovery.")
        lines: queue.Queue[str | None] = queue.Queue()
        diagnostic_codes: list[str] = []
        exception_types: list[str] = []
        tail: deque[str] = deque(maxlen=5)

        output_tail = self._output_tail
        started = self._monotonic()
        # Where a slow start spends its time: before the first output line,
        # before the port answers, or minting the token afterwards.
        timing: dict[str, float] = {}
        line_count = [0]
        returned = [False]

        def read_lines() -> None:
            for line in stdout:
                line_count[0] += 1
                timing.setdefault("first_output", self._monotonic() - started)
                if line.startswith(_READY_PREFIX) and "ready_line" not in timing:
                    timing["ready_line"] = self._monotonic() - started
                    if returned[0]:
                        # Evidence for the upstream report: whether the line is
                        # lost or only late, and by how much.
                        _LOGGER.info(
                            "KiroCrew readiness line arrived after start returned, at %.1fs",
                            timing["ready_line"],
                        )
                output_tail.append(line.strip())
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
        # One background CLI mint at a time. Its result is used only once the
        # port has answered: a token from a gateway that is not serving yet is
        # not a readiness signal.
        minted: queue.Queue[str | None] = queue.Queue()
        minting = [False]
        early_token: list[str] = []

        def mint_in_background() -> None:
            minted.put(self._mint_token_without_ready_line(environment))

        def start_mint() -> None:
            minting[0] = True
            timing["mint_attempts"] = timing.get("mint_attempts", 0.0) + 1
            threading.Thread(target=mint_in_background, daemon=True).start()

        deadline = self._monotonic() + timeout_seconds
        next_probe = self._monotonic() + _HEALTH_PROBE_INTERVAL_SECONDS
        port_answered = False
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
                port_answered = self._health_probe(_DASHBOARD_PORT, _HEALTH_PROBE_INTERVAL_SECONDS)
                if port_answered:
                    timing.setdefault("port_answered", now - started)
            with contextlib.suppress(queue.Empty):
                result = minted.get_nowait()
                minting[0] = False
                if result is not None:
                    early_token[:] = [result]
            if early_token and port_answered:
                _LOGGER.warning(
                    "KiroCrew is serving on port %d but never printed its readiness "
                    "line; minted the dashboard token through the CLI instead.",
                    _DASHBOARD_PORT,
                )
                self._log_start_timing(started, timing, line_count[0])
                returned[0] = True
                return (
                    GatewayReady(process.pid, _DASHBOARD_PORT, self._state_home),
                    early_token[0],
                )
            if not minting[0] and not early_token and port_answered:
                start_mint()
            try:
                line = lines.get(timeout=min(0.1, max(deadline - self._monotonic(), 0.001)))
            except queue.Empty:
                # Output has paused with no readiness line yet: start the CLI
                # mint now rather than after the port answers, so its own
                # 10-15 s start-up overlaps the gateway's.
                if not minting[0] and not early_token and line_count[0] > 0:
                    start_mint()
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
            self._log_start_timing(started, timing, line_count[0])
            return GatewayReady(pid, port, Path(home)), token
        _LOGGER.error("KiroCrew readiness timed out. Output tail: %s", " | ".join(tail))
        raise GatewayError("KiroCrew readiness timed out.")

    def _log_start_timing(self, started: float, timing: Mapping[str, float], lines: int) -> None:
        def at(name: str) -> str:
            value = timing.get(name)
            return "never" if value is None else f"{value:.1f}s"

        _LOGGER.info(
            "KiroCrew start timing total=%.1fs first_output=%s output_lines=%d "
            "port_answered=%s ready_line=%s token_mint_attempts=%d",
            self._monotonic() - started,
            at("first_output"),
            lines,
            at("port_answered"),
            at("ready_line"),
            int(timing.get("mint_attempts", 0)),
        )

    def _mint_token_without_ready_line(self, environment: Mapping[str, str]) -> str | None:
        """Ask the upstream CLI for a dashboard token; ``None`` when it is not ready yet."""
        try:
            # 30 s, not 10: on a loaded box right after a large restore the CLI
            # itself takes longer than 10 s to start, and every attempt timed out.
            output = self._token_command(self._executable, environment, 30.0)
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


def _overlay_pins_kiro_cli(state_home: Path) -> bool:
    """Whether ``config.local.json`` still carries the kiro-cli pin earlier images wrote."""
    try:
        loaded = cast(
            object,
            json.loads((state_home / _LOCAL_CONFIG_FILENAME).read_text(encoding="utf-8")),
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    agent = cast(dict[str, object], loaded).get("agent")
    return isinstance(agent, dict) and agent.get("acp_backend") == _KIRO_CLI_ACP_BACKEND


def _exchange_link_for_session(port: int, link_token: str, timeout: float) -> str | None:
    """Open one authenticated route with ``?token=`` and return the session cookie it sets."""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        query = urllib.parse.urlencode({"token": link_token})
        connection.request("GET", f"/api/status?{query}", headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        response.read()
        cookie = http.cookies.SimpleCookie()
        for header in response.headers.get_all("Set-Cookie") or ():
            cookie.load(header)
        morsel = cookie.get(f"mc_token_{port}")
        return morsel.value if morsel is not None and morsel.value else None
    finally:
        connection.close()


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
