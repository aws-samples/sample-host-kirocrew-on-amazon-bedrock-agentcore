"""Start the KiroCrew gateway by forking a process that has already imported it.

The gateway imports about 5,600 modules before it can serve; measured in this
image that is ~8 of the ~10 seconds a gateway takes to start, and on Runtime V2
every one of those files is a slow first read after a snapshot restore. A
fork server imports them once -- while the workspace is still restoring on V1,
or before the snapshot on V2 -- and forks a gateway that starts with them
already in memory (2.3 s instead of 10 s in the same image).

The server is single-threaded on purpose: a fork copies only the calling
thread, so a server with other threads could hand a child a lock that nobody
will release. It accepts one request per connection: a JSON line with argv,
environment and working directory, plus the child's stdout passed as a file
descriptor. It answers with the child's pid, and later with its exit status on
the same connection, since only the server can reap its children.

Values made before a fork are shared by every child, and on V2 by every session
restored from the snapshot. The child therefore reseeds ``random`` and mixes
fresh kernel entropy into OpenSSL before it runs the gateway.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import os
import selectors
import signal
import socket
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import IO, Final, cast

_LOGGER = logging.getLogger(__name__)

DEFAULT_MODULES: Final = Path("/opt/app/forkserver-modules.txt")
#: Requests are small JSON lines; a whole environment fits comfortably.
_MAX_REQUEST: Final = 256 * 1024
_REAP_INTERVAL: Final = 0.5
_READY: Final = b"ready\n"


def preimport(names: Iterable[str]) -> tuple[int, int]:
    """Import every module that can be imported; return (imported, failed)."""
    imported = failed = 0
    for name in names:
        name = name.strip()
        if not name or name.startswith("#"):
            continue
        try:
            importlib.import_module(name)
            imported += 1
        except BaseException:
            failed += 1
    return imported, failed


def _entry_point() -> Callable[[], object]:
    # The same entry point the `kirocrew` console script calls.
    from kiro_crew._bootstrap import main

    return cast(Callable[[], object], main)


def _reseed() -> None:
    import random
    import ssl

    random.seed()
    ssl.RAND_add(os.urandom(64), 64.0)


def _run_child(request: Mapping[str, object], stdout_fd: int) -> int:
    """Turn this forked process into the gateway; return its exit status."""
    os.setsid()
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.dup2(stdout_fd, 1)
    os.dup2(stdout_fd, 2)
    os.close(devnull)
    os.close(stdout_fd)
    sys.stdout = os.fdopen(1, "w", buffering=1, closefd=False)
    sys.stderr = os.fdopen(2, "w", buffering=1, closefd=False)
    environment = cast(Mapping[str, str], request["env"])
    os.environ.clear()
    os.environ.update(environment)
    os.chdir(cast(str, request["cwd"]))
    sys.argv = list(cast(list[str], request["argv"]))
    _reseed()
    try:
        result = _entry_point()()
    except SystemExit as exit_:
        result = exit_.code
    sys.stdout.flush()
    sys.stderr.flush()
    if result is None:
        return 0
    return result if isinstance(result, int) else 1


def serve(
    socket_path: Path, *, ready_fd: int | None = None, stop: Callable[[], bool] = lambda: False
) -> None:
    """Accept fork requests forever. The caller has already imported the modules."""
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    socket_path.chmod(0o600)
    listener.listen(16)
    if ready_fd is not None:
        os.write(ready_fd, _READY)
        os.close(ready_fd)
    selector = selectors.DefaultSelector()
    selector.register(listener, selectors.EVENT_READ)
    children: dict[int, socket.socket] = {}
    while not stop():
        if selector.select(_REAP_INTERVAL):
            connection, _address = listener.accept()
            pid = _handle(connection, listener, selector)
            if pid is None:
                connection.close()
            else:
                children[pid] = connection
        _reap(children)


def _handle(
    connection: socket.socket, listener: socket.socket, selector: selectors.BaseSelector
) -> int | None:
    connection.settimeout(5.0)
    try:
        data, fds, _flags, _address = socket.recv_fds(connection, _MAX_REQUEST, 1)
        request = json.loads(data.decode())
        if not fds or not isinstance(request, dict):
            raise ValueError("A request needs a JSON body and one stdout descriptor.")
    except (OSError, ValueError) as error:
        _LOGGER.warning("Rejected a fork request: %s", error)
        return None
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            selector.close()
            listener.close()
            connection.close()
            status = _run_child(request, fds[0])
        finally:
            os._exit(status)
    os.close(fds[0])
    connection.sendall(json.dumps({"pid": pid}).encode() + b"\n")
    return pid


def _reap(children: dict[int, socket.socket]) -> None:
    for pid in list(children):
        try:
            done, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            done, status = pid, 0
        if done == 0:
            continue
        connection = children.pop(pid)
        with contextlib.suppress(OSError):
            code = os.waitstatus_to_exitcode(status)
            connection.sendall(json.dumps({"exit": code}).encode() + b"\n")
        connection.close()


class ForkedProcess:
    """A gateway started by the fork server, with the subprocess handle's shape."""

    def __init__(
        self, connection: socket.socket, pid: int, stdout: IO[str], *, buffered: bytes = b""
    ) -> None:
        self.pid = pid
        self.stdout: IO[str] | None = stdout
        self.returncode: int | None = None
        self._connection = connection
        self._buffer = buffered

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        if b"\n" not in self._buffer:
            self._connection.setblocking(False)
            try:
                chunk = self._connection.recv(4096)
            except BlockingIOError:
                return None
            except OSError:
                chunk = b""
            if not chunk:
                return self._judge_by_pid()
            self._buffer += chunk
        if b"\n" in self._buffer:
            line = self._buffer.split(b"\n", 1)[0]
            self.returncode = int(json.loads(line.decode())["exit"])
            self._connection.close()
        return self.returncode

    def _judge_by_pid(self) -> int | None:
        """The server went away without reporting: decide from the pid alone."""
        self._connection.close()
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = -signal.SIGKILL
        except PermissionError:
            return None
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Process {self.pid} still running.")
            time.sleep(0.05)
        return code


class ForkServerClient:
    """Asks a running fork server for a gateway; `None` from ``start`` means use Popen."""

    def __init__(self, socket_path: Path, *, timeout: float = 10.0) -> None:
        self._socket_path = socket_path
        self._timeout = timeout

    def available(self) -> bool:
        return self._socket_path.exists()

    def start(self, command: list[str], env: Mapping[str, str], cwd: str) -> ForkedProcess | None:
        if not self.available():
            return None
        read_fd, write_fd = os.pipe()
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.settimeout(self._timeout)
            connection.connect(str(self._socket_path))
            body = json.dumps({"argv": command, "env": dict(env), "cwd": cwd}).encode()
            socket.send_fds(connection, [body], [write_fd])
            reply = b""
            while b"\n" not in reply:
                chunk = connection.recv(4096)
                if not chunk:
                    raise OSError("The fork server closed the connection.")
                reply += chunk
            # A gateway that exits at once can have its exit status arrive in
            # the same read as its pid; the rest belongs to the process handle.
            line, rest = reply.split(b"\n", 1)
            pid = int(json.loads(line.decode())["pid"])
        except (OSError, ValueError, KeyError) as error:
            _LOGGER.warning("Fork server unavailable, starting the gateway directly: %s", error)
            connection.close()
            os.close(read_fd)
            os.close(write_fd)
            return None
        os.close(write_fd)
        stdout = os.fdopen(read_fd, "r", encoding="utf-8", errors="replace")
        return ForkedProcess(connection, pid, stdout, buffered=rest)


#: A directory only this user can enter holds the socket and the server's home,
#: so nothing else on the machine can reach or replace the socket.
_PRIVATE_DIR: Final = Path(tempfile.gettempdir()) / f"kirocrew-forkserver-{os.getuid()}"
DEFAULT_SOCKET: Final = _PRIVATE_DIR / "server.sock"
#: The server imports with its own scratch home: before a restore (V1) or the
#: snapshot (V2) the user's home is empty or about to be replaced, and nothing
#: the imports might write belongs in it.
_SERVER_HOME: Final = _PRIVATE_DIR / "home"


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError(f"{path} is not a private directory of this user.")


_CREDENTIAL_KEYS: Final = frozenset(
    {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)


class ForkServer:
    """Owns the fork server process and starts gateways through it.

    ``start_gateway`` has the supervisor's ``ProcessFactory`` shape. It waits for
    a server that is still importing, and falls back to a plain subprocess when
    the server is disabled, failed or does not answer, so a broken fork server
    costs start-up time, never a start.
    """

    def __init__(
        self,
        environment: Mapping[str, str],
        *,
        socket_path: Path = DEFAULT_SOCKET,
        modules: Path = DEFAULT_MODULES,
        fallback: Callable[[list[str], Mapping[str, str]], object],
        ready_timeout: float = 30.0,
        popen: Callable[..., object] | None = None,
    ) -> None:
        self._environment = environment
        self._socket_path = socket_path
        self._modules = modules
        self._fallback = fallback
        self._ready_timeout = ready_timeout
        self._popen = popen
        self._process: object | None = None
        self._ready_fd: int | None = None
        self._ready = False
        self._started_at = 0.0
        self.forked = 0

    @property
    def enabled(self) -> bool:
        return self._environment.get("KIROCREW_FORKSERVER", "1") != "0" and self._modules.is_file()

    def launch(self) -> None:
        """Start the server in the background; it imports while the caller restores."""
        if not self.enabled or self._process is not None:
            return
        import subprocess  # nosec B404

        popen = self._popen or subprocess.Popen
        try:
            _private_directory(self._socket_path.parent)
            _private_directory(_SERVER_HOME)
        except OSError as error:
            _LOGGER.warning("Gateway fork server disabled: %s", error)
            return
        environment = {k: v for k, v in self._environment.items() if k not in _CREDENTIAL_KEYS}
        environment.update(
            {"HOME": str(_SERVER_HOME), "KIROCREW_HOME": str(_SERVER_HOME / ".kiro" / "crew")}
        )
        read_fd, write_fd = os.pipe()
        self._started_at = time.monotonic()
        self._process = popen(  # nosec B603 - fixed argv, no shell
            [
                sys.executable,
                "-m",
                "kirocrew_agentcore_runtime.forkserver",
                str(self._socket_path),
                str(self._modules),
                str(write_fd),
            ],
            env=environment,
            cwd="/",
            pass_fds=(write_fd,),
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        os.close(write_fd)
        self._ready_fd = read_fd

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until the server has finished importing; False if it never will."""
        if self._ready:
            return True
        if self._ready_fd is None:
            return False
        selector = selectors.DefaultSelector()
        selector.register(self._ready_fd, selectors.EVENT_READ)
        limit = self._ready_timeout if timeout is None else timeout
        events = selector.select(limit)
        selector.close()
        data = os.read(self._ready_fd, len(_READY)) if events else b""
        if data == _READY:
            self._ready = True
            _LOGGER.info(
                "Gateway fork server ready after %.1fs.", time.monotonic() - self._started_at
            )
        else:
            _LOGGER.warning("Gateway fork server did not become ready; starting gateways directly.")
        os.close(self._ready_fd)
        self._ready_fd = None
        return self._ready

    def start_gateway(self, command: list[str], env: Mapping[str, str]) -> object:
        if self.enabled and self.wait_ready():
            process = ForkServerClient(self._socket_path).start(command, env, str(Path.cwd()))
            if process is not None:
                self.forked += 1
                _LOGGER.info("Gateway started through the fork server (pid %d).", process.pid)
                return process
        return self._fallback(command, env)


def _rss_mib() -> int:
    """Resident memory of this process, from /proc where it exists (0 elsewhere)."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 0


def main(argv: list[str] | None = None) -> None:
    """``python -m kirocrew_agentcore_runtime.forkserver SOCKET [MODULES] [READY_FD]``."""
    args = sys.argv[1:] if argv is None else argv
    socket_path = Path(args[0])
    modules = Path(args[1]) if len(args) > 1 else DEFAULT_MODULES
    ready_fd = int(args[2]) if len(args) > 2 else None
    started = time.monotonic()
    names = modules.read_text(encoding="utf-8").splitlines() if modules.is_file() else []
    imported, failed = preimport(names)
    print(
        f"forkserver imported={imported} failed={failed} seconds={time.monotonic() - started:.1f}",
        file=sys.stderr,
        flush=True,
    )
    serve(socket_path, ready_fd=ready_fd)


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    main()
