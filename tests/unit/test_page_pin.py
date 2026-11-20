from __future__ import annotations

import importlib.util
import logging
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
