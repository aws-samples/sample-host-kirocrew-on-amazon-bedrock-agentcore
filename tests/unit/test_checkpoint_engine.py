from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kirocrew_agentcore_persistence.checkpoint import (
    CHECKPOINT_STEPS,
    CheckpointEngine,
    CheckpointError,
    InMemoryCheckpointStore,
    ReadOnlyPersistenceError,
    SystemFlusher,
)
from kirocrew_agentcore_persistence.crypto import EncryptedBlob, SandboxCipher
from kirocrew_agentcore_persistence.journal import DirtyJournal
from kirocrew_agentcore_persistence.manifest import ManifestBuilder

SANDBOX_ID = "sbx_01J00000000000000000000000"


@dataclass
class MutableClock:
    now: datetime = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeQuiescer:
    def __init__(self) -> None:
        self.pause_count = 0
        self.resume_count = 0

    def pause(self) -> None:
        self.pause_count += 1

    def resume(self) -> None:
        self.resume_count += 1


class FakeFlusher:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls = 0

    def flush(self, workspace: Path) -> None:
        assert workspace.exists()
        self.calls += 1
        if self.failure is not None:
            raise self.failure


class BadDigestCipher(SandboxCipher):
    def encrypt(self, plaintext: bytes) -> EncryptedBlob:
        encrypted = super().encrypt(plaintext)
        if plaintext.startswith(b"file"):
            return EncryptedBlob("0" * 64, encrypted.ciphertext)
        return encrypted


def setup_workspace(path: Path) -> DirtyJournal:
    durable = path / "user/file.txt"
    durable.parent.mkdir(parents=True)
    durable.write_text("file content", encoding="utf-8")
    journal = DirtyJournal(path)
    journal.mark("user/file.txt", datetime(2026, 8, 17, 16, 0, tzinfo=UTC))
    return journal


def engine(
    workspace: Path,
    *,
    clock: MutableClock | None = None,
    store: InMemoryCheckpointStore | None = None,
    quiescer: FakeQuiescer | None = None,
    flusher: FakeFlusher | None = None,
    injector: Callable[[str], None] | None = None,
    cipher: SandboxCipher | None = None,
    rpo: timedelta = timedelta(seconds=30),
) -> tuple[
    CheckpointEngine,
    DirtyJournal,
    InMemoryCheckpointStore,
    FakeQuiescer,
    FakeFlusher,
]:
    journal = setup_workspace(workspace)
    selected_store = store or InMemoryCheckpointStore()
    selected_quiescer = quiescer or FakeQuiescer()
    selected_flusher = flusher or FakeFlusher()
    selected_clock = clock or MutableClock()
    failure_injector = injector or (lambda _step: None)
    checkpoint = CheckpointEngine(
        workspace,
        SANDBOX_ID,
        journal,
        ManifestBuilder(workspace, chunk_size=4),
        cipher or SandboxCipher(b"k" * 32, SANDBOX_ID),
        selected_store,
        selected_quiescer,
        selected_flusher,
        clock=selected_clock,
        recovery_point_objective=rpo,
        failure_injector=failure_injector,
    )
    return checkpoint, journal, selected_store, selected_quiescer, selected_flusher


def test_successful_periodic_checkpoint_commits_then_clears_and_resumes(tmp_path: Path) -> None:
    checkpoint, journal, store, quiescer, flusher = engine(tmp_path / "workspace")
    receipt = checkpoint.checkpoint(1)
    assert receipt.generation == 1
    assert receipt.dirty_paths == frozenset({"user/file.txt"})
    assert store.latest_committed() == 1
    assert store.commits[1] == receipt.manifest_digest
    assert journal.snapshot().paths == frozenset()
    assert quiescer.pause_count == quiescer.resume_count == 1
    assert flusher.calls == 1
    local = tmp_path / "workspace/.agentcore/persistence.json"
    assert json.loads(local.read_text(encoding="utf-8"))["generation"] == 1
    assert local.stat().st_mode & 0o777 == 0o600
    assert not checkpoint.read_only


def test_final_checkpoint_leaves_processes_quiesced(tmp_path: Path) -> None:
    checkpoint, _, _, quiescer, _ = engine(tmp_path / "workspace")
    checkpoint.checkpoint(1, final=True)
    assert quiescer.pause_count == 1
    assert quiescer.resume_count == 0


@pytest.mark.parametrize("failure_step", CHECKPOINT_STEPS)
def test_failure_injection_never_selects_an_uncommitted_generation(
    tmp_path: Path, failure_step: str
) -> None:
    def inject(step: str) -> None:
        if step == failure_step:
            raise RuntimeError(f"crash after {step}")

    checkpoint, journal, store, quiescer, _ = engine(tmp_path / failure_step, injector=inject)
    with pytest.raises(CheckpointError, match="did not commit"):
        checkpoint.checkpoint(1)
    committed_steps = {
        "generation_committed",
        "local_state_written",
        "journal_cleared",
    }
    assert store.latest_committed() == (1 if failure_step in committed_steps else None)
    expected_dirty = (
        frozenset() if failure_step == "journal_cleared" else frozenset({"user/file.txt"})
    )
    assert journal.snapshot().paths == expected_dirty
    assert quiescer.pause_count == quiescer.resume_count == 1


def test_mutation_arriving_during_checkpoint_remains_in_journal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    journal_holder: list[DirtyJournal] = []

    def inject(step: str) -> None:
        if step == "manifest_uploaded":
            journal_holder[0].mark("user/second.txt", datetime(2026, 8, 17, 16, 0, 1, tzinfo=UTC))

    checkpoint, journal, _, _, _ = engine(workspace, injector=inject)
    journal_holder.append(journal)
    checkpoint.checkpoint(1)
    assert journal.snapshot().paths == frozenset({"user/second.txt"})


def test_unchanged_chunks_are_deduplicated_across_generations(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    checkpoint, journal, store, _, _ = engine(workspace)
    checkpoint.checkpoint(1)
    chunk_count = len(store.chunks)
    journal.mark("user/file.txt", datetime(2026, 8, 17, 16, 0, 2, tzinfo=UTC))
    checkpoint.checkpoint(2)
    assert len(store.chunks) == chunk_count
    assert store.latest_committed() == 2


def test_rpo_breach_enters_and_retains_read_only_state(tmp_path: Path) -> None:
    clock = MutableClock()
    checkpoint, _, _, _, _ = engine(tmp_path / "workspace", clock=clock)
    checkpoint.assert_writable()
    clock.advance(timedelta(seconds=30))
    with pytest.raises(ReadOnlyPersistenceError, match="breached"):
        checkpoint.enforce_rpo()
    assert checkpoint.read_only
    with pytest.raises(ReadOnlyPersistenceError, match="read-only"):
        checkpoint.assert_writable()


def test_checkpoint_failure_at_rpo_boundary_reports_read_only(tmp_path: Path) -> None:
    clock = MutableClock()
    workspace = tmp_path / "workspace"
    checkpoint, _, _, quiescer, _ = engine(
        workspace,
        clock=clock,
        flusher=FakeFlusher(RuntimeError("storage unavailable")),
    )
    clock.advance(timedelta(seconds=31))
    with pytest.raises(ReadOnlyPersistenceError, match="breached"):
        checkpoint.checkpoint(1)
    assert checkpoint.read_only
    assert quiescer.resume_count == 1


def test_engine_rejects_nonpositive_rpo(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="positive"):
        CheckpointEngine(
            workspace,
            SANDBOX_ID,
            DirtyJournal(workspace),
            ManifestBuilder(workspace),
            SandboxCipher(b"k" * 32, SANDBOX_ID),
            InMemoryCheckpointStore(),
            FakeQuiescer(),
            FakeFlusher(),
            recovery_point_objective=timedelta(0),
        )


def test_store_requires_manifest_and_monotonic_generation() -> None:
    store = InMemoryCheckpointStore()
    assert not store.has_chunk("digest")
    store.upload_chunk("digest", b"ciphertext")
    store.upload_chunk("digest", b"ignored")
    assert store.chunks["digest"] == b"ciphertext"
    assert store.latest_committed() is None
    with pytest.raises(CheckpointError, match="Manifest"):
        store.commit_generation(1, "missing")
    blob = EncryptedBlob("manifest", b"ciphertext")
    store.upload_manifest(1, blob)
    with pytest.raises(CheckpointError, match="Manifest"):
        store.commit_generation(1, "wrong")
    store.commit_generation(1, "manifest")
    with pytest.raises(CheckpointError, match="monotonically"):
        store.commit_generation(1, "manifest")


def test_chunk_digest_change_is_rejected(tmp_path: Path) -> None:
    checkpoint, journal, store, quiescer, _ = engine(
        tmp_path / "workspace",
        cipher=BadDigestCipher(b"k" * 32, SANDBOX_ID),
    )
    with pytest.raises(CheckpointError, match="did not commit"):
        checkpoint.checkpoint(1)
    assert store.latest_committed() is None
    assert journal.snapshot().paths
    assert quiescer.resume_count == 1


def test_system_flusher_requires_workspace_and_calls_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flusher = SystemFlusher()
    with pytest.raises(CheckpointError, match="does not exist"):
        flusher.flush(tmp_path / "missing")
    calls: list[bool] = []
    monkeypatch.setattr("os.sync", lambda: calls.append(True))
    flusher.flush(tmp_path)
    assert calls == [True]


def test_direct_read_only_failure_is_preserved_and_resumes_processes(tmp_path: Path) -> None:
    def inject(step: str) -> None:
        if step == "quiesced":
            raise ReadOnlyPersistenceError("durability unavailable")

    checkpoint, _, _, quiescer, _ = engine(tmp_path / "workspace", injector=inject)
    with pytest.raises(ReadOnlyPersistenceError, match="durability unavailable"):
        checkpoint.checkpoint(1)
    assert quiescer.resume_count == 1
