"""The gateway fork server.

The child side runs only after a real fork, which coverage cannot follow, so it
is driven in-process with the process-level calls replaced. The server loop,
the client and the exit-status relay run for real over a Unix socket, with
`os.fork` and `os.waitpid` replaced so no process is actually forked.
"""

from __future__ import annotations

import io
import json
import os
import selectors
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from kirocrew_agentcore_runtime import forkserver as fs


@pytest.fixture
def short_dir() -> Iterator[Path]:
    # macOS limits a Unix socket path to 104 bytes; pytest's tmp_path is longer.
    path = Path(tempfile.mkdtemp(dir="/tmp", prefix="fs-"))  # nosec B108
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_preimport_counts_imports_and_skips_comments_and_failures() -> None:
    assert fs.preimport(["json", "", "# comment", "no_such_module_xyz", " os "]) == (2, 1)


def test_reseed_mixes_fresh_entropy(monkeypatch: pytest.MonkeyPatch) -> None:
    added: list[tuple[bytes, float]] = []
    monkeypatch.setattr("ssl.RAND_add", lambda data, entropy: added.append((data, entropy)))
    fs._reseed()
    assert len(added) == 1 and len(added[0][0]) == 64


def test_entry_point_is_the_console_script_main(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = type(sys)("kiro_crew._bootstrap")
    fake.main = lambda: 7  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiro_crew._bootstrap", fake)
    assert fs._entry_point()() == 7


class _ChildCalls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, result: object) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.environ: dict[str, str] = {"OLD": "1"}
        for name in ("setsid", "dup2", "close", "chdir"):
            monkeypatch.setattr(os, name, self._record(name))

        def open_(*args: object) -> int:
            self.calls.append(("open", args))
            return 99

        monkeypatch.setattr(os, "open", open_)
        monkeypatch.setattr(os, "fdopen", lambda *a, **k: io.StringIO())
        monkeypatch.setattr(os, "environ", self.environ)
        monkeypatch.setattr(sys, "argv", ["pytest"])
        monkeypatch.setattr(sys, "stdout", sys.stdout)
        monkeypatch.setattr(sys, "stderr", sys.stderr)
        monkeypatch.setattr(fs, "_reseed", lambda: self.calls.append(("reseed", ())))

        def entry() -> object:
            self.argv = list(sys.argv)
            if isinstance(result, BaseException):
                raise result
            return result

        monkeypatch.setattr(fs, "_entry_point", lambda: entry)

    def _record(self, name: str) -> Any:
        return lambda *args: self.calls.append((name, args))


@pytest.mark.parametrize(
    ("result", "status"),
    [(None, 0), (3, 3), ("bad", 1), (SystemExit(4), 4), (SystemExit(None), 0)],
)
def test_the_child_becomes_the_gateway(
    monkeypatch: pytest.MonkeyPatch, result: object, status: int
) -> None:
    child = _ChildCalls(monkeypatch, result)
    request = {"argv": ["kirocrew", "gateway"], "env": {"HOME": "/home/u"}, "cwd": "/w"}
    assert fs._run_child(request, 11) == status
    names = [name for name, _ in child.calls]
    assert names[0] == "setsid" and "reseed" in names
    assert ("dup2", (11, 1)) in child.calls and ("dup2", (11, 2)) in child.calls
    assert ("chdir", ("/w",)) in child.calls
    assert child.environ == {"HOME": "/home/u"}
    assert child.argv == ["kirocrew", "gateway"]


def _serve_in_thread(path: Path, monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    done = threading.Event()
    read_fd, write_fd = os.pipe()
    thread = threading.Thread(
        target=fs.serve,
        args=(path,),
        kwargs={"ready_fd": write_fd, "stop": done.is_set},
        daemon=True,
    )
    monkeypatch.setattr(fs, "_REAP_INTERVAL", 0.02)
    thread.start()
    assert os.read(read_fd, 16) == b"ready\n"
    os.close(read_fd)
    return done


def test_a_fork_request_returns_the_pid_and_later_the_exit_status(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exited = threading.Event()
    monkeypatch.setattr(os, "fork", lambda: 4242)

    def waitpid(pid: int, _options: int) -> tuple[int, int]:
        assert pid == 4242
        return (pid, 9) if exited.is_set() else (0, 0)  # 9 = killed by SIGKILL

    monkeypatch.setattr(os, "waitpid", waitpid)
    path = short_dir / "s"
    done = _serve_in_thread(path, monkeypatch)
    try:
        process = fs.ForkServerClient(path).start(["kirocrew"], {"A": "1"}, "/")
        assert process is not None and process.pid == 4242
        assert process.poll() is None
        exited.set()
        assert process.wait(5) == -signal.SIGKILL
        assert process.poll() == -signal.SIGKILL
    finally:
        done.set()


def test_a_child_already_reaped_reports_success(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fork", lambda: 4243)

    def waitpid(_pid: int, _options: int) -> tuple[int, int]:
        raise ChildProcessError

    monkeypatch.setattr(os, "waitpid", waitpid)
    path = short_dir / "s"
    done = _serve_in_thread(path, monkeypatch)
    try:
        process = fs.ForkServerClient(path).start(["kirocrew"], {}, "/")
        assert process is not None and process.wait(5) == 0
    finally:
        done.set()


def test_a_malformed_request_is_rejected_without_forking(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fork", lambda: pytest.fail("must not fork"))
    path = short_dir / "s"
    done = _serve_in_thread(path, monkeypatch)
    try:
        for body, fds in ((b"not json", True), (b"[]", True), (b"{}", False)):
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.connect(str(path))
            read_fd, write_fd = os.pipe()
            socket.send_fds(connection, [body], [write_fd] if fds else [])
            connection.settimeout(5)
            assert connection.recv(64) == b""  # closed without a reply
            connection.close()
            os.close(read_fd)
            os.close(write_fd)
    finally:
        done.set()


def test_the_fork_child_runs_the_gateway_and_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = socket.socketpair()
    read_fd, write_fd = os.pipe()
    socket.send_fds(left, [b'{"argv": [], "env": {}, "cwd": "/"}'], [write_fd])
    monkeypatch.setattr(os, "fork", lambda: 0)
    monkeypatch.setattr(fs, "_run_child", lambda request, fd: 5)

    class Exited(BaseException):
        pass

    def exit_(status: int) -> None:
        raise Exited(status)

    monkeypatch.setattr(os, "_exit", exit_)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    selector = selectors.DefaultSelector()
    with pytest.raises(Exited) as raised:
        fs._handle(right, listener, selector)
    assert raised.value.args == (5,)
    left.close()
    os.close(read_fd)


def test_an_unreachable_reaped_connection_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = socket.socketpair()
    left.close()
    monkeypatch.setattr(os, "waitpid", lambda pid, options: (pid, 0))
    children = {77: right}
    fs._reap(children)
    assert children == {}


def test_a_vanished_server_is_judged_by_the_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    def judged(kill_error: type[BaseException] | None) -> int | None:
        left, right = socket.socketpair()
        left.close()

        def kill(_pid: int, _signal: int) -> None:
            if kill_error is not None:
                raise kill_error

        monkeypatch.setattr(os, "kill", kill)
        return fs.ForkedProcess(right, 99, io.StringIO()).poll()

    assert judged(ProcessLookupError) == -signal.SIGKILL
    assert judged(PermissionError) is None
    assert judged(None) is None


def test_a_broken_connection_counts_as_a_vanished_server(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def setblocking(self, _flag: bool) -> None:
            pass

        def recv(self, _size: int) -> bytes:
            raise ConnectionResetError

        def close(self) -> None:
            pass

    monkeypatch.setattr(os, "kill", lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError))
    process = fs.ForkedProcess(Broken(), 5, io.StringIO())  # type: ignore[arg-type]
    assert process.poll() == -signal.SIGKILL


def test_a_partial_status_line_waits_for_the_rest() -> None:
    left, right = socket.socketpair()
    process = fs.ForkedProcess(right, 5, io.StringIO())
    left.sendall(b'{"exit"')
    time.sleep(0.05)
    assert process.poll() is None
    left.sendall(b": 2}\n")
    assert process.wait(5) == 2
    left.close()


def test_wait_times_out() -> None:
    left, right = socket.socketpair()
    with pytest.raises(TimeoutError):
        fs.ForkedProcess(right, 5, io.StringIO()).wait(0.05)
    left.close()


def test_the_client_falls_back_when_the_server_is_missing_or_broken(short_dir: Path) -> None:
    assert fs.ForkServerClient(short_dir / "missing").start(["x"], {}, "/") is None
    path = short_dir / "s"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)

    def close_at_once() -> None:
        connection, _ = listener.accept()
        connection.recv(4096)
        connection.close()

    threading.Thread(target=close_at_once, daemon=True).start()
    assert fs.ForkServerClient(path, timeout=5).start(["x"], {}, "/") is None
    listener.close()


def test_private_directory_rejects_a_shared_one(short_dir: Path) -> None:
    private = short_dir / "p"
    fs._private_directory(private)
    assert (private.stat().st_mode & 0o777) == 0o700
    private.chmod(0o755)
    with pytest.raises(PermissionError):
        fs._private_directory(private)
    private.rmdir()


def _remember(seen: list[list[str]], command: list[str]) -> str:
    seen.append(command)
    return "plain"


class _FakePopen:
    def __init__(self, reply: bytes | None) -> None:
        self.reply = reply
        self.calls: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> object:
        self.calls.append({"argv": argv, **kwargs})
        write_fd = int(argv[-1])
        if self.reply is not None:
            os.write(write_fd, self.reply)
        return object()


def _server(
    short_dir: Path, popen: _FakePopen, *, environment: dict[str, str] | None = None
) -> tuple[fs.ForkServer, list[list[str]]]:
    modules = short_dir / "modules.txt"
    modules.write_text("json\n")
    fallbacks: list[list[str]] = []
    server = fs.ForkServer(
        environment or {"AWS_SESSION_TOKEN": "secret", "PATH": "/bin"},
        socket_path=short_dir / "sub" / "s",
        modules=modules,
        fallback=lambda command, env: _remember(fallbacks, command),
        ready_timeout=2,
        popen=popen,
    )
    return server, fallbacks


def test_launch_starts_a_private_server_without_credentials(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "_SERVER_HOME", short_dir / "home")
    popen = _FakePopen(b"ready\n")
    server, _ = _server(short_dir, popen)
    server.launch()
    server.launch()  # once only
    assert len(popen.calls) == 1
    call = popen.calls[0]
    assert call["argv"][1:3] == ["-m", "kirocrew_agentcore_runtime.forkserver"]
    assert "AWS_SESSION_TOKEN" not in call["env"]
    assert call["env"]["HOME"] == str(short_dir / "home")
    assert server.wait_ready() and server.wait_ready()


def test_a_server_that_never_reports_ready_falls_back(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "_SERVER_HOME", short_dir / "home")
    server, fallbacks = _server(short_dir, _FakePopen(None))
    server.launch()
    assert server.wait_ready(timeout=0.05) is False
    assert server.wait_ready() is False
    assert server.start_gateway(["kirocrew"], {}) == "plain"
    assert fallbacks == [["kirocrew"]] and server.forked == 0


def test_a_forked_gateway_is_counted(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fs, "_SERVER_HOME", short_dir / "home")
    server, fallbacks = _server(short_dir, _FakePopen(b"ready\n"))
    server.launch()
    forked = type("Forked", (), {"pid": 7})()
    monkeypatch.setattr(fs.ForkServerClient, "start", lambda self, c, e, cwd: forked)
    assert server.start_gateway(["kirocrew"], {}) is forked
    assert server.forked == 1 and fallbacks == []


def test_a_disabled_or_unprivate_server_never_launches(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    popen = _FakePopen(b"ready\n")
    disabled, _ = _server(short_dir, popen, environment={"KIROCREW_FORKSERVER": "0"})
    disabled.launch()
    assert popen.calls == [] and disabled.wait_ready() is False
    shared = short_dir / "shared"
    shared.mkdir(mode=0o755)
    shared.chmod(0o755)
    monkeypatch.setattr(fs, "_SERVER_HOME", shared)
    server, _ = _server(short_dir, popen)
    server.launch()
    assert popen.calls == []
    shared.rmdir()
    (short_dir / "sub").rmdir()


def test_a_ready_server_that_refuses_falls_back(
    short_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs, "_SERVER_HOME", short_dir / "home")
    server, fallbacks = _server(short_dir, _FakePopen(b"ready\n"))
    server.launch()
    monkeypatch.setattr(fs.ForkServerClient, "start", lambda self, c, e, cwd: None)
    assert server.start_gateway(["kirocrew"], {}) == "plain"
    assert fallbacks == [["kirocrew"]] and server.forked == 0


def test_serve_without_a_ready_descriptor(short_dir: Path) -> None:
    calls = iter([False, True])
    fs.serve(short_dir / "s", stop=lambda: next(calls))
    assert (short_dir / "s").exists()


def test_main_imports_then_serves(short_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[tuple[Path, int | None]] = []
    monkeypatch.setattr(fs, "serve", lambda path, ready_fd=None: served.append((path, ready_fd)))
    modules = short_dir / "m.txt"
    modules.write_text("json\n")
    fs.main([str(short_dir / "s"), str(modules), "9"])
    fs.main([str(short_dir / "s"), str(short_dir / "none.txt")])
    monkeypatch.setattr(sys, "argv", ["forkserver", str(short_dir / "s")])
    fs.main()
    assert served == [(short_dir / "s", 9), (short_dir / "s", None), (short_dir / "s", None)]
    assert json.loads('{"ok": true}')


def test_rss_is_read_from_proc_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "read_text", lambda self: "Name: x\nVmRSS:\t  204800 kB\n")
    assert fs._rss_mib() == 200

    def missing(self: Path) -> str:
        raise FileNotFoundError

    monkeypatch.setattr(Path, "read_text", missing)
    assert fs._rss_mib() == 0
    monkeypatch.setattr(Path, "read_text", lambda self: "Name: x\n")
    assert fs._rss_mib() == 0
