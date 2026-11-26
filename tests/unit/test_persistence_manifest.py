from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import time
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
    safe_symlink_target,
    workspace_fingerprint,
)

ROOT = Path(__file__).parents[2]
SANDBOX_ID = "sbx_01J00000000000000000000000"


def test_policy_accepts_only_documented_durable_roots() -> None:
    policy = PersistencePolicy()
    for path in [
        "home/.kiro/crew/config.json",
        "home/.config/tool/settings.json",
        "home/.local/share/kiro-cli/data.sqlite3",
        "home/workplace/kirocrew-workspace/taskrunner_main/plan_1/task-check.txt",
        "artifacts/report/summary.md",
        "knowledge/base/entry.json",
        "memory/agent/notes.md",
        "projects/default/source.py",
        "user/report.txt",
        # A repository's refs survive a restore, so its objects must too:
        # without them HEAD names a commit that no longer exists and every
        # unpushed commit is gone.
        "projects/default/.git/HEAD",
        "projects/default/.git/objects/pack/pack-1.pack",
        "projects/default/.git/objects/ab/cdef0123",
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


def test_workspace_fingerprint_tracks_durable_metadata_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    report = workspace / "user/report.txt"
    report.parent.mkdir(parents=True)
    report.write_text("v1", encoding="utf-8")
    first = workspace_fingerprint(workspace)
    assert workspace_fingerprint(workspace) == first

    os.utime(report, ns=(1, 1))
    touched = workspace_fingerprint(workspace)
    assert touched != first

    # Paths outside the durable roots never influence the fingerprint.
    (workspace / "stray.txt").write_text("ignored", encoding="utf-8")
    assert workspace_fingerprint(workspace) == touched

    # Excluded entries inside a root are walked but never hashed.
    log = workspace / "user/debug.log"
    log.write_text("x", encoding="utf-8")
    os.utime(report.parent, ns=(2, 2))
    with_log = workspace_fingerprint(workspace)
    log.write_text("xx", encoding="utf-8")
    os.utime(report.parent, ns=(2, 2))
    assert workspace_fingerprint(workspace) == with_log

    # A missing workspace yields the stable empty fingerprint.
    assert workspace_fingerprint(tmp_path / "missing") == workspace_fingerprint(
        tmp_path / "also-missing"
    )


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
    # Never traversed, and -- an absolute link restore would refuse, which
    # would invalidate the whole generation -- not recorded either.
    assert built.manifest.entries == ()


def test_policy_excludes_the_in_workspace_embedding_model_cache() -> None:
    policy = PersistencePolicy()
    # The model ships in the image; a workspace copy is dead weight that
    # bloats checkpoints and eats the 1GB session-storage quota.
    assert not policy.includes(PurePosixPath("home/.kiro/crew/models/qwen3-embedding-0.6b.gguf"))
    assert not policy.includes(PurePosixPath("home/.kiro/crew/models"))
    assert not policy.includes(
        PurePosixPath("home/.kiro/crew/browser-harness/chromium/154.0.8037.0/chrome")
    )
    assert policy.includes(PurePosixPath("home/.kiro/crew/browser-harness/state.json"))
    # Only that exact directory: user files merely named "models" survive.
    assert policy.includes(PurePosixPath("projects/default/models/data.txt"))
    assert policy.includes(PurePosixPath("home/.kiro/crew/skills/models.md"))


def test_checkpoint_leaves_out_symlinks_restore_would_refuse(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # One unrestorable link used to invalidate the whole generation: the
    # checkpoint committed, the next start fell back a generation and the
    # work in between was gone. The checkpoint now applies restore's own rule.
    workspace = tmp_path / "workspace"
    project = workspace / "projects/default"
    project.mkdir(parents=True)
    (project / "data.txt").write_text("kept", encoding="utf-8")
    (project / "inside").symlink_to("data.txt")
    (project / "absolute").symlink_to("/etc/hosts")
    (project / "escapes").symlink_to("../../../outside")
    with caplog.at_level("WARNING"):
        built = ManifestBuilder(workspace).build(1, SANDBOX_ID, "2026-10-08T07:00:00Z")
    links = {e.path: e.symlink_target for e in built.manifest.entries if e.type == "symlink"}
    assert links == {"projects/default/inside": "data.txt"}
    assert "projects/default/absolute" in caplog.text
    assert "projects/default/escapes" in caplog.text
    assert safe_symlink_target(PurePosixPath("projects/default/x"), "../../user/y")
    assert not safe_symlink_target(PurePosixPath("projects/default/x"), "../../../y")


def test_builder_reuses_entries_of_unchanged_files_after_a_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "app"
    project.mkdir(parents=True)
    (project / "big.bin").write_bytes(b"A" * 64)
    (project / "notes.txt").write_text("v1")
    long_ago = 1_700_000_000_000_000_000
    for name in ("big.bin", "notes.txt"):
        os.utime(project / name, ns=(long_ago, long_ago))
    later = time.time_ns() + 10 * 10**9
    builder = ManifestBuilder(workspace, chunk_size=16, clock_ns=lambda: later)
    reads: list[str] = []
    original = builder._read_chunks

    def counting(path: Path) -> list[bytes]:
        reads.append(path.name)
        return original(path)

    builder._read_chunks = counting  # type: ignore[method-assign]

    first = builder.build(1, SANDBOX_ID, "2026-10-29T00:00:00Z")
    assert sorted(reads) == ["big.bin", "notes.txt"]
    # Not yet remembered: an uncommitted build is no baseline.
    reads.clear()
    builder.build(2, SANDBOX_ID, "2026-10-29T00:01:00Z")
    assert sorted(reads) == ["big.bin", "notes.txt"]

    builder.remember(first)
    (project / "notes.txt").write_text("v2")
    os.utime(project / "notes.txt", ns=(long_ago + 5, long_ago + 5))
    reads.clear()
    second = builder.build(3, SANDBOX_ID, "2026-10-29T00:02:00Z")
    assert reads == ["notes.txt"]
    # The reused entry is the committed one; only the changed file brings chunks.
    by_path = {entry.path: entry for entry in second.manifest.entries}
    first_by_path = {entry.path: entry for entry in first.manifest.entries}
    assert by_path["projects/app/big.bin"] == first_by_path["projects/app/big.bin"]
    assert by_path["projects/app/notes.txt"] != first_by_path["projects/app/notes.txt"]
    assert set(second.chunks) == set(by_path["projects/app/notes.txt"].chunks)

    builder.forget()
    reads.clear()
    builder.build(4, SANDBOX_ID, "2026-10-29T00:03:00Z")
    assert sorted(reads) == ["big.bin", "notes.txt"]


def test_builder_does_not_trust_files_modified_within_the_racy_window(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "app"
    project.mkdir(parents=True)
    target = project / "hot.txt"
    target.write_text("x")
    now = target.stat().st_mtime_ns
    builder = ManifestBuilder(workspace, clock_ns=lambda: now)
    built = builder.build(1, SANDBOX_ID, "2026-10-29T00:00:00Z")
    assert "projects/app/hot.txt" not in built.stamps
    builder.remember(built)
    reads: list[str] = []
    original = builder._read_chunks

    def counting(path: Path) -> list[bytes]:
        reads.append(path.name)
        return original(path)

    builder._read_chunks = counting  # type: ignore[method-assign]
    builder.build(2, SANDBOX_ID, "2026-10-29T00:01:00Z")
    assert reads == ["hot.txt"]


def test_builder_rereads_a_file_whose_inode_changed(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    project = workspace / "projects" / "app"
    project.mkdir(parents=True)
    target = project / "swap.txt"
    target.write_text("old")
    long_ago = 1_700_000_000_000_000_000
    os.utime(target, ns=(long_ago, long_ago))
    builder = ManifestBuilder(workspace, clock_ns=lambda: time.time_ns() + 10 * 10**9)
    builder.remember(builder.build(1, SANDBOX_ID, "2026-10-29T00:00:00Z"))
    replacement = project / "swap.tmp"
    replacement.write_text("new")
    os.utime(replacement, ns=(long_ago, long_ago))
    replacement.replace(target)
    built = builder.build(2, SANDBOX_ID, "2026-10-29T00:01:00Z")
    entry = next(e for e in built.manifest.entries if e.path == "projects/app/swap.txt")
    assert entry.chunks and set(entry.chunks) <= set(built.chunks)


class _GrowingWhileRead(ManifestBuilder):
    """Appends to the file between the builder's lstat and its read."""

    def _read_chunks(self, path: Path) -> list[bytes]:
        if path.name == "pack":
            with path.open("ab") as handle:
                handle.write(b"-appended")
        return super()._read_chunks(path)


def test_a_file_written_during_the_build_records_the_size_actually_read(
    tmp_path: Path,
) -> None:
    """Restore checks size and digest together; a stale size made the whole
    generation unrestorable (seen on a sandbox whose app was cloning)."""
    workspace = tmp_path / "workspace"
    target = workspace / "home" / ".kiro" / "crew" / "pack"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"original")

    builder = _GrowingWhileRead(workspace)
    built = builder.build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")

    entry = next(e for e in built.manifest.entries if e.path.endswith("crew/pack"))
    content = b"".join(built.chunks[digest] for digest in entry.chunks)
    assert content == b"original-appended"
    assert entry.size == len(content)
    assert entry.digest == hashlib.sha256(content).hexdigest()
    # Not offered as an unchanged baseline: its stamp no longer matches.
    assert entry.path not in built.stamps


class _VanishingBuilder(ManifestBuilder):
    """Deletes files right after the walk lists them, as git does with temp packs."""

    def _walk(self) -> list[Path]:
        paths = super()._walk()
        for name in ("tmp_pack", "tmp_link"):
            victim = self._workspace / "home" / ".kiro" / "crew" / name
            victim.unlink()
        return paths


def test_a_file_removed_during_the_build_is_skipped_not_fatal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    crew = workspace / "home" / ".kiro" / "crew"
    crew.mkdir(parents=True)
    (crew / "tmp_pack").write_bytes(b"x")
    (crew / "tmp_link").symlink_to("tmp_pack")
    (crew / "kept").write_bytes(b"kept")

    built = _VanishingBuilder(workspace).build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")

    paths = {entry.path for entry in built.manifest.entries}
    assert "home/.kiro/crew/kept" in paths
    assert not any(path.endswith(("tmp_pack", "tmp_link")) for path in paths)


def test_a_file_removed_between_stat_and_read_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    crew = workspace / "home" / ".kiro" / "crew"
    crew.mkdir(parents=True)
    (crew / "gone").write_bytes(b"x")
    builder = ManifestBuilder(workspace)

    def vanished(_path: Path) -> list[bytes]:
        raise FileNotFoundError

    monkeypatch.setattr(builder, "_read_chunks", vanished)
    built = builder.build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")
    assert not any(e.path.endswith("crew/gone") for e in built.manifest.entries)


def test_the_fingerprint_ignores_a_file_removed_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kirocrew_agentcore_persistence.manifest as manifest_module

    workspace = tmp_path / "workspace"
    crew = workspace / "home" / ".kiro" / "crew"
    crew.mkdir(parents=True)
    (crew / "gone").write_bytes(b"x")
    real_walk = manifest_module._walk_included_roots

    def walk_then_delete(root: Path) -> list[Path]:
        paths = real_walk(root)
        (crew / "gone").unlink()
        return paths

    monkeypatch.setattr(manifest_module, "_walk_included_roots", walk_then_delete)
    assert workspace_fingerprint(workspace)


def test_a_symlink_removed_between_stat_and_readlink_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    crew = workspace / "home" / ".kiro" / "crew"
    crew.mkdir(parents=True)
    (crew / "link").symlink_to("elsewhere")
    real_readlink = Path.readlink

    def gone(self: Path) -> Path:
        if self.name == "link":
            raise FileNotFoundError
        return real_readlink(self)

    monkeypatch.setattr(Path, "readlink", gone)
    built = ManifestBuilder(workspace).build(1, SANDBOX_ID, "2026-08-17T16:00:00Z")
    assert not any(e.path.endswith("crew/link") for e in built.manifest.entries)
