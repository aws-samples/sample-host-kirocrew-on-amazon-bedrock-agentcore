from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator, FormatChecker
from kirocrew_agentcore_persistence.manifest import (
    PERSISTENCE_CATEGORIES,
    ManifestBuilder,
    PersistencePolicy,
)

ROOT = Path(__file__).parents[2]
SANDBOX_ID = "sbx_01J00000000000000000000000"


def test_policy_accepts_only_documented_durable_roots() -> None:
    policy = PersistencePolicy()
    for path in [
        "home/.kiro/crew/config.json",
        "home/.config/tool/settings.json",
        "home/.local/share/kiro-cli/data.sqlite3",
        "projects/default/source.py",
        "user/report.txt",
    ]:
        assert policy.includes(PurePosixPath(path))
    for path in [
        "/absolute",
        "../escape",
        "home/.local/share/other-tool/state.db",
        "home/.local/bin/kiro-cli",
        "home/.aws/credentials",
        "home/.ssh/id_key",
        "home/.kiro/.cache/item",
        "projects/default/node_modules/pkg",
        "user/runtime.log",
        "user/process.pid",
        "user/.env",
        ".agentcore/persistence.json",
    ]:
        assert not policy.includes(PurePosixPath(path))


def test_manifest_builder_requires_valid_parameters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Chunk size"):
        ManifestBuilder(tmp_path, chunk_size=0)
    builder = ManifestBuilder(tmp_path)
    with pytest.raises(ValueError, match="generation"):
        builder.build(0, SANDBOX_ID, "2026-08-17T16:00:00Z")
    with pytest.raises(ValueError, match="sandbox"):
        builder.build(1, "", "2026-08-17T16:00:00Z")
    missing = ManifestBuilder(tmp_path / "missing").build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")
    assert missing.manifest.entries == ()


def test_manifest_preserves_supported_metadata_and_excludes_transient_files(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    durable_files = {
        "home/.kiro/crew/config.json": b"configuration",
        "home/.kiro/crew/memory/index.json": b"memory",
        "home/.config/tool/settings.json": b"tool config",
        "projects/default/README.md": b"project",
        "projects/default/unicode-文件.txt": "内容".encode(),
        "user/empty.bin": b"",
        "user/large.bin": b"0123456789",
    }
    for relative, content in durable_files.items():
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    executable = workspace / "projects/default/run.sh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o750)
    symlink = workspace / "user/project-link"
    symlink.symlink_to("../projects/default")

    for relative in [
        "home/.kiro/.cache/rebuildable",
        "home/.config/tool/debug.log",
        "projects/default/node_modules/pkg/index.js",
        "user/process.pid",
        "user/.env",
        ".agentcore/dirty-journal/events.jsonl",
    ]:
        path = workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("excluded", encoding="utf-8")
    fifo = workspace / "user/pipe"
    os.mkfifo(fifo)
    socket_path = workspace / "user/socket"
    unix_socket = socket.socket(socket.AF_UNIX)
    unix_socket.bind(str(socket_path))
    try:
        built = ManifestBuilder(workspace, chunk_size=4).build(
            3, SANDBOX_ID, "2026-08-17T16:00:00Z"
        )
    finally:
        unix_socket.close()

    entries = {entry.path: entry for entry in built.manifest.entries}
    assert tuple(entries) == tuple(sorted(entries))
    assert set(durable_files) <= set(entries)
    assert entries["projects/default/run.sh"].mode == 0o750
    assert entries["user/project-link"].symlink_target == "../projects/default"
    assert entries["user/empty.bin"].chunks
    assert len(entries["user/large.bin"].chunks) == 3
    assert all(".cache" not in path and not path.endswith((".log", ".pid")) for path in entries)
    assert "user/pipe" not in entries
    assert "user/socket" not in entries
    assert set(PERSISTENCE_CATEGORIES) == set(json.loads(built.manifest.to_json())["categories"])

    schema = cast(
        dict[str, object],
        json.loads(
            (ROOT / "contracts/schemas/persistence-manifest.schema.json").read_text(
                encoding="utf-8"
            )
        ),
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(
        json.loads(built.manifest.to_json())
    )
    assert stat.S_ISFIFO(fifo.lstat().st_mode)


@given(content=st.binary(max_size=10_000), chunk_size=st.integers(min_value=1, max_value=1024))
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_manifest_chunks_reconstruct_arbitrary_file_content(
    tmp_path: Path, content: bytes, chunk_size: int
) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / "user/blob.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    built = ManifestBuilder(workspace, chunk_size=chunk_size).build(
        1, SANDBOX_ID, "2026-08-17T16:00:00Z"
    )
    entry = next(item for item in built.manifest.entries if item.path == "user/blob.bin")
    reconstructed = b"".join(built.chunks[digest] for digest in entry.chunks)
    assert reconstructed == content
    assert entry.digest == hashlib.sha256(content).hexdigest()


def test_manifest_records_symlinked_persistence_root_without_traversal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "external-user-data"
    target.mkdir()
    (target / "outside.txt").write_text("outside", encoding="utf-8")
    (workspace / "user").symlink_to(target, target_is_directory=True)
    built = ManifestBuilder(workspace).build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")
    assert len(built.manifest.entries) == 1
    assert built.manifest.entries[0].path == "user"
    assert built.manifest.entries[0].type == "symlink"
    assert "outside.txt" not in {entry.path for entry in built.manifest.entries}
