from __future__ import annotations

import importlib.util
import json
import logging
import subprocess  # nosec B404
import sys
from pathlib import Path

import pytest
from kirocrew_agentcore_runtime import page_pin

ROOT = Path(__file__).resolve().parents[2]


def test_expand_resolves_placeholders_globs_and_skips_missing(tmp_path: Path) -> None:
    (tmp_path / "lib-aarch64").mkdir()
    (tmp_path / "lib-aarch64" / "a.so").write_bytes(b"a")
    (tmp_path / "node-linux-arm64").mkdir()
    (tmp_path / "node-linux-arm64" / "node").write_bytes(b"n")
    (tmp_path / "tree" / "deep").mkdir(parents=True)
    (tmp_path / "tree" / "deep" / "x.js").write_bytes(b"x")
    lines = [
        "# comment",
        "",
        f"{tmp_path}/lib-{{machine}}/a.so",
        f"{tmp_path}/lib-{{machine}}/a.so",
        f"{tmp_path}/node-linux-{{node_arch}}/node",
        f"{tmp_path}/tree/**",
        f"{tmp_path}/missing.txt",
        f"{tmp_path}/tree",
    ]
    paths = page_pin.expand(lines, machine="aarch64")
    assert paths == [
        tmp_path / "lib-aarch64" / "a.so",
        tmp_path / "node-linux-arm64" / "node",
        tmp_path / "tree" / "deep" / "x.js",
    ]
    assert page_pin.expand([f"{tmp_path}/lib-{{machine}}/a.so"], machine="s390x") == []
    # The default machine is the host's own.
    assert page_pin.expand(["# only a comment"]) == []


def test_pin_maps_within_budget_and_skips_failures(tmp_path: Path) -> None:
    small = tmp_path / "small"
    small.write_bytes(b"x" * 10)
    big = tmp_path / "big"
    big.write_bytes(b"y" * 100)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    before = page_pin.pinned_count()
    report = page_pin.pin([small, empty, big, tmp_path / "gone"], budget_bytes=50)
    assert report.files == 1
    assert report.bytes == 10
    assert report.skipped == 2
    assert report.seconds >= 0
    assert page_pin.pinned_count() == before + 1


def test_pin_holds_no_descriptor_per_file(tmp_path: Path) -> None:
    files = []
    for index in range(64):
        path = tmp_path / f"f{index}"
        path.write_bytes(b"p" * 16)
        files.append(path)
    descriptors = Path("/dev/fd")
    before = len(list(descriptors.iterdir()))
    report = page_pin.pin(files)
    assert report.files == 64
    assert len(list(descriptors.iterdir())) <= before


def test_map_file_reports_failures() -> None:
    with pytest.raises(OSError):
        page_pin._map_file(-1, 16)


def test_pin_stops_at_the_deadline(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.write_bytes(b"x")
    second = tmp_path / "second"
    second.write_bytes(b"y")
    report = page_pin.pin([first, second], deadline_seconds=-1.0)
    assert report.files == 0
    assert report.skipped == 2


def test_pin_from_environment(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"z" * 4)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(f"{target}\n", encoding="utf-8")
    assert page_pin.pin_from_environment({}) is None
    assert page_pin.pin_from_environment({page_pin.PAGE_PIN_ENV: "0"}) is None
    with caplog.at_level(logging.WARNING):
        missing = page_pin.pin_from_environment(
            {page_pin.PAGE_PIN_ENV: "1", page_pin.MANIFEST_ENV: str(tmp_path / "nope")}
        )
    assert missing is None
    assert "unreadable" in caplog.text
    with caplog.at_level(logging.INFO):
        report = page_pin.pin_from_environment(
            {page_pin.PAGE_PIN_ENV: "1", page_pin.MANIFEST_ENV: str(manifest)}
        )
    assert report is not None and report.files == 1
    assert "Pinned 1 files" in caplog.text
    # The pages live in the pinner, which keeps running after its report.
    pinner = page_pin._PINNERS.pop()
    assert pinner.poll() is None
    pinner.kill()
    pinner.wait()


def test_pin_from_environment_tolerates_a_failed_pinner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("", encoding="utf-8")
    environment = {page_pin.PAGE_PIN_ENV: "1", page_pin.MANIFEST_ENV: str(manifest)}

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("no fork")

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", refuse)
        with caplog.at_level(logging.WARNING):
            assert page_pin.pin_from_environment(environment) is None
    assert "did not start" in caplog.text

    silent = [sys.executable, "-c", "import time; time.sleep(30)"]
    original = subprocess.Popen
    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", lambda _argv, **kwargs: original(silent, **kwargs))
        with caplog.at_level(logging.WARNING):
            assert page_pin.pin_from_environment(environment, wait_seconds=0.2) is None
    assert "no report" in caplog.text
    page_pin._PINNERS.pop().kill()


def test_read_report_rejects_bad_output() -> None:
    garbled = subprocess.Popen(
        [sys.executable, "-c", "print('not json')"], stdout=subprocess.PIPE, text=True
    )
    assert page_pin._read_report(garbled, 5.0) is None
    garbled.wait()
    garbled.stdout.close()  # type: ignore[union-attr]
    closed = subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, text=True)
    assert page_pin._read_report(closed, 5.0) is None
    closed.wait()


def test_pinner_main_reports_and_holds(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / "file"
    target.write_bytes(b"q" * 8)
    manifest = tmp_path / "manifest.txt"
    manifest.write_text(f"{target}\n", encoding="utf-8")
    held: list[bool] = []
    assert page_pin.main(["page_pin", str(manifest)], hold=lambda: held.append(True)) == 0
    assert held == [True]
    assert json.loads(capsys.readouterr().out)["files"] == 1


def test_shipped_manifest_uses_placeholders() -> None:
    lines = (ROOT / "runtime" / "pin-manifest.txt").read_text(encoding="utf-8").splitlines()
    body = [line for line in lines if line and not line.startswith("#")]
    assert len(body) > 1000
    assert not any("x86_64" in line or "aarch64" in line for line in body)
    assert any("{machine}" in line for line in body)


def test_manifest_builder_normalizes_trace(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location(
        "pin_manifest", ROOT / "tools" / "pin_manifest.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    trace = "\n".join(
        [
            '12 openat(AT_FDCWD, "/lib/x86_64-linux-gnu/libc.so.6", O_RDONLY) = 3',
            '12 openat(AT_FDCWD, "/missing", O_RDONLY) = -1 ENOENT (No such file)',
            '12 execve("/opt/k/node-v1-linux-x64/bin/node", [...]) = 0',
            '12 openat(AT_FDCWD, "/proc/self/status", O_RDONLY) = 3',
            '12 openat(AT_FDCWD, "/usr/lib/dir/", O_RDONLY) = 3',
            "/usr/local/lib/python3.12/os.py",
            "not a path",
        ]
    )
    lines = module.build(trace)
    assert lines[:3] == [
        "/lib/{machine}-linux-gnu/libc.so.6",
        "/opt/k/node-v1-linux-{node_arch}/bin/node",
        "/usr/local/lib/python3.12/os.py",
    ]
    assert lines[3:] == list(module._EXTRA)
    source = tmp_path / "trace.log"
    source.write_text(trace, encoding="utf-8")
    assert module.main(["pin_manifest.py", str(source)]) == 0
