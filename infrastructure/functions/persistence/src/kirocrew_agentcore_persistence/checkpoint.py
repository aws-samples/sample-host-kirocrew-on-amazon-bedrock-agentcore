from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

from kirocrew_agentcore_persistence.crypto import EncryptedBlob, SandboxCipher
from kirocrew_agentcore_persistence.journal import DirtyJournal
from kirocrew_agentcore_persistence.manifest import BuiltManifest, ManifestBuilder

DEFAULT_RECOVERY_POINT_OBJECTIVE: Final = timedelta(seconds=30)
CHECKPOINT_STEPS: Final = (
    "quiesced",
    "flushed",
    "manifest_built",
    "chunks_uploaded",
    "manifest_uploaded",
    "generation_committed",
    "local_state_written",
    "journal_cleared",
)


class CheckpointError(RuntimeError):
    """Checkpoint did not complete every atomic commit step."""


class ReadOnlyPersistenceError(CheckpointError):
    """Durability is unavailable and writes must remain disabled."""


class Quiescer(Protocol):
    def pause(self) -> None: ...

    def resume(self) -> None: ...


class Flusher(Protocol):
    def flush(self, workspace: Path) -> None: ...


class CheckpointStore(Protocol):
    def has_chunk(self, digest: str) -> bool: ...

    def upload_chunk(self, digest: str, ciphertext: bytes) -> None: ...

    def upload_manifest(self, generation: int, blob: EncryptedBlob) -> None: ...

    def commit_generation(self, generation: int, manifest_digest: str) -> None: ...


class SystemFlusher:
    def flush(self, workspace: Path) -> None:
        if not workspace.exists():
            raise CheckpointError("Workspace does not exist.")
        os.sync()


@dataclass(frozen=True, slots=True)
class CheckpointReceipt:
    generation: int
    manifest_digest: str
    committed_at: datetime
    dirty_paths: frozenset[str]


class InMemoryCheckpointStore:
    def __init__(self) -> None:
        self.chunks: dict[str, bytes] = {}
        self.manifests: dict[int, EncryptedBlob] = {}
        self.commits: dict[int, str] = {}

    def has_chunk(self, digest: str) -> bool:
        return digest in self.chunks

    def upload_chunk(self, digest: str, ciphertext: bytes) -> None:
        self.chunks.setdefault(digest, ciphertext)

    def upload_manifest(self, generation: int, blob: EncryptedBlob) -> None:
        self.manifests[generation] = blob

    def commit_generation(self, generation: int, manifest_digest: str) -> None:
        manifest = self.manifests.get(generation)
        if manifest is None or manifest.digest != manifest_digest:
            raise CheckpointError("Manifest must exist before generation commit.")
        if self.commits and generation <= max(self.commits):
            raise CheckpointError("Generation commit must advance monotonically.")
        self.commits[generation] = manifest_digest

    def latest_committed(self) -> int | None:
        return max(self.commits) if self.commits else None


class CheckpointEngine:
    def __init__(
        self,
        workspace: Path,
        sandbox_id: str,
        journal: DirtyJournal,
        builder: ManifestBuilder,
        cipher: SandboxCipher,
        store: CheckpointStore,
        quiescer: Quiescer,
        flusher: Flusher,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        recovery_point_objective: timedelta = DEFAULT_RECOVERY_POINT_OBJECTIVE,
        failure_injector: Callable[[str], None] = lambda _step: None,
        runtime_version: str = "development",
    ) -> None:
        if recovery_point_objective <= timedelta(0) or not runtime_version:
            raise ValueError(
                "Recovery-point objective must be positive and runtime version required."
            )
        self._workspace = workspace
        self._sandbox_id = sandbox_id
        self._runtime_version = runtime_version
        self._journal = journal
        self._builder = builder
        self._cipher = cipher
        self._store = store
        self._quiescer = quiescer
        self._flusher = flusher
        self._clock = clock
        self._rpo = recovery_point_objective
        self._failure_injector = failure_injector
        self._read_only = False

    @property
    def read_only(self) -> bool:
        return self._read_only

    def assert_writable(self) -> None:
        if self._read_only:
            raise ReadOnlyPersistenceError(
                "Sandbox is read-only because durability is unavailable."
            )
        self.enforce_rpo()

    def enforce_rpo(self) -> None:
        first_dirty_at = self._journal.snapshot().first_dirty_at
        if first_dirty_at is not None and self._clock() - first_dirty_at >= self._rpo:
            self._read_only = True
            raise ReadOnlyPersistenceError("Checkpoint recovery-point objective was breached.")

    def checkpoint(self, generation: int, *, final: bool = False) -> CheckpointReceipt:
        snapshot = self._journal.snapshot()
        paused = False
        completed = False
        try:
            self._quiescer.pause()
            paused = True
            self._inject("quiesced")
            self._flusher.flush(self._workspace)
            self._inject("flushed")
            now = self._clock()
            built = self._builder.build(
                generation,
                self._sandbox_id,
                now.isoformat().replace("+00:00", "Z"),
            )
            self._inject("manifest_built")
            self._upload_chunks(built)
            self._inject("chunks_uploaded")
            manifest_blob = self._cipher.encrypt(built.manifest.to_json())
            self._store.upload_manifest(generation, manifest_blob)
            self._inject("manifest_uploaded")
            self._store.commit_generation(generation, manifest_blob.digest)
            self._inject("generation_committed")
            self._write_local_state(generation, manifest_blob.digest)
            self._inject("local_state_written")
            self._journal.clear_through(snapshot.through_sequence)
            self._inject("journal_cleared")
            completed = True
            self._read_only = False
            return CheckpointReceipt(
                generation,
                manifest_blob.digest,
                now,
                snapshot.paths,
            )
        except ReadOnlyPersistenceError:
            raise
        except Exception as error:
            try:
                self.enforce_rpo()
            except ReadOnlyPersistenceError as rpo_error:
                raise rpo_error from error
            raise CheckpointError("Checkpoint did not commit completely.") from error
        finally:
            if paused and (not final or not completed):
                self._quiescer.resume()

    def _upload_chunks(self, built: BuiltManifest) -> None:
        for digest, plaintext in sorted(built.chunks.items()):
            if self._store.has_chunk(digest):
                continue
            blob = self._cipher.encrypt(plaintext)
            if blob.digest != digest:
                raise CheckpointError("Chunk digest changed during encryption.")
            self._store.upload_chunk(digest, blob.ciphertext)

    def _write_local_state(self, generation: int, manifest_digest: str) -> None:
        directory = self._workspace / ".agentcore"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "persistence.json"
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "generation": generation,
                    "manifestDigest": manifest_digest,
                    "runtimeVersion": self._runtime_version,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(destination)

    def _inject(self, step: str) -> None:
        self._failure_injector(step)
