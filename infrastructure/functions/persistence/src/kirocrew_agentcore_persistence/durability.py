from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol, cast

from kirocrew_agentcore_persistence.checkpoint import CheckpointError
from kirocrew_agentcore_persistence.crypto import EncryptedBlob, IntegrityError, SandboxCipher

_SANDBOX_PATTERN: Final = re.compile(r"sbx_[0-9A-Z]{26}\Z")
_DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_DEFAULT_URL_TTL: Final = timedelta(minutes=5)
_DEFAULT_GC_GRACE: Final = timedelta(hours=24)


class StorageOperation(StrEnum):
    GET = "GET"
    PUT = "PUT"
    HEAD = "HEAD"


class BrokerAuthorizationError(ValueError):
    """The persistence broker did not complete the operation."""


class BrokerRefusalError(BrokerAuthorizationError):
    """The broker refused the caller's token or sandbox binding outright.

    Distinct from other broker failures (throttling, malformed input, an
    unexpected exception) because a refusal is authoritative: the token or the
    session it names is no longer accepted, so retrying cannot help.
    """


class ObjectNotFoundError(KeyError):
    """A required durable object does not exist."""


@dataclass(frozen=True, slots=True)
class PresignedOperation:
    url: str
    method: StorageOperation
    expires_at: datetime
    headers: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _PresignedGrant:
    key: str
    operation: StorageOperation
    expires_at: datetime
    headers: Mapping[str, str]


class LocalObjectStore:
    """Credential-free S3 adapter that enforces broker-issued operation grants."""

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.objects: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self._grants: dict[str, _PresignedGrant] = {}
        self._clock = clock

    def presign(
        self,
        key: str,
        operation: StorageOperation,
        expires_at: datetime,
        headers: Mapping[str, str],
    ) -> PresignedOperation:
        token = secrets.token_urlsafe(24)
        self._grants[token] = _PresignedGrant(key, operation, expires_at, dict(headers))
        return PresignedOperation(
            f"local-s3-presigned://{token}", operation, expires_at, dict(headers)
        )

    def request(
        self,
        grant: PresignedOperation,
        operation: StorageOperation,
        *,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> bytes | bool:
        token = grant.url.removeprefix("local-s3-presigned://")
        stored = self._grants.get(token)
        supplied_headers = dict(headers or {})
        if stored is None or stored.operation != operation:
            raise BrokerAuthorizationError("The storage operation is not authorized.")
        if self._clock() >= stored.expires_at:
            raise BrokerAuthorizationError("The storage operation grant expired.")
        if supplied_headers != dict(stored.headers):
            raise BrokerAuthorizationError("Required storage headers do not match the grant.")
        if operation == StorageOperation.PUT:
            if body is None:
                raise ValueError("PUT requires a body.")
            self.objects[stored.key] = body
            self.metadata[stored.key] = supplied_headers
            return b""
        if operation == StorageOperation.HEAD:
            return stored.key in self.objects
        if body is not None:
            raise ValueError("GET does not accept a body.")
        try:
            return self.objects[stored.key]
        except KeyError as error:
            raise ObjectNotFoundError("Durable object does not exist.") from error

    def internal_list(self, prefix: str) -> tuple[str, ...]:
        return tuple(sorted(key for key in self.objects if key.startswith(prefix)))

    def internal_delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.metadata.pop(key, None)


class LocalKmsAdapter:
    """Deterministic local KMS adapter that binds data keys to encryption context."""

    def __init__(self, master_key: bytes) -> None:
        if len(master_key) < 32:
            raise ValueError("Local KMS master key must contain at least 32 bytes.")
        self._master_key = master_key

    def data_key(self, context: Mapping[str, str]) -> bytes:
        encoded = json.dumps(dict(sorted(context.items())), separators=(",", ":")).encode()
        return hmac.new(self._master_key, b"data-key\x00" + encoded, hashlib.sha256).digest()


class PersistenceBroker:
    """Derives every object key and KMS context from a validated sandbox ID."""

    def __init__(
        self,
        bucket: str,
        objects: LocalObjectStore,
        kms: LocalKmsAdapter,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        url_ttl: timedelta = _DEFAULT_URL_TTL,
    ) -> None:
        if not bucket or url_ttl <= timedelta(0) or url_ttl > timedelta(minutes=15):
            raise ValueError("Bucket and a URL TTL of at most 15 minutes are required.")
        self._bucket = bucket
        self._objects = objects
        self._kms = kms
        self._clock = clock
        self._url_ttl = url_ttl

    def cipher(self, sandbox_id: str) -> SandboxCipher:
        sandbox = self._validate_sandbox(sandbox_id)
        return SandboxCipher(self._kms.data_key(self.encryption_context(sandbox)), sandbox)

    def encryption_context(self, sandbox_id: str) -> dict[str, str]:
        sandbox = self._validate_sandbox(sandbox_id)
        return {
            "application": "kirocrew-agentcore",
            "purpose": "sandbox-checkpoint",
            "sandboxId": sandbox,
        }

    def presign_chunk(
        self, sandbox_id: str, digest: str, operation: StorageOperation
    ) -> PresignedOperation:
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise BrokerAuthorizationError("Invalid content digest.")
        return self._presign(sandbox_id, f"chunks/{digest}.bin", operation)

    def presign_manifest(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation:
        return self._presign(
            sandbox_id, f"manifests/{self._validate_generation(generation)}.json.enc", operation
        )

    def presign_commit(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation:
        return self._presign(
            sandbox_id, f"commits/{self._validate_generation(generation)}.json", operation
        )

    def request(
        self,
        grant: PresignedOperation,
        operation: StorageOperation,
        *,
        body: bytes | None = None,
    ) -> bytes | bool:
        return self._objects.request(grant, operation, body=body, headers=grant.headers)

    def internal_keys(self, sandbox_id: str, category: str) -> tuple[str, ...]:
        sandbox = self._validate_sandbox(sandbox_id)
        if category not in {"chunks", "manifests", "commits"}:
            raise BrokerAuthorizationError("Invalid storage category.")
        return self._objects.internal_list(f"snapshots/{sandbox}/{category}/")

    def internal_delete(self, sandbox_id: str, category: str, name: str) -> None:
        sandbox = self._validate_sandbox(sandbox_id)
        valid_names = {
            "chunks": _DIGEST_PATTERN.fullmatch(name.removesuffix(".bin")) is not None
            and name.endswith(".bin"),
            "manifests": name.endswith(".json.enc") and name.removesuffix(".json.enc").isdigit(),
            "commits": name.endswith(".json") and name.removesuffix(".json").isdigit(),
        }
        if category not in valid_names or not valid_names[category]:
            raise BrokerAuthorizationError("Invalid internal object identifier.")
        self._objects.internal_delete(f"snapshots/{sandbox}/{category}/{name}")

    def _presign(
        self, sandbox_id: str, suffix: str, operation: StorageOperation
    ) -> PresignedOperation:
        sandbox = self._validate_sandbox(sandbox_id)
        context = base64.b64encode(
            json.dumps(self.encryption_context(sandbox), sort_keys=True).encode()
        ).decode()
        headers = {
            "x-amz-server-side-encryption": "aws:kms",
            "x-amz-server-side-encryption-context": context,
            "x-amz-server-side-encryption-bucket": self._bucket,
        }
        return self._objects.presign(
            f"snapshots/{sandbox}/{suffix}",
            operation,
            self._clock() + self._url_ttl,
            headers,
        )

    @staticmethod
    def _validate_sandbox(sandbox_id: str) -> str:
        if not _SANDBOX_PATTERN.fullmatch(sandbox_id):
            raise BrokerAuthorizationError("Invalid sandbox identity.")
        return sandbox_id

    @staticmethod
    def _validate_generation(generation: int) -> int:
        if generation <= 0:
            raise BrokerAuthorizationError("Generation must be positive.")
        return generation


@dataclass(frozen=True, slots=True)
class CommittedGeneration:
    generation: int
    manifest_digest: str
    committed_at: datetime


class CheckpointBroker(Protocol):
    def encryption_context(self, sandbox_id: str) -> dict[str, str]: ...

    def presign_chunk(
        self, sandbox_id: str, digest: str, operation: StorageOperation
    ) -> PresignedOperation: ...

    def presign_manifest(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation: ...

    def presign_commit(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation: ...

    def request(
        self,
        grant: PresignedOperation,
        operation: StorageOperation,
        *,
        body: bytes | None = None,
    ) -> bytes | bool: ...

    def internal_keys(self, sandbox_id: str, category: str) -> tuple[str, ...]: ...

    def internal_delete(self, sandbox_id: str, category: str, name: str) -> None: ...


class BrokeredCheckpointStore:
    """Checkpoint store and restore source backed only by broker-derived operations."""

    def __init__(
        self,
        broker: CheckpointBroker,
        sandbox_id: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._broker = broker
        self._sandbox_id = sandbox_id
        self._clock = clock
        broker.encryption_context(sandbox_id)

    def has_chunk(self, digest: str) -> bool:
        grant = self._broker.presign_chunk(self._sandbox_id, digest, StorageOperation.HEAD)
        return cast(bool, self._broker.request(grant, StorageOperation.HEAD))

    def upload_chunk(self, digest: str, ciphertext: bytes) -> None:
        if self.has_chunk(digest):
            return
        grant = self._broker.presign_chunk(self._sandbox_id, digest, StorageOperation.PUT)
        self._broker.request(grant, StorageOperation.PUT, body=ciphertext)

    def upload_manifest(self, generation: int, blob: EncryptedBlob) -> None:
        payload = json.dumps(
            {
                "ciphertext": base64.b64encode(blob.ciphertext).decode(),
                "digest": blob.digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        grant = self._broker.presign_manifest(self._sandbox_id, generation, StorageOperation.PUT)
        self._broker.request(grant, StorageOperation.PUT, body=payload)

    def commit_generation(self, generation: int, manifest_digest: str) -> None:
        manifest = self.get_manifest(generation)
        if manifest.digest != manifest_digest:
            raise CheckpointError("Manifest must exist before generation commit.")
        committed = self.committed_generations()
        if committed and generation <= committed[-1].generation:
            raise CheckpointError("Generation commit must advance monotonically.")
        payload = json.dumps(
            {
                "committedAt": self._clock().isoformat().replace("+00:00", "Z"),
                "generation": generation,
                "manifestDigest": manifest_digest,
                "schemaVersion": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        grant = self._broker.presign_commit(self._sandbox_id, generation, StorageOperation.PUT)
        self._broker.request(grant, StorageOperation.PUT, body=payload)

    def latest_committed(self) -> int | None:
        generations = self.committed_generations()
        return generations[-1].generation if generations else None

    def committed_generations(self) -> tuple[CommittedGeneration, ...]:
        commits: list[CommittedGeneration] = []
        for key in self._broker.internal_keys(self._sandbox_id, "commits"):
            generation = int(key.rsplit("/", 1)[-1].removesuffix(".json"))
            grant = self._broker.presign_commit(self._sandbox_id, generation, StorageOperation.GET)
            raw = cast(bytes, self._broker.request(grant, StorageOperation.GET))
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise IntegrityError("Commit record is malformed.")
            digest = value.get("manifestDigest")
            timestamp = value.get("committedAt")
            stored_generation = value.get("generation")
            if (
                not isinstance(digest, str)
                or not _DIGEST_PATTERN.fullmatch(digest)
                or not isinstance(timestamp, str)
                or stored_generation != generation
            ):
                raise IntegrityError("Commit record is malformed.")
            try:
                committed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as error:
                raise IntegrityError("Commit record is malformed.") from error
            commits.append(
                CommittedGeneration(
                    generation,
                    digest,
                    committed_at,
                )
            )
        return tuple(sorted(commits, key=lambda item: item.generation))

    def get_manifest(self, generation: int) -> EncryptedBlob:
        grant = self._broker.presign_manifest(self._sandbox_id, generation, StorageOperation.GET)
        raw = cast(bytes, self._broker.request(grant, StorageOperation.GET))
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            digest = value["digest"]
            ciphertext = value["ciphertext"]
            if not isinstance(digest, str) or not isinstance(ciphertext, str):
                raise ValueError
            return EncryptedBlob(digest, base64.b64decode(ciphertext, validate=True))
        except (KeyError, ValueError, TypeError) as error:
            raise IntegrityError("Encrypted manifest object is malformed.") from error

    def get_chunk(self, digest: str) -> bytes:
        grant = self._broker.presign_chunk(self._sandbox_id, digest, StorageOperation.GET)
        return cast(bytes, self._broker.request(grant, StorageOperation.GET))

    def generation_object_names(self, generation: int) -> tuple[str, str]:
        return f"{generation}.json.enc", f"{generation}.json"

    def remove_generation(self, generation: int) -> None:
        manifest, commit = self.generation_object_names(generation)
        self._broker.internal_delete(self._sandbox_id, "manifests", manifest)
        self._broker.internal_delete(self._sandbox_id, "commits", commit)

    def remove_chunk(self, digest: str) -> None:
        self._broker.internal_delete(self._sandbox_id, "chunks", f"{digest}.bin")

    def chunk_digests(self) -> frozenset[str]:
        return frozenset(
            key.rsplit("/", 1)[-1].removesuffix(".bin")
            for key in self._broker.internal_keys(self._sandbox_id, "chunks")
        )


@dataclass(frozen=True, slots=True)
class GarbageCollectionPlan:
    retained_generations: tuple[int, ...]
    generation_candidates: frozenset[int]
    chunk_candidates: frozenset[str]


class RetentionManager:
    def __init__(
        self,
        store: BrokeredCheckpointStore,
        cipher: SandboxCipher,
        manifest_chunks: Callable[[bytes], frozenset[str]],
        *,
        retain_generations: int = 2,
        grace_period: timedelta = _DEFAULT_GC_GRACE,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if retain_generations < 2 or grace_period <= timedelta(0):
            raise ValueError("At least two generations and a positive grace period are required.")
        self._store = store
        self._cipher = cipher
        self._manifest_chunks = manifest_chunks
        self._retain = retain_generations
        self._grace = grace_period
        self._clock = clock
        self._marks: dict[tuple[str, str], datetime] = {}

    def mark(self) -> GarbageCollectionPlan:
        committed = self._store.committed_generations()
        retained = committed[-self._retain :]
        retained_numbers = tuple(item.generation for item in retained)
        referenced: set[str] = set()
        for item in retained:
            plaintext = self._cipher.decrypt(self._store.get_manifest(item.generation))
            referenced.update(self._manifest_chunks(plaintext))
        generation_candidates = frozenset(
            item.generation for item in committed if item.generation not in retained_numbers
        )
        chunk_candidates = self._store.chunk_digests() - referenced
        current = {
            *(("generation", str(value)) for value in generation_candidates),
            *(("chunk", value) for value in chunk_candidates),
        }
        now = self._clock()
        self._marks = {key: self._marks.get(key, now) for key in current}
        return GarbageCollectionPlan(
            retained_numbers, generation_candidates, frozenset(chunk_candidates)
        )

    def sweep(self) -> GarbageCollectionPlan:
        plan = self.mark()
        now = self._clock()
        for (kind, value), marked_at in tuple(self._marks.items()):
            if now - marked_at < self._grace:
                continue
            if kind == "generation":
                self._store.remove_generation(int(value))
            else:
                self._store.remove_chunk(value)
            del self._marks[(kind, value)]
        return plan


@dataclass(frozen=True, slots=True)
class IntegrityAuditResult:
    sandbox_id: str
    checked_generations: tuple[int, ...]
    ok: bool
    failures: tuple[str, ...]
    checked_at: datetime


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.results: list[IntegrityAuditResult] = []
        self.failure_metrics = 0

    def record(self, result: IntegrityAuditResult) -> None:
        self.results.append(result)
        if not result.ok:
            self.failure_metrics += 1


class IntegrityAuditor:
    """Checks retained manifests/chunk references without launching user compute."""

    def __init__(
        self,
        sandbox_id: str,
        store: BrokeredCheckpointStore,
        cipher: SandboxCipher,
        manifest_chunks: Callable[[bytes], frozenset[str]],
        sink: InMemoryAuditSink,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sandbox_id = sandbox_id
        self._store = store
        self._cipher = cipher
        self._manifest_chunks = manifest_chunks
        self._sink = sink
        self._clock = clock

    def run(self) -> IntegrityAuditResult:
        generations = self._store.committed_generations()[-2:]
        failures: list[str] = []
        for item in generations:
            try:
                plaintext = self._cipher.decrypt(self._store.get_manifest(item.generation))
                for digest in self._manifest_chunks(plaintext):
                    if not self._store.has_chunk(digest):
                        failures.append(f"generation {item.generation}: missing referenced chunk")
            except (IntegrityError, ObjectNotFoundError, RuntimeError):
                failures.append(f"generation {item.generation}: manifest integrity failure")
        result = IntegrityAuditResult(
            self._sandbox_id,
            tuple(item.generation for item in generations),
            not failures,
            tuple(failures),
            self._clock(),
        )
        self._sink.record(result)
        return result


def run_scheduled_audits(auditors: Iterable[IntegrityAuditor]) -> tuple[IntegrityAuditResult, ...]:
    return tuple(auditor.run() for auditor in auditors)
