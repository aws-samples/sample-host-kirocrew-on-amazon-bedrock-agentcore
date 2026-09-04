from __future__ import annotations

import http.client
import os
import platform
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kirocrew_agentcore_persistence.checkpoint import CheckpointEngine, SystemFlusher
from kirocrew_agentcore_persistence.durability import (
    BrokeredCheckpointStore,
    LocalKmsAdapter,
    LocalObjectStore,
    PersistenceBroker,
)
from kirocrew_agentcore_persistence.journal import DirtyJournal
from kirocrew_agentcore_persistence.manifest import ManifestBuilder
from kirocrew_agentcore_persistence.restore import RestoreEngine

from kirocrew_agentcore_runtime.supervisor import (
    KiroCrewSupervisor,
    RuntimeMetadata,
    WorkspaceLayout,
)

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_HEX_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SMOKE_SANDBOX_ID = "sbx_00000000000000000000000000"


@dataclass(frozen=True, slots=True)
class ImageMetadata:
    kirocrew_version: str
    kirocrew_artifact_sha256: str
    kiro_cli_version: str
    protocol_version: str
    source_revision: str
    base_image_digest: str
    dependency_lock_sha256: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] = os.environ) -> ImageMetadata:
        names = {
            "kirocrew_version": "KIROCREW_VERSION",
            "kirocrew_artifact_sha256": "KIROCREW_ARTIFACT_SHA256",
            "kiro_cli_version": "KIRO_CLI_VERSION",
            "protocol_version": "KIROCREW_AGENTCORE_PROTOCOL",
            "source_revision": "KIROCREW_SOURCE_REVISION",
            "base_image_digest": "KIROCREW_BASE_IMAGE_DIGEST",
            "dependency_lock_sha256": "KIROCREW_DEPENDENCY_LOCK_SHA256",
        }
        values: dict[str, str] = {}
        for field, name in names.items():
            value = environment.get(name)
            if not value:
                raise ValueError(f"Required runtime metadata is missing: {name}")
            values[field] = value
        metadata = cls(**values)
        if not _HEX_DIGEST.fullmatch(metadata.kirocrew_artifact_sha256):
            raise ValueError("KiroCrew artifact digest is invalid.")
        if not _DIGEST.fullmatch(metadata.base_image_digest):
            raise ValueError("Base image digest is invalid.")
        if not _HEX_DIGEST.fullmatch(metadata.dependency_lock_sha256):
            raise ValueError("Dependency lock digest is invalid.")
        if metadata.protocol_version != "kirocrew-agentcore.v1":
            raise ValueError("Runtime protocol version is unsupported.")
        return metadata

    def diagnostics(self) -> dict[str, object]:
        return {
            "architecture": platform.machine(),
            "baseImageDigest": self.base_image_digest,
            "dependencyLockSha256": self.dependency_lock_sha256,
            "kiroCliVersion": self.kiro_cli_version,
            "kiroCrewArtifactSha256": self.kirocrew_artifact_sha256,
            "kiroCrewVersion": self.kirocrew_version,
            "protocolVersion": self.protocol_version,
            "sourceRevision": self.source_revision,
        }

    def runtime_metadata(self) -> RuntimeMetadata:
        return RuntimeMetadata(
            self.kirocrew_version,
            self.kirocrew_artifact_sha256,
            self.protocol_version,
        )


def _restore_smoke_workspace(
    workspace: Path, metadata: ImageMetadata
) -> tuple[Path, BrokeredCheckpointStore, PersistenceBroker, str]:
    smoke_root = workspace / ".agentcore/image-smoke"
    source = smoke_root / "source"
    restored = smoke_root / "restored"
    shutil.rmtree(smoke_root, ignore_errors=True)
    marker = source / "projects/default/restore-marker.txt"
    marker.parent.mkdir(parents=True)
    marker.write_text("authoritative-checkpoint\n", encoding="utf-8")
    (source / "home").mkdir()

    sandbox_id = _SMOKE_SANDBOX_ID
    broker = PersistenceBroker(
        "image-smoke-checkpoints",
        LocalObjectStore(),
        LocalKmsAdapter(b"image-smoke-local-kms-key-32bytes"),
    )
    store = BrokeredCheckpointStore(broker, sandbox_id)
    cipher = broker.cipher(sandbox_id)
    built = ManifestBuilder(source).build(
        1,
        sandbox_id,
        datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    for digest, plaintext in built.chunks.items():
        store.upload_chunk(digest, cipher.encrypt(plaintext).ciphertext)
    manifest_blob = cipher.encrypt(built.manifest.to_json())
    store.upload_manifest(1, manifest_blob)
    store.commit_generation(1, manifest_blob.digest)

    report = RestoreEngine(
        restored,
        sandbox_id,
        metadata.kirocrew_version,
        store,
        cipher,
    ).restore(existing_sandbox=True)
    if marker.read_text(encoding="utf-8") != (
        restored / "projects/default/restore-marker.txt"
    ).read_text(encoding="utf-8"):
        raise RuntimeError("The authoritative checkpoint did not restore correctly.")
    return restored, store, broker, report.outcome


def _invoke_gateway(token: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", 5476, timeout=10)
    try:
        connection.request("GET", "/api/health", headers={"Authorization": f"Bearer {token}"})
        response = connection.getresponse()
        response.read()
        return response.status
    finally:
        connection.close()


def _gateway_stopped(supervisor: KiroCrewSupervisor) -> bool:
    return not supervisor.ready


def smoke_gateway(
    metadata: ImageMetadata,
    workspace: Path = Path("/mnt/workspace"),
    *,
    supervisor_type: type[KiroCrewSupervisor] = KiroCrewSupervisor,
) -> dict[str, object]:
    if os.geteuid() == 0:
        raise RuntimeError("Runtime image smoke must execute as non-root.")
    workspace.mkdir(parents=True, exist_ok=True)
    if not os.access(workspace, os.W_OK):
        raise RuntimeError("The managed workspace is not writable.")

    restored, store, broker, restore_outcome = _restore_smoke_workspace(workspace, metadata)
    supervisor = supervisor_type(WorkspaceLayout(restored), metadata.runtime_metadata())
    ready = supervisor.start(timeout_seconds=90)
    try:
        if not supervisor.ready or ready.port != 5476:
            raise RuntimeError("The loopback KiroCrew gateway did not become ready.")
        token = supervisor.token()
        if not token:
            raise RuntimeError("The internal KiroCrew token is unavailable.")
        invocation_status = _invoke_gateway(token)
        if invocation_status != 200:
            raise RuntimeError("The authenticated KiroCrew health invocation failed.")

        checkpoint_marker = restored / "projects/default/checkpoint-marker.txt"
        checkpoint_marker.write_text("final-checkpoint\n", encoding="utf-8")
        journal = DirtyJournal(restored)
        journal.mark("projects/default/checkpoint-marker.txt", datetime.now(UTC))
        receipt = CheckpointEngine(
            restored,
            _SMOKE_SANDBOX_ID,
            journal,
            ManifestBuilder(restored),
            broker.cipher(_SMOKE_SANDBOX_ID),
            store,
            supervisor,
            SystemFlusher(),
            runtime_version=metadata.kirocrew_version,
        ).checkpoint(2, final=True)
        supervisor.resume()
        result = {
            **metadata.diagnostics(),
            "checkpointGeneration": receipt.generation,
            "gatewayPort": ready.port,
            "gracefulStop": False,
            "healthy": True,
            "invocationStatus": invocation_status,
            "nonRoot": True,
            "restoreOutcome": restore_outcome,
        }
    finally:
        supervisor.terminate(grace_seconds=10)

    if not _gateway_stopped(supervisor):
        raise RuntimeError("The KiroCrew gateway did not stop gracefully.")
    result["gracefulStop"] = True
    return result
