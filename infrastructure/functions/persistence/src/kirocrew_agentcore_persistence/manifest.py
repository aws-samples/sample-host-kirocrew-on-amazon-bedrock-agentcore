from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Literal

MANIFEST_SCHEMA_VERSION: Final = 1
DEFAULT_CHUNK_SIZE: Final = 4 * 1024 * 1024
PERSISTENCE_CATEGORIES: Final = (
    "configuration",
    "agents",
    "workspace-projects",
    "memory",
    "knowledge",
    "artifacts",
    "skills",
    "conversation-session-metadata",
    "scheduled-jobs",
    "kiro-credential-state",
    "user-durable-state",
)
EntryType = Literal["file", "directory", "symlink"]


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    path: str
    type: EntryType
    mode: int
    mtime_ns: int
    size: int
    digest: str | None
    chunks: tuple[str, ...]
    symlink_target: str | None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "path": self.path,
            "type": self.type,
            "mode": self.mode,
            "mtimeNs": self.mtime_ns,
            "size": self.size,
        }
        if self.digest is not None:
            result["digest"] = self.digest
        if self.chunks:
            result["chunks"] = list(self.chunks)
        if self.symlink_target is not None:
            result["symlinkTarget"] = self.symlink_target
        return result


@dataclass(frozen=True, slots=True)
class PersistenceManifest:
    schema_version: int
    generation: int
    sandbox_id: str
    created_at: str
    entries: tuple[ManifestEntry, ...]

    def to_json(self) -> bytes:
        value = {
            "schemaVersion": self.schema_version,
            "generation": self.generation,
            "sandboxId": self.sandbox_id,
            "createdAt": self.created_at,
            "categories": list(PERSISTENCE_CATEGORIES),
            "entries": [entry.as_dict() for entry in self.entries],
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass(frozen=True, slots=True)
class BuiltManifest:
    manifest: PersistenceManifest
    chunks: dict[str, bytes]


class PersistencePolicy:
    _roots: Final = (
        PurePosixPath("home/.kiro"),
        PurePosixPath("home/.config"),
        # Kiro CLI keeps its sign-in (the auth_kv table of data.sqlite3) here, not
        # under ~/.kiro; without it every restore comes back signed out.
        PurePosixPath("home/.local/share/kiro-cli"),
        # The runtime creates these top-level directories next to the projects
        # root; anything a user or feature drops there must survive a restore.
        PurePosixPath("artifacts"),
        PurePosixPath("knowledge"),
        PurePosixPath("memory"),
        PurePosixPath("projects"),
        PurePosixPath("user"),
    )
    _excluded_segments: Final = frozenset(
        {
            ".agentcore",
            ".aws",
            ".cache",
            ".git/objects",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".ssh",
            "__pycache__",
            "node_modules",
            # The embedding model ships in the image (KIROCREW_EMBED_MODEL_PATH);
            # a legacy in-workspace copy is ~640MB of re-downloadable weights
            # that would bloat every checkpoint and eat the 1GB session quota.
            "home/.kiro/crew/models",
        }
    )
    _excluded_names: Final = frozenset(
        {".env", ".env.local", "adapter.run", "kirocrew.pid", "runtime.pid"}
    )
    _excluded_suffixes: Final = (".log", ".pid", ".sock", ".tmp", "~")

    def covers(self, relative_path: PurePosixPath) -> bool:
        """Whether the path lies inside the durable roots at all.

        Distinct from :meth:`includes`: a path can be covered by a root yet
        excluded by policy (caches, the in-image embedding model). Restore
        treats covered-but-excluded entries in an older manifest as
        skippable history rather than a validation failure.
        """
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
        return any(
            relative_path == root or relative_path.is_relative_to(root) for root in self._roots
        )

    def includes(self, relative_path: PurePosixPath) -> bool:
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
        if not any(
            relative_path == root or relative_path.is_relative_to(root) for root in self._roots
        ):
            return False
        joined = relative_path.as_posix()
        if any(
            excluded in relative_path.parts
            or joined == excluded
            or joined.startswith(f"{excluded}/")
            or f"/{excluded}/" in f"/{joined}/"
            for excluded in self._excluded_segments
        ):
            return False
        return relative_path.name not in self._excluded_names and not relative_path.name.endswith(
            self._excluded_suffixes
        )


def _walk_included_roots(workspace: Path) -> list[Path]:
    paths: list[Path] = []
    if not workspace.exists():
        return paths
    for root in PersistencePolicy._roots:
        candidate = workspace / root.as_posix()
        if not candidate.exists() and not candidate.is_symlink():
            continue
        paths.append(candidate)
        if candidate.is_dir() and not candidate.is_symlink():
            paths.extend(sorted(candidate.rglob("*")))
    return sorted(set(paths))


def workspace_fingerprint(workspace: Path, *, policy: PersistencePolicy | None = None) -> str:
    """A cheap identity of the durable workspace state.

    Only paths, sizes, and modification times of policy-included entries are
    hashed, never content, so callers can poll frequently to decide whether a
    full checkpoint is worth its cost. Any metadata change to an included
    entry changes the fingerprint.
    """
    selected = policy or PersistencePolicy()
    digest = hashlib.sha256()
    for path in _walk_included_roots(workspace):
        relative = PurePosixPath(path.relative_to(workspace).as_posix())
        if not selected.includes(relative):
            continue
        metadata = path.lstat()
        digest.update(
            f"{relative.as_posix()}\x00{metadata.st_mode}\x00"
            f"{metadata.st_mtime_ns}\x00{metadata.st_size}\x00".encode()
        )
    return digest.hexdigest()


class ManifestBuilder:
    def __init__(
        self,
        workspace: Path,
        *,
        policy: PersistencePolicy | None = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("Chunk size must be positive.")
        self._workspace = workspace
        self._policy = policy or PersistencePolicy()
        self._chunk_size = chunk_size

    def build(self, generation: int, sandbox_id: str, created_at: str) -> BuiltManifest:
        if generation <= 0 or not sandbox_id:
            raise ValueError("Positive generation and sandbox ID are required.")
        entries: list[ManifestEntry] = []
        chunks: dict[str, bytes] = {}
        for path in self._walk():
            relative = PurePosixPath(path.relative_to(self._workspace).as_posix())
            metadata = path.lstat()
            if not self._policy.includes(relative):
                continue
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(
                    ManifestEntry(
                        relative.as_posix(),
                        "directory",
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_mtime_ns,
                        0,
                        None,
                        (),
                        None,
                    )
                )
            elif stat.S_ISLNK(metadata.st_mode):
                target = str(path.readlink())
                entries.append(
                    ManifestEntry(
                        relative.as_posix(),
                        "symlink",
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_mtime_ns,
                        len(target.encode()),
                        None,
                        (),
                        target,
                    )
                )
            elif stat.S_ISREG(metadata.st_mode):
                file_chunks = self._read_chunks(path)
                chunk_digests: list[str] = []
                whole_file = hashlib.sha256()
                for data in file_chunks:
                    digest = hashlib.sha256(data).hexdigest()
                    chunks.setdefault(digest, data)
                    chunk_digests.append(digest)
                    whole_file.update(data)
                entries.append(
                    ManifestEntry(
                        relative.as_posix(),
                        "file",
                        stat.S_IMODE(metadata.st_mode),
                        metadata.st_mtime_ns,
                        metadata.st_size,
                        whole_file.hexdigest(),
                        tuple(chunk_digests),
                        None,
                    )
                )
        manifest = PersistenceManifest(
            MANIFEST_SCHEMA_VERSION,
            generation,
            sandbox_id,
            created_at,
            tuple(sorted(entries, key=lambda entry: entry.path)),
        )
        return BuiltManifest(manifest, chunks)

    def _walk(self) -> list[Path]:
        return _walk_included_roots(self._workspace)

    def _read_chunks(self, path: Path) -> list[bytes]:
        chunks: list[bytes] = []
        with path.open("rb") as source:
            while data := source.read(self._chunk_size):
                chunks.append(data)
        return chunks or [b""]
