from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final, Literal, cast

from kirocrew_agentcore_persistence.crypto import EncryptedBlob, IntegrityError, SandboxCipher
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    BrokeredCheckpointStore,
    CommittedGeneration,
    ObjectNotFoundError,
)
from kirocrew_agentcore_persistence.manifest import (
    MANIFEST_SCHEMA_VERSION,
    PERSISTENCE_CATEGORIES,
    ManifestEntry,
    PersistenceManifest,
    PersistencePolicy,
)

MAX_RESTORE_FILES: Final = 100_000
MAX_RESTORE_BYTES: Final = 50 * 1024 * 1024 * 1024
RestoreOutcome = Literal["initialized", "local", "restored", "fallback", "failed"]


class RestoreError(RuntimeError):
    """No validated generation could be installed safely."""


class ManifestValidationError(RestoreError):
    """A decrypted manifest violates the persistence contract."""


@dataclass(frozen=True, slots=True)
class RestoreConditions:
    managed_session_expired: bool = False
    runtime_version_reset: bool = False
    local_integrity_ok: bool = True


@dataclass(frozen=True, slots=True)
class RestoreReport:
    outcome: RestoreOutcome
    generation: int | None
    fallback_used: bool
    reasons: tuple[str, ...]
    attempted_generations: tuple[int, ...]
    completed_at: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "attemptedGenerations": list(self.attempted_generations),
            "completedAt": self.completed_at.isoformat().replace("+00:00", "Z"),
            "fallbackUsed": self.fallback_used,
            "generation": self.generation,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "schemaVersion": 1,
        }


class ManifestMigrator:
    def __init__(self) -> None:
        self._migrations: dict[int, Callable[[dict[str, object]], dict[str, object]]] = {
            0: self._zero_to_one
        }

    def migrate(self, value: dict[str, object]) -> dict[str, object]:
        schema = value.get("schemaVersion")
        if type(schema) is not int:
            raise ManifestValidationError("Manifest schema version is invalid.")
        migrated = dict(value)
        while schema < MANIFEST_SCHEMA_VERSION:
            migration = self._migrations.get(schema)
            if migration is None:
                raise ManifestValidationError("Manifest migration path is unavailable.")
            migrated = migration(migrated)
            next_schema = migrated.get("schemaVersion")
            if type(next_schema) is not int or next_schema <= schema:
                raise ManifestValidationError("Manifest migration did not advance the schema.")
            schema = next_schema
        if schema != MANIFEST_SCHEMA_VERSION:
            raise ManifestValidationError("Manifest schema version is unsupported.")
        return migrated

    @staticmethod
    def _zero_to_one(value: dict[str, object]) -> dict[str, object]:
        migrated = dict(value)
        migrated["schemaVersion"] = 1
        migrated.setdefault("categories", list(PERSISTENCE_CATEGORIES))
        return migrated


class ManifestDecoder:
    def __init__(
        self,
        *,
        policy: PersistencePolicy | None = None,
        migrator: ManifestMigrator | None = None,
        max_files: int = MAX_RESTORE_FILES,
        max_bytes: int = MAX_RESTORE_BYTES,
    ) -> None:
        if max_files <= 0 or max_bytes <= 0:
            raise ValueError("Restore limits must be positive.")
        self._policy = policy or PersistencePolicy()
        self._migrator = migrator or ManifestMigrator()
        self._max_files = max_files
        self._max_bytes = max_bytes

    def decode(self, raw: bytes, sandbox_id: str, generation: int) -> PersistenceManifest:
        try:
            loaded = cast(object, json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestValidationError("Manifest JSON is invalid.") from error
        if not isinstance(loaded, dict):
            raise ManifestValidationError("Manifest root must be an object.")
        value = self._migrator.migrate(cast(dict[str, object], loaded))
        if value.get("sandboxId") != sandbox_id or value.get("generation") != generation:
            raise ManifestValidationError("Manifest sandbox or generation does not match.")
        created_at = value.get("createdAt")
        entries_value = value.get("entries")
        categories = value.get("categories")
        if (
            not isinstance(created_at, str)
            or not isinstance(entries_value, list)
            or categories != list(PERSISTENCE_CATEGORIES)
            or len(entries_value) > self._max_files
        ):
            raise ManifestValidationError("Manifest metadata or limits are invalid.")
        entries: list[ManifestEntry] = []
        seen: set[str] = set()
        total_bytes = 0
        for item in entries_value:
            entry = self._entry(item)
            if entry is None:
                continue
            if entry.path in seen:
                raise ManifestValidationError("Manifest paths must be unique.")
            seen.add(entry.path)
            total_bytes += entry.size
            if total_bytes > self._max_bytes:
                raise ManifestValidationError("Manifest exceeds the restore size limit.")
            entries.append(entry)
        if tuple(entry.path for entry in entries) != tuple(sorted(seen)):
            raise ManifestValidationError("Manifest entries must be sorted.")
        return PersistenceManifest(
            MANIFEST_SCHEMA_VERSION, generation, sandbox_id, created_at, tuple(entries)
        )

    def chunk_references(self, raw: bytes) -> frozenset[str]:
        try:
            loaded = cast(object, json.loads(raw))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManifestValidationError("Manifest JSON is invalid.") from error
        if not isinstance(loaded, dict):
            raise ManifestValidationError("Manifest root must be an object.")
        entries = loaded.get("entries")
        if not isinstance(entries, list):
            raise ManifestValidationError("Manifest entries are invalid.")
        references: set[str] = set()
        for item in entries:
            if not isinstance(item, dict):
                raise ManifestValidationError("Manifest entry is invalid.")
            chunks = item.get("chunks", [])
            if not isinstance(chunks, list) or not all(self._digest(value) for value in chunks):
                raise ManifestValidationError("Manifest chunk reference is invalid.")
            references.update(cast(list[str], chunks))
        return frozenset(references)

    def _entry(self, value: object) -> ManifestEntry | None:
        if not isinstance(value, dict):
            raise ManifestValidationError("Manifest entry must be an object.")
        path = value.get("path")
        entry_type = value.get("type")
        mode = value.get("mode")
        mtime = value.get("mtimeNs")
        size = value.get("size")
        if (
            not isinstance(path, str)
            or entry_type not in {"file", "directory", "symlink"}
            or type(mode) is not int
            or type(mtime) is not int
            or type(size) is not int
            or mode < 0
            or mode > 0o7777
            or mtime < 0
            or size < 0
        ):
            raise ManifestValidationError("Manifest entry metadata is invalid.")
        relative = PurePosixPath(path)
        if path != relative.as_posix() or not self._policy.covers(relative):
            raise ManifestValidationError("Manifest path is outside durable roots.")
        if not self._policy.includes(relative):
            # Covered by a durable root but excluded by the current policy:
            # an older checkpoint legitimately carries entries a newer policy
            # no longer persists (the in-workspace embedding model). They are
            # history to skip, not a poisoned manifest - failing here would
            # brick every sandbox restored from a pre-exclusion checkpoint.
            return None
        digest_value = value.get("digest")
        chunks_value = value.get("chunks", [])
        target_value = value.get("symlinkTarget")
        if not isinstance(chunks_value, list) or not all(
            self._digest(item) for item in chunks_value
        ):
            raise ManifestValidationError("Manifest chunks are invalid.")
        if entry_type == "file":
            if not self._digest(digest_value) or not chunks_value or target_value is not None:
                raise ManifestValidationError("File manifest entry is invalid.")
        elif entry_type == "directory":
            if size != 0 or digest_value is not None or chunks_value or target_value is not None:
                raise ManifestValidationError("Directory manifest entry is invalid.")
        else:
            if (
                digest_value is not None
                or chunks_value
                or not isinstance(target_value, str)
                or not self._safe_symlink(relative, target_value)
            ):
                raise ManifestValidationError("Symlink manifest entry is invalid.")
        return ManifestEntry(
            path,
            cast(Literal["file", "directory", "symlink"], entry_type),
            mode,
            mtime,
            size,
            cast(str | None, digest_value),
            tuple(cast(list[str], chunks_value)),
            target_value,
        )

    @staticmethod
    def _digest(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _safe_symlink(path: PurePosixPath, target: str) -> bool:
        candidate = PurePosixPath(target)
        if candidate.is_absolute():
            return False
        depth = len(path.parent.parts)
        for part in candidate.parts:
            if part == "..":
                depth -= 1
                if depth < 0:
                    return False
            else:
                depth += 1
        return True


def _replace_path(source: Path, destination: Path) -> None:
    try:
        source.replace(destination)
        return
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    # The entry sits on its own mount (the fleet bind-mounts home inside the
    # workspace), so a rename can never leave it. Copy it across and empty
    # the source; the mount-point directory itself stays behind.
    if source.is_dir() and not source.is_symlink():
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
        _remove_tree_tolerating_mounts(source)
    else:
        if destination.is_symlink() or destination.exists():
            destination.unlink()
        if source.is_symlink():
            destination.symlink_to(source.readlink())
            source.unlink()
        else:
            shutil.copy2(source, destination)
            source.unlink()


def _remove_tree_tolerating_mounts(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    if not path.exists():
        return
    for child in path.iterdir():
        _remove_tree_tolerating_mounts(child)
    try:
        path.rmdir()
    except OSError as error:
        if error.errno != errno.EBUSY:
            raise


class RestoreEngine:
    def __init__(
        self,
        workspace: Path,
        sandbox_id: str,
        runtime_version: str,
        store: BrokeredCheckpointStore,
        cipher: SandboxCipher,
        *,
        decoder: ManifestDecoder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        replace: Callable[[Path, Path], None] = _replace_path,
        fetch_workers: int = 16,
    ) -> None:
        if not runtime_version:
            raise ValueError("Runtime version is required.")
        if fetch_workers < 1:
            raise ValueError("At least one fetch worker is required.")
        self._workspace = workspace
        self._sandbox_id = sandbox_id
        self._runtime_version = runtime_version
        self._store = store
        self._cipher = cipher
        self._decoder = decoder or ManifestDecoder()
        self._clock = clock
        self._replace = replace
        self._fetch_workers = fetch_workers

    def restore(
        self,
        *,
        existing_sandbox: bool,
        conditions: RestoreConditions | None = None,
    ) -> RestoreReport:
        selected_conditions = conditions or RestoreConditions()
        committed = self._store.committed_generations()
        if not committed:
            if existing_sandbox:
                report = self._report("failed", None, False, ("no-retained-generation",), ())
                self._write_report(report)
                raise RestoreError("PERSISTENCE_RESTORE_FAILED")
            self._workspace.mkdir(parents=True, exist_ok=True)
            report = self._report("initialized", None, False, ("new-sandbox",), ())
            self._write_report(report)
            return report
        latest = committed[-1]
        reasons = self._restore_reasons(latest, selected_conditions)
        if not reasons:
            report = self._report("local", latest.generation, False, (), ())
            self._write_report(report)
            return report
        attempts: list[int] = []
        failures: list[str] = []
        for index, generation in enumerate(reversed(committed[-2:])):
            attempts.append(generation.generation)
            try:
                self._restore_generation(generation)
            except (
                # A broker rejection mid-restore (token expiry, throttling)
                # must fall back to the previous generation like any other
                # invalid-generation condition, never abort initialization.
                BrokerAuthorizationError,
                IntegrityError,
                ManifestValidationError,
                ObjectNotFoundError,
                OSError,
            ) as error:
                # The class name and errno-style detail carry no payload data
                # and are essential for diagnosing fleet-only failures.
                failures.append(
                    f"generation-{generation.generation}-invalid:{type(error).__name__}:{error}"[
                        :200
                    ]
                )
                continue
            outcome: RestoreOutcome = "fallback" if index else "restored"
            report = self._report(
                outcome,
                generation.generation,
                index > 0,
                tuple(reasons + failures),
                tuple(attempts),
            )
            self._write_report(report)
            return report
        report = self._report("failed", None, False, tuple(reasons + failures), tuple(attempts))
        self._write_report(report)
        raise RestoreError("PERSISTENCE_RESTORE_FAILED " + "; ".join(reasons + failures))

    def _restore_reasons(
        self, latest: CommittedGeneration, conditions: RestoreConditions
    ) -> list[str]:
        reasons: list[str] = []
        state = self._local_state()
        if state is None:
            reasons.append("missing-or-empty-managed-storage")
        else:
            if (
                state.get("generation") != latest.generation
                or state.get("manifestDigest") != latest.manifest_digest
            ):
                reasons.append("local-generation-mismatch")
            if state.get("runtimeVersion") != self._runtime_version:
                reasons.append("runtime-version-reset")
        if conditions.managed_session_expired:
            reasons.append("managed-session-expired")
        if conditions.runtime_version_reset and "runtime-version-reset" not in reasons:
            reasons.append("runtime-version-reset")
        if not conditions.local_integrity_ok:
            reasons.append("local-integrity-failed")
        return reasons

    def _local_state(self) -> dict[str, object] | None:
        state_path = self._workspace / ".agentcore/persistence.json"
        if not state_path.is_file():
            return None
        try:
            value = cast(object, json.loads(state_path.read_bytes()))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        return cast(dict[str, object], value) if isinstance(value, dict) else None

    def _restore_generation(self, committed: CommittedGeneration) -> None:
        manifest_blob = self._store.get_manifest(committed.generation)
        if manifest_blob.digest != committed.manifest_digest:
            raise IntegrityError("Committed manifest digest does not match object.")
        plaintext = self._cipher.decrypt(manifest_blob)
        manifest = self._decoder.decode(plaintext, self._sandbox_id, committed.generation)
        # Stage inside the workspace: in the fleet the workspace is the only
        # writable mount and its parent rejects writes, and .agentcore is
        # already excluded from checkpoints.
        stage = self._workspace / ".agentcore/restore-stage"
        backup = self._workspace / ".agentcore/restore-backup"
        self._workspace.mkdir(parents=True, exist_ok=True)
        self._remove_tree(stage)
        self._remove_tree(backup)
        stage.mkdir(parents=True, exist_ok=True)
        try:
            self._materialize(stage, manifest)
            self._write_state(stage, committed)
            self._install(stage, backup)
        finally:
            self._remove_tree(stage)

    def _materialize(self, stage: Path, manifest: PersistenceManifest) -> None:
        # Sequential chunk fetches took minutes for real workspaces, which
        # held cold starts open long enough for client retries to fan out
        # into concurrent sessions. Prefetching in parallel keeps a restore
        # inside one invocation.
        remaining = Counter(
            digest for entry in manifest.entries if entry.type == "file" for digest in entry.chunks
        )
        with ThreadPoolExecutor(max_workers=self._fetch_workers) as executor:
            futures = {digest: executor.submit(self._fetch_chunk, digest) for digest in remaining}
            directories: list[tuple[Path, ManifestEntry]] = []
            try:
                for entry in manifest.entries:
                    destination = stage / entry.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if entry.type == "directory":
                        destination.mkdir(exist_ok=True)
                        directories.append((destination, entry))
                    elif entry.type == "symlink":
                        if entry.symlink_target is None:
                            raise ManifestValidationError("Symlink target is missing.")
                        os.symlink(entry.symlink_target, destination)
                        os.utime(
                            destination,
                            ns=(entry.mtime_ns, entry.mtime_ns),
                            follow_symlinks=False,
                        )
                    else:
                        whole = hashlib.sha256()
                        size = 0
                        with destination.open("wb") as output:
                            for digest in entry.chunks:
                                chunk = futures[digest].result()
                                remaining[digest] -= 1
                                if remaining[digest] == 0:
                                    # Drop delivered chunks so a large
                                    # workspace never sits in memory whole.
                                    del futures[digest]
                                output.write(chunk)
                                whole.update(chunk)
                                size += len(chunk)
                        if size != entry.size or whole.hexdigest() != entry.digest:
                            raise IntegrityError("Restored file content does not match manifest.")
                        destination.chmod(entry.mode)
                        os.utime(destination, ns=(entry.mtime_ns, entry.mtime_ns))
            finally:
                for future in futures.values():
                    future.cancel()
        for destination, entry in reversed(directories):
            destination.chmod(entry.mode)
            os.utime(destination, ns=(entry.mtime_ns, entry.mtime_ns))

    def _fetch_chunk(self, digest: str) -> bytes:
        return self._cipher.decrypt(EncryptedBlob(digest, self._store.get_chunk(digest)))

    def _install(self, stage: Path, backup: Path) -> None:
        """Swap the workspace contents for the staged tree, entry by entry.

        The workspace itself is a mount point in the fleet, so it can never
        be renamed wholesale; every move stays inside it and is undone if a
        later move fails.
        """
        state_destination = self._workspace / ".agentcore/persistence.json"
        backup.mkdir(parents=True)
        moved: list[str] = []
        installed: list[str] = []
        state_backed_up = False
        try:
            if state_destination.exists():
                self._replace(state_destination, backup / "persistence.json")
                state_backed_up = True
            for entry in sorted(self._workspace.iterdir()):
                if entry.name == ".agentcore":
                    continue
                self._replace(entry, backup / entry.name)
                moved.append(entry.name)
            for entry in sorted(stage.iterdir()):
                if entry.name == ".agentcore":
                    continue
                self._replace(entry, self._workspace / entry.name)
                installed.append(entry.name)
            state_destination.parent.mkdir(parents=True, exist_ok=True)
            self._replace(stage / ".agentcore/persistence.json", state_destination)
        except OSError:
            for name in installed:
                self._remove_tree(self._workspace / name)
            for name in moved:
                source = backup / name
                if source.is_symlink() or source.exists():
                    self._remove_tree(self._workspace / name)
                    self._replace(source, self._workspace / name)
            if state_backed_up:
                self._remove_tree(state_destination)
                self._replace(backup / "persistence.json", state_destination)
            raise
        # The staged tree replaced every managed entry, so pre-restore dirty
        # journal state no longer describes the workspace.
        self._remove_tree(self._workspace / ".agentcore/dirty-journal")
        self._remove_tree(backup)

    def _write_state(self, root: Path, committed: CommittedGeneration) -> None:
        state = root / ".agentcore/persistence.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps(
                {
                    "generation": committed.generation,
                    "manifestDigest": committed.manifest_digest,
                    "runtimeVersion": self._runtime_version,
                    "schemaVersion": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        state.chmod(0o600)

    def _report(
        self,
        outcome: RestoreOutcome,
        generation: int | None,
        fallback: bool,
        reasons: tuple[str, ...],
        attempts: tuple[int, ...],
    ) -> RestoreReport:
        return RestoreReport(outcome, generation, fallback, reasons, attempts, self._clock())

    def _write_report(self, report: RestoreReport) -> None:
        destination = self._workspace / ".agentcore/restore-report.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report.as_dict(), sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(destination)

    @staticmethod
    def _remove_tree(path: Path) -> None:
        _remove_tree_tolerating_mounts(path)
