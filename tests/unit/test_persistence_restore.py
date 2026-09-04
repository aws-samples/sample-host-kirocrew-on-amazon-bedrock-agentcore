from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import kirocrew_agentcore_persistence.restore as module
import pytest
from kirocrew_agentcore_persistence.durability import (
    BrokeredCheckpointStore,
    LocalKmsAdapter,
    LocalObjectStore,
    PersistenceBroker,
)
from kirocrew_agentcore_persistence.manifest import (
    PERSISTENCE_CATEGORIES,
    ManifestBuilder,
    PersistenceManifest,
)
from kirocrew_agentcore_persistence.restore import (
    ManifestDecoder,
    ManifestMigrator,
    ManifestValidationError,
    RestoreConditions,
    RestoreEngine,
    RestoreError,
)

SANDBOX_ID = "sbx_01J00000000000000000000000"


@dataclass
class MutableClock:
    now: datetime = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def storage(
    clock: MutableClock | None = None,
) -> tuple[MutableClock, LocalObjectStore, PersistenceBroker, BrokeredCheckpointStore]:
    selected = clock or MutableClock()
    objects = LocalObjectStore(clock=selected)
    broker = PersistenceBroker("snapshots", objects, LocalKmsAdapter(b"k" * 32), clock=selected)
    return selected, objects, broker, BrokeredCheckpointStore(broker, SANDBOX_ID, clock=selected)


def create_generation(
    workspace: Path,
    generation: int,
    broker: PersistenceBroker,
    store: BrokeredCheckpointStore,
) -> str:
    built = ManifestBuilder(workspace, chunk_size=3).build(
        generation, SANDBOX_ID, "2026-07-01T12:00:00Z"
    )
    cipher = broker.cipher(SANDBOX_ID)
    for digest, plaintext in built.chunks.items():
        store.upload_chunk(digest, cipher.encrypt(plaintext).ciphertext)
    blob = cipher.encrypt(built.manifest.to_json())
    store.upload_manifest(generation, blob)
    store.commit_generation(generation, blob.digest)
    return blob.digest


def representative_workspace(root: Path, text: str) -> None:
    file_path = root / "user/data.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(text, encoding="utf-8")
    file_path.chmod(0o640)
    config = root / "home/.config/app/settings.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('{"enabled":true}', encoding="utf-8")
    project = root / "projects/default"
    project.mkdir(parents=True)
    (project / "link").symlink_to("../../user/data.txt")


def engine(
    workspace: Path,
    broker: PersistenceBroker,
    store: BrokeredCheckpointStore,
    *,
    runtime_version: str = "runtime-v2",
    clock: MutableClock | None = None,
    replace: Callable[[Path, Path], None] | None = None,
) -> RestoreEngine:
    if replace is not None:
        return RestoreEngine(
            workspace,
            SANDBOX_ID,
            runtime_version,
            store,
            broker.cipher(SANDBOX_ID),
            clock=clock or MutableClock(),
            replace=replace,
        )
    return RestoreEngine(
        workspace,
        SANDBOX_ID,
        runtime_version,
        store,
        broker.cipher(SANDBOX_ID),
        clock=clock or MutableClock(),
    )


def test_empty_managed_mount_restores_authoritative_generation(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    source = tmp_path / "empty-mount-source"
    source.mkdir()
    representative_workspace(source, "durable content")
    create_generation(source, 1, broker, store)
    target = source.parent / "empty-mount-target"
    if target.exists():
        __import__("shutil").rmtree(target)
    target.mkdir()
    report = engine(target, broker, store).restore(existing_sandbox=True)
    assert report.outcome == "restored"
    assert report.generation == 1
    assert (target / "user/data.txt").read_text(encoding="utf-8") == "durable content"
    assert (target / "user/data.txt").stat().st_mode & 0o777 == 0o640
    assert (target / "projects/default/link").is_symlink()
    assert (
        json.loads((target / ".agentcore/persistence.json").read_text())["runtimeVersion"]
        == "runtime-v2"
    )
    assert (target / ".agentcore/restore-report.json").stat().st_mode & 0o777 == 0o600


def test_checkpoint_older_than_fourteen_days_still_restores(tmp_path: Path) -> None:
    clock, _, broker, store = storage()
    source = tmp_path / "old-checkpoint-source"
    source.mkdir()
    representative_workspace(source, "old but authoritative")
    create_generation(source, 1, broker, store)
    clock.advance(timedelta(days=30))
    target = source.parent / "old-checkpoint-target"
    if target.exists():
        __import__("shutil").rmtree(target)
    report = engine(target, broker, store, clock=clock).restore(
        existing_sandbox=True,
        conditions=RestoreConditions(managed_session_expired=True),
    )
    assert report.outcome == "restored"
    assert "managed-session-expired" in report.reasons
    assert (target / "user/data.txt").read_text() == "old but authoritative"


def test_runtime_version_reset_forces_restore_even_with_matching_local_pointer(
    tmp_path: Path,
) -> None:
    _, _, broker, store = storage()
    workspace = tmp_path / "workspace"
    representative_workspace(workspace, "authoritative")
    digest = create_generation(workspace, 1, broker, store)
    state = workspace / ".agentcore/persistence.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "generation": 1,
                "manifestDigest": digest,
                "runtimeVersion": "runtime-v1",
            }
        )
    )
    (workspace / "user/data.txt").write_text("local mutation")
    report = engine(workspace, broker, store).restore(
        existing_sandbox=True,
        conditions=RestoreConditions(runtime_version_reset=True),
    )
    assert report.outcome == "restored"
    assert report.reasons == ("runtime-version-reset",)
    assert (workspace / "user/data.txt").read_text() == "authoritative"


def test_corrupt_latest_generation_falls_back_to_immediately_previous(
    tmp_path: Path,
) -> None:
    _, objects, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "generation one")
    create_generation(source, 1, broker, store)
    (source / "user/data.txt").write_text("generation two")
    create_generation(source, 2, broker, store)
    objects.objects[f"snapshots/{SANDBOX_ID}/manifests/2.json.enc"] = b"corrupt"
    target = tmp_path / "target"
    target.mkdir()
    report = engine(target, broker, store).restore(existing_sandbox=True)
    assert report.outcome == "fallback"
    assert report.fallback_used
    assert report.attempted_generations == (2, 1)
    assert any(reason.startswith("generation-2-invalid") for reason in report.reasons)
    assert (target / "user/data.txt").read_text() == "generation one"


def test_broker_rejection_mid_restore_falls_back_instead_of_aborting(
    tmp_path: Path,
) -> None:
    from kirocrew_agentcore_persistence.durability import BrokerAuthorizationError

    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "generation one")
    create_generation(source, 1, broker, store)
    (source / "user/data.txt").write_text("generation two")
    create_generation(source, 2, broker, store)

    class RejectingStore:
        """Denies the latest manifest, as an expired binding token would."""

        def __init__(self, inner: BrokeredCheckpointStore) -> None:
            self._inner = inner

        def committed_generations(self) -> tuple[object, ...]:
            return self._inner.committed_generations()

        def get_manifest(self, generation: int) -> object:
            if generation == 2:
                raise BrokerAuthorizationError("Persistence broker rejected the operation.")
            return self._inner.get_manifest(generation)

        def get_chunk(self, digest: str) -> bytes:
            return self._inner.get_chunk(digest)

    target = tmp_path / "target"
    target.mkdir()
    rejecting = cast(BrokeredCheckpointStore, RejectingStore(store))
    report = RestoreEngine(
        target,
        SANDBOX_ID,
        "runtime-v2",
        rejecting,
        broker.cipher(SANDBOX_ID),
        clock=MutableClock(),
    ).restore(existing_sandbox=True)
    assert report.outcome == "fallback"
    assert any("BrokerAuthorizationError" in reason for reason in report.reasons)
    assert (target / "user/data.txt").read_text() == "generation one"


def test_new_sandbox_may_initialize_empty_but_existing_sandbox_never_does(
    tmp_path: Path,
) -> None:
    _, _, broker, store = storage()
    new_workspace = tmp_path / "new"
    initialized = engine(new_workspace, broker, store).restore(existing_sandbox=False)
    assert initialized.outcome == "initialized"
    assert initialized.reasons == ("new-sandbox",)
    existing_workspace = tmp_path / "existing"
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(existing_workspace, broker, store).restore(existing_sandbox=True)
    report = json.loads((existing_workspace / ".agentcore/restore-report.json").read_text())
    assert report["outcome"] == "failed"
    assert list(existing_workspace.iterdir()) == [existing_workspace / ".agentcore"]


def test_matching_local_generation_is_reused_and_integrity_failure_forces_restore(
    tmp_path: Path,
) -> None:
    _, _, broker, store = storage()
    workspace = tmp_path / "workspace"
    representative_workspace(workspace, "remote")
    digest = create_generation(workspace, 1, broker, store)
    state = workspace / ".agentcore/persistence.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "generation": 1,
                "manifestDigest": digest,
                "runtimeVersion": "runtime-v2",
            }
        )
    )
    local = engine(workspace, broker, store).restore(existing_sandbox=True)
    assert local.outcome == "local"
    assert local.attempted_generations == ()
    (workspace / "user/data.txt").write_text("corrupt local")
    restored = engine(workspace, broker, store).restore(
        existing_sandbox=True,
        conditions=RestoreConditions(local_integrity_ok=False),
    )
    assert restored.outcome == "restored"
    assert "local-integrity-failed" in restored.reasons
    assert (workspace / "user/data.txt").read_text() == "remote"


def test_malformed_local_pointer_and_generation_mismatch_force_restore(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    for index, content in enumerate((b"not json", b"[]", b'{"generation":9}')):
        target = tmp_path / f"target-{index}"
        state = target / ".agentcore/persistence.json"
        state.parent.mkdir(parents=True)
        state.write_bytes(content)
        report = engine(target, broker, store).restore(existing_sandbox=True)
        assert report.outcome == "restored"
        assert report.reasons


def test_atomic_install_rolls_back_original_workspace_on_swap_failure(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    target = tmp_path / "target"
    representative_workspace(target, "original")
    calls = 0

    def fail_second_swap(origin: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("swap failed")
        origin.replace(destination)

    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(target, broker, store, replace=fail_second_swap).restore(existing_sandbox=True)
    assert (target / "user/data.txt").read_text() == "original"
    assert not (tmp_path / ".target.restore-stage").exists()


def test_explicit_runtime_reset_condition_forces_matching_local_generation(
    tmp_path: Path,
) -> None:
    _, _, broker, store = storage()
    workspace = tmp_path / "workspace"
    representative_workspace(workspace, "remote")
    digest = create_generation(workspace, 1, broker, store)
    state = workspace / ".agentcore/persistence.json"
    state.parent.mkdir()
    state.write_text(
        json.dumps(
            {
                "generation": 1,
                "manifestDigest": digest,
                "runtimeVersion": "runtime-v2",
            }
        )
    )
    report = engine(workspace, broker, store).restore(
        existing_sandbox=True,
        conditions=RestoreConditions(runtime_version_reset=True),
    )
    assert report.outcome == "restored"
    assert report.reasons == ("runtime-version-reset",)


def test_committed_manifest_digest_mismatch_is_rejected(tmp_path: Path) -> None:
    _, objects, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    manifest_key = f"snapshots/{SANDBOX_ID}/manifests/1.json.enc"
    value = json.loads(objects.objects[manifest_key])
    value["digest"] = "0" * 64
    objects.objects[manifest_key] = json.dumps(value).encode()
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(tmp_path / "target", broker, store).restore(existing_sandbox=True)


def test_restored_whole_file_size_must_match_manifest(tmp_path: Path) -> None:
    _, objects, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    cipher = broker.cipher(SANDBOX_ID)
    plaintext = cipher.decrypt(store.get_manifest(1))
    value = json.loads(plaintext)
    file_value = next(entry for entry in value["entries"] if entry["type"] == "file")
    file_value["size"] += 1
    replacement = cipher.encrypt(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
    store.upload_manifest(1, replacement)
    commit_key = f"snapshots/{SANDBOX_ID}/commits/1.json"
    commit_value = json.loads(objects.objects[commit_key])
    commit_value["manifestDigest"] = replacement.digest
    objects.objects[commit_key] = json.dumps(commit_value).encode()
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(tmp_path / "target", broker, store).restore(existing_sandbox=True)


def test_failed_initial_install_without_existing_workspace_is_safe(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)

    def fail_swap(_origin: Path, _destination: Path) -> None:
        raise OSError("swap failed")

    target = tmp_path / "target"
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(target, broker, store, replace=fail_swap).restore(existing_sandbox=True)
    assert not (tmp_path / ".target.restore-stage").exists()


def test_restore_requires_at_least_one_fetch_worker(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    with pytest.raises(ValueError, match="fetch worker"):
        RestoreEngine(
            tmp_path / "workspace",
            SANDBOX_ID,
            "0.2.0",
            store,
            broker.cipher(SANDBOX_ID),
            fetch_workers=0,
        )


def test_restore_reuses_prefetched_chunks_shared_between_files(tmp_path: Path) -> None:
    """Identical content across files shares one chunk; each copy must land."""
    _, _, broker, store = storage()
    source = tmp_path / "source"
    (source / "user").mkdir(parents=True)
    (source / "user/first.bin").write_text("shared-chunk-content")
    (source / "user/second.bin").write_text("shared-chunk-content")
    create_generation(source, 1, broker, store)
    target = tmp_path / "target"
    report = engine(target, broker, store).restore(existing_sandbox=True)
    assert report.outcome == "restored"
    assert (target / "user/first.bin").read_text() == "shared-chunk-content"
    assert (target / "user/second.bin").read_text() == "shared-chunk-content"


def test_failed_install_restores_backed_up_state_file(tmp_path: Path) -> None:
    """A workspace with a previous persistence.json gets it back on rollback."""
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    digest = create_generation(source, 1, broker, store)
    target = tmp_path / "target"
    representative_workspace(target, "original")
    state = target / ".agentcore/persistence.json"
    state.parent.mkdir(parents=True)
    state.write_text("original-state")
    calls = 0

    def fail_late(origin: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if origin.name == "persistence.json" and origin.parent.parent.name == "restore-stage":
            raise OSError("state swap failed")
        origin.replace(destination)

    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(target, broker, store, replace=fail_late).restore(existing_sandbox=True)
    assert state.read_text() == "original-state"
    assert (target / "user/data.txt").read_text() == "original"
    assert digest


def test_rollback_skips_entries_whose_backup_move_itself_failed(tmp_path: Path) -> None:
    """If moving an entry to backup fails part-way, rollback must not require it."""
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    target = tmp_path / "target"
    representative_workspace(target, "original")
    (target / "zz-extra.txt").write_text("second entry")

    def fail_second_backup_move(origin: Path, destination: Path) -> None:
        if destination.parent.name == "restore-backup" and origin.name == "zz-extra.txt":
            # Simulate a move that failed after being recorded as attempted:
            # the origin is consumed but the backup copy never appears.
            origin.unlink()
            raise OSError("backup move failed")
        origin.replace(destination)

    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(target, broker, store, replace=fail_second_backup_move).restore(
            existing_sandbox=True
        )
    assert (target / "user/data.txt").read_text() == "original"


def _exdev_replace(monkeypatch: pytest.MonkeyPatch, failing: set[str]) -> None:
    original = Path.replace

    def replace(self: Path, target: object) -> object:
        if self.name in failing:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return original(self, target)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "replace", replace)


def test_replace_path_copies_directories_across_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mount point inside the workspace can never be renamed away."""
    source = tmp_path / "home"
    (source / "nested").mkdir(parents=True)
    (source / "nested/credentials.json").write_text("keep me")
    (source / "link").symlink_to("nested")
    destination = tmp_path / "backup-home"
    _exdev_replace(monkeypatch, {"home"})
    module._replace_path(source, destination)
    assert (destination / "nested/credentials.json").read_text() == "keep me"
    assert (destination / "link").is_symlink()
    assert not source.exists()


def test_replace_path_copies_files_and_symlinks_across_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file_source = tmp_path / "data.txt"
    file_source.write_text("payload")
    file_destination = tmp_path / "moved.txt"
    file_destination.write_text("stale")
    link_source = tmp_path / "pointer"
    link_source.symlink_to("data.txt")
    link_destination = tmp_path / "moved-pointer"
    _exdev_replace(monkeypatch, {"data.txt", "pointer"})
    module._replace_path(file_source, file_destination)
    assert file_destination.read_text() == "payload"
    assert not file_source.exists()
    module._replace_path(link_source, link_destination)
    assert link_destination.is_symlink()
    assert not link_source.is_symlink()


def test_replace_path_reraises_other_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "entry"
    source.write_text("x")

    def replace(self: Path, target: object) -> object:
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(Path, "replace", replace)
    with pytest.raises(OSError, match="denied"):
        module._replace_path(source, tmp_path / "target")


def test_tolerant_removal_keeps_busy_mount_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mount = tmp_path / "mounted"
    (mount / "inner").mkdir(parents=True)
    (mount / "inner/file.txt").write_text("x")
    original = Path.rmdir

    def rmdir(self: Path) -> None:
        if self == mount:
            raise OSError(errno.EBUSY, "Device or resource busy")
        original(self)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    module._remove_tree_tolerating_mounts(mount)
    assert mount.exists()
    assert list(mount.iterdir()) == []
    module._remove_tree_tolerating_mounts(tmp_path / "absent")

    def rmdir_fail(self: Path) -> None:
        raise OSError(errno.EACCES, "denied")

    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setattr(Path, "rmdir", rmdir_fail)
    with pytest.raises(OSError, match="denied"):
        module._remove_tree_tolerating_mounts(other)


def test_rollback_continues_past_a_vanished_backup_entry(tmp_path: Path) -> None:
    """Rollback restores every surviving backup even if one entry vanished."""
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    target = tmp_path / "target"
    representative_workspace(target, "original")
    (target / "aaa-first.txt").write_text("first entry")

    def hostile(origin: Path, destination: Path) -> None:
        if destination.parent.name == "restore-backup" and origin.name == "aaa-first.txt":
            origin.replace(destination)
            destination.unlink()  # the backup copy vanishes after the move
            return
        if origin.name == "persistence.json" and "restore-stage" in str(origin):
            raise OSError("state swap failed")
        origin.replace(destination)

    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        engine(target, broker, store, replace=hostile).restore(existing_sandbox=True)
    assert (target / "user/data.txt").read_text() == "original"
    assert not (target / "aaa-first.txt").exists()


def test_restore_succeeds_when_workspace_parent_is_read_only(tmp_path: Path) -> None:
    """The fleet mounts the workspace as the only writable path.

    Staging next to the workspace raised PermissionError there, which made
    every cold-start restore fail within seconds while local runs (writable
    parents) passed. Restores must never write outside the workspace.
    """
    _, _, broker, store = storage()
    source = tmp_path / "source"
    representative_workspace(source, "remote")
    create_generation(source, 1, broker, store)
    mount = tmp_path / "mnt"
    workspace = mount / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "stale.txt").write_text("pre-restore")
    mount.chmod(0o555)
    try:
        report = engine(workspace, broker, store).restore(existing_sandbox=True)
    finally:
        mount.chmod(0o755)
    assert report.outcome == "restored"
    assert report.generation == 1
    assert (workspace / "user/data.txt").read_text() == "remote"
    assert not (workspace / "stale.txt").exists()
    assert not (workspace / ".agentcore/restore-stage").exists()
    assert not (workspace / ".agentcore/restore-backup").exists()


def test_restore_cleanup_handles_file_symlink_and_missing_paths(tmp_path: Path) -> None:
    regular = tmp_path / "regular"
    regular.write_text("temporary")
    RestoreEngine._remove_tree(regular)
    assert not regular.exists()
    target = tmp_path / "target"
    target.write_text("target")
    link = tmp_path / "link"
    link.symlink_to(target)
    RestoreEngine._remove_tree(link)
    assert not link.exists()
    RestoreEngine._remove_tree(tmp_path / "missing")


def canonical_manifest(entries: list[dict[str, object]], *, schema: int = 1) -> dict[str, object]:
    value: dict[str, object] = {
        "createdAt": "2026-01-01T00:00:00Z",
        "entries": entries,
        "generation": 1,
        "sandboxId": SANDBOX_ID,
        "schemaVersion": schema,
    }
    if schema == 1:
        value["categories"] = list(PERSISTENCE_CATEGORIES)
    return value


def file_entry(**changes: object) -> dict[str, object]:
    digest = hashlib.sha256(b"data").hexdigest()
    value: dict[str, object] = {
        "chunks": [digest],
        "digest": digest,
        "mode": 0o600,
        "mtimeNs": 1,
        "path": "user/file",
        "size": 4,
        "type": "file",
    }
    value.update(changes)
    return value


def decode(decoder: ManifestDecoder, value: object) -> PersistenceManifest:
    return decoder.decode(json.dumps(value).encode(), SANDBOX_ID, 1)


def test_manifest_decoder_supports_v0_migration_and_chunk_references() -> None:
    decoder = ManifestDecoder()
    entry = file_entry()
    decoded = decode(decoder, canonical_manifest([entry], schema=0))
    assert decoded.schema_version == 1
    raw = json.dumps(canonical_manifest([entry])).encode()
    assert decoder.chunk_references(raw) == frozenset(cast(list[str], entry["chunks"]))
    with pytest.raises(ValueError, match="limits"):
        ManifestDecoder(max_files=0)


@pytest.mark.parametrize(
    "value,match",
    [
        (b"not-json", "JSON"),
        (b"[]", "root"),
        (json.dumps({"schemaVersion": "1"}).encode(), "schema"),
        (json.dumps({"schemaVersion": 2}).encode(), "unsupported"),
        (
            json.dumps({"schemaVersion": 0, "entries": []}).encode(),
            "sandbox or generation",
        ),
    ],
)
def test_manifest_decoder_rejects_bad_roots_and_versions(value: bytes, match: str) -> None:
    with pytest.raises(ManifestValidationError, match=match):
        ManifestDecoder().decode(value, SANDBOX_ID, 1)


@pytest.mark.parametrize(
    "entry",
    [
        "not-object",
        file_entry(path="../escape"),
        file_entry(path="user/../escape"),
        file_entry(mode=True),
        file_entry(mode=0o10000),
        file_entry(mtimeNs=-1),
        file_entry(size=-1),
        file_entry(type="fifo"),
        file_entry(digest="bad"),
        file_entry(chunks="bad"),
        file_entry(chunks=[]),
        file_entry(symlinkTarget="target"),
        {"path": "user/dir", "type": "directory", "mode": 0o700, "mtimeNs": 1, "size": 1},
        {
            "path": "user/link",
            "type": "symlink",
            "mode": 0o777,
            "mtimeNs": 1,
            "size": 1,
            "symlinkTarget": "/etc/passwd",
        },
        {
            "path": "user/link",
            "type": "symlink",
            "mode": 0o777,
            "mtimeNs": 1,
            "size": 1,
            "symlinkTarget": "../../escape",
        },
    ],
)
def test_manifest_decoder_rejects_unsafe_or_inconsistent_entries(entry: object) -> None:
    with pytest.raises(ManifestValidationError):
        decode(ManifestDecoder(), canonical_manifest([cast(dict[str, object], entry)]))


def test_manifest_decoder_rejects_metadata_limits_duplicates_and_unsorted_entries() -> None:
    base = canonical_manifest([file_entry()])
    mutations: list[dict[str, object]] = []
    wrong_sandbox = dict(base)
    wrong_sandbox["sandboxId"] = "other"
    mutations.append(wrong_sandbox)
    no_created = dict(base)
    no_created["createdAt"] = 1
    mutations.append(no_created)
    bad_categories = dict(base)
    bad_categories["categories"] = []
    mutations.append(bad_categories)
    duplicates = dict(base)
    duplicates["entries"] = [file_entry(), file_entry()]
    mutations.append(duplicates)
    unsorted = dict(base)
    unsorted["entries"] = [file_entry(path="user/z"), file_entry(path="user/a")]
    mutations.append(unsorted)
    for value in mutations:
        with pytest.raises(ManifestValidationError):
            decode(ManifestDecoder(), value)
    with pytest.raises(ManifestValidationError, match="limits"):
        decode(
            ManifestDecoder(max_files=0 + 1),
            canonical_manifest([file_entry(), file_entry(path="user/b")]),
        )
    with pytest.raises(ManifestValidationError, match="size"):
        decode(ManifestDecoder(max_bytes=3), base)


def test_chunk_reference_validation_and_migration_guards() -> None:
    decoder = ManifestDecoder()
    with pytest.raises(ManifestValidationError, match="JSON"):
        decoder.chunk_references(b"bad")
    with pytest.raises(ManifestValidationError, match="root"):
        decoder.chunk_references(b"[]")
    with pytest.raises(ManifestValidationError, match="entries"):
        decoder.chunk_references(b"{}")
    for entries in [["bad"], [{"chunks": "bad"}], [{"chunks": ["bad"]}]]:
        with pytest.raises(ManifestValidationError):
            decoder.chunk_references(json.dumps({"entries": entries}).encode())

    class StuckMigrator(ManifestMigrator):
        def __init__(self) -> None:
            super().__init__()
            self._migrations[0] = lambda value: value

    with pytest.raises(ManifestValidationError, match="did not advance"):
        StuckMigrator().migrate({"schemaVersion": 0})
    migrator = ManifestMigrator()
    migrator._migrations.clear()
    with pytest.raises(ManifestValidationError, match="unavailable"):
        migrator.migrate({"schemaVersion": 0})


def test_restore_constructor_and_materializer_defensive_checks(tmp_path: Path) -> None:
    _, _, broker, store = storage()
    with pytest.raises(ValueError, match="Runtime version"):
        RestoreEngine(tmp_path, SANDBOX_ID, "", store, broker.cipher(SANDBOX_ID))
    source = tmp_path / "source"
    representative_workspace(source, "data")
    create_generation(source, 1, broker, store)
    original_decode = ManifestDecoder.decode

    def impossible_manifest(
        self: ManifestDecoder, raw: bytes, sandbox_id: str, generation: int
    ) -> object:
        result = original_decode(self, raw, sandbox_id, generation)
        entries = list(result.entries)
        link_index = next(index for index, entry in enumerate(entries) if entry.type == "symlink")
        entries[link_index] = __import__("dataclasses").replace(
            entries[link_index], symlink_target=None
        )
        return __import__("dataclasses").replace(result, entries=tuple(entries))

    decoder = ManifestDecoder()
    decoder.decode = impossible_manifest.__get__(decoder, ManifestDecoder)  # type: ignore[method-assign]
    target = tmp_path / "target"
    restorer = RestoreEngine(
        target,
        SANDBOX_ID,
        "v2",
        store,
        broker.cipher(SANDBOX_ID),
        decoder=decoder,
    )
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        restorer.restore(existing_sandbox=True)
