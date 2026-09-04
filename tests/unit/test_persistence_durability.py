from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from kirocrew_agentcore_persistence.checkpoint import CheckpointError
from kirocrew_agentcore_persistence.crypto import IntegrityError
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    BrokeredCheckpointStore,
    InMemoryAuditSink,
    IntegrityAuditor,
    LocalKmsAdapter,
    LocalObjectStore,
    ObjectNotFoundError,
    PersistenceBroker,
    RetentionManager,
    StorageOperation,
    run_scheduled_audits,
)
from kirocrew_agentcore_persistence.restore import ManifestDecoder

SANDBOX_ID = "sbx_01J00000000000000000000000"
OTHER_SANDBOX_ID = "sbx_01J00000000000000000000001"


@dataclass
class MutableClock:
    now: datetime = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def components(
    clock: MutableClock | None = None,
) -> tuple[MutableClock, LocalObjectStore, PersistenceBroker, BrokeredCheckpointStore]:
    selected_clock = clock or MutableClock()
    objects = LocalObjectStore(clock=selected_clock)
    broker = PersistenceBroker(
        "snapshot-bucket",
        objects,
        LocalKmsAdapter(b"m" * 32),
        clock=selected_clock,
    )
    return (
        selected_clock,
        objects,
        broker,
        BrokeredCheckpointStore(broker, SANDBOX_ID, clock=selected_clock),
    )


def commit(
    store: BrokeredCheckpointStore,
    broker: PersistenceBroker,
    generation: int,
    manifest: dict[str, object],
    chunks: dict[str, bytes],
) -> str:
    cipher = broker.cipher(SANDBOX_ID)
    for digest, plaintext in chunks.items():
        store.upload_chunk(digest, cipher.encrypt(plaintext).ciphertext)
    blob = cipher.encrypt(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    store.upload_manifest(generation, blob)
    store.commit_generation(generation, blob.digest)
    return blob.digest


def manifest(generation: int, chunk_digest: str | None = None) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    if chunk_digest is not None:
        entries.append(
            {
                "chunks": [chunk_digest],
                "digest": chunk_digest,
                "mode": 0o600,
                "mtimeNs": 1,
                "path": "user/file.txt",
                "size": 4,
                "type": "file",
            }
        )
    return {
        "categories": [
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
        ],
        "createdAt": "2026-08-17T16:00:00Z",
        "entries": entries,
        "generation": generation,
        "sandboxId": SANDBOX_ID,
        "schemaVersion": 1,
    }


def test_broker_derives_scoped_keys_context_and_short_lived_grants() -> None:
    clock, objects, broker, _ = components()
    digest = "a" * 64
    grant = broker.presign_chunk(SANDBOX_ID, digest, StorageOperation.PUT)
    assert grant.method == StorageOperation.PUT
    assert grant.expires_at == clock.now + timedelta(minutes=5)
    assert grant.headers["x-amz-server-side-encryption"] == "aws:kms"
    broker.request(grant, StorageOperation.PUT, body=b"ciphertext")
    key = f"snapshots/{SANDBOX_ID}/chunks/{digest}.bin"
    assert objects.objects[key] == b"ciphertext"
    assert "sandboxId" not in grant.url
    assert broker.cipher(SANDBOX_ID).encrypt(b"same") != broker.cipher(OTHER_SANDBOX_ID).encrypt(
        b"same"
    )
    assert broker.encryption_context(SANDBOX_ID)["sandboxId"] == SANDBOX_ID


def test_local_store_enforces_method_expiry_headers_and_request_shapes() -> None:
    clock, objects, broker, _ = components()
    digest = "b" * 64
    put = broker.presign_chunk(SANDBOX_ID, digest, StorageOperation.PUT)
    with pytest.raises(BrokerAuthorizationError, match="not authorized"):
        objects.request(put, StorageOperation.GET, headers=put.headers)
    with pytest.raises(BrokerAuthorizationError, match="headers"):
        objects.request(put, StorageOperation.PUT, body=b"x", headers={})
    with pytest.raises(ValueError, match="body"):
        objects.request(put, StorageOperation.PUT, headers=put.headers)
    clock.advance(timedelta(minutes=5))
    with pytest.raises(BrokerAuthorizationError, match="expired"):
        objects.request(put, StorageOperation.PUT, body=b"x", headers=put.headers)
    missing = broker.presign_chunk(SANDBOX_ID, digest, StorageOperation.GET)
    with pytest.raises(ObjectNotFoundError, match="does not exist"):
        broker.request(missing, StorageOperation.GET)
    head = broker.presign_chunk(SANDBOX_ID, digest, StorageOperation.HEAD)
    assert broker.request(head, StorageOperation.HEAD) is False
    with pytest.raises(ValueError, match="does not accept"):
        objects.request(missing, StorageOperation.GET, body=b"x", headers=missing.headers)


def test_broker_rejects_arbitrary_identity_prefix_and_invalid_configuration() -> None:
    _, objects, broker, _ = components()
    kms = LocalKmsAdapter(b"k" * 32)
    with pytest.raises(ValueError, match="master key"):
        LocalKmsAdapter(b"short")
    with pytest.raises(ValueError, match="TTL"):
        PersistenceBroker("", objects, kms)
    with pytest.raises(ValueError, match="TTL"):
        PersistenceBroker("bucket", objects, kms, url_ttl=timedelta(minutes=16))
    with pytest.raises(BrokerAuthorizationError, match="sandbox"):
        broker.encryption_context("../another-sandbox")
    with pytest.raises(BrokerAuthorizationError, match="digest"):
        broker.presign_chunk(SANDBOX_ID, "bad", StorageOperation.GET)
    with pytest.raises(BrokerAuthorizationError, match="positive"):
        broker.presign_manifest(SANDBOX_ID, 0, StorageOperation.GET)
    with pytest.raises(BrokerAuthorizationError, match="category"):
        broker.internal_keys(SANDBOX_ID, "arbitrary-prefix")
    with pytest.raises(BrokerAuthorizationError, match="identifier"):
        broker.internal_delete(SANDBOX_ID, "chunks", "../bad.bin")
    with pytest.raises(BrokerAuthorizationError, match="identifier"):
        broker.internal_delete(SANDBOX_ID, "unknown", "1.json")


def test_brokered_store_requires_valid_manifest_and_monotonic_commits() -> None:
    _, objects, broker, store = components()
    assert store.latest_committed() is None
    digest = "c" * 64
    store.upload_chunk(digest, b"first")
    store.upload_chunk(digest, b"second")
    assert store.get_chunk(digest) == b"first"
    blob = broker.cipher(SANDBOX_ID).encrypt(b"{}")
    store.upload_manifest(1, blob)
    with pytest.raises(CheckpointError, match="Manifest"):
        store.commit_generation(1, "d" * 64)
    store.commit_generation(1, blob.digest)
    assert store.latest_committed() == 1
    with pytest.raises(CheckpointError, match="monotonically"):
        store.commit_generation(1, blob.digest)
    assert store.generation_object_names(1) == ("1.json.enc", "1.json")
    commit_key = f"snapshots/{SANDBOX_ID}/commits/1.json"
    objects.objects[commit_key] = b"[]"
    with pytest.raises(IntegrityError, match="Commit"):
        store.committed_generations()


def test_brokered_store_rejects_malformed_commit_and_manifest_objects() -> None:
    _, objects, broker, store = components()
    commit_key = f"snapshots/{SANDBOX_ID}/commits/1.json"
    manifest_key = f"snapshots/{SANDBOX_ID}/manifests/1.json.enc"
    invalid_commits = [
        {},
        {"generation": 2, "manifestDigest": "a" * 64, "committedAt": "2026-01-01Z"},
        {"generation": 1, "manifestDigest": "bad", "committedAt": "2026-01-01Z"},
        {"generation": 1, "manifestDigest": "a" * 64, "committedAt": 1},
    ]
    for value in invalid_commits:
        objects.objects[commit_key] = json.dumps(value).encode()
        with pytest.raises(IntegrityError, match="Commit"):
            store.committed_generations()
    objects.objects[commit_key] = json.dumps(
        {"generation": 1, "manifestDigest": "a" * 64, "committedAt": "not-a-time"}
    ).encode()
    with pytest.raises(IntegrityError, match="Commit"):
        store.committed_generations()
    objects.objects.pop(commit_key)
    for raw_manifest in [b"[]", b"{}", b'{"digest":1,"ciphertext":1}', b"not-json"]:
        objects.objects[manifest_key] = raw_manifest
        with pytest.raises(IntegrityError, match="manifest"):
            store.get_manifest(1)
    objects.objects[manifest_key] = b'{"digest":"a","ciphertext":"%%%"}'
    with pytest.raises(IntegrityError, match="manifest"):
        store.get_manifest(1)
    objects.objects.pop(manifest_key)
    with pytest.raises(ObjectNotFoundError):
        store.get_manifest(1)


def test_retention_marks_then_sweeps_only_still_unreferenced_objects() -> None:
    clock, _, broker, store = components()
    decoder = ManifestDecoder()
    chunks: list[str] = []
    for generation, content in enumerate((b"old1", b"old2", b"new3"), start=1):
        digest = __import__("hashlib").sha256(content).hexdigest()
        chunks.append(digest)
        commit(store, broker, generation, manifest(generation, digest), {digest: content})
    manager = RetentionManager(
        store,
        broker.cipher(SANDBOX_ID),
        decoder.chunk_references,
        clock=clock,
        grace_period=timedelta(hours=1),
    )
    plan = manager.mark()
    assert plan.retained_generations == (2, 3)
    assert plan.generation_candidates == frozenset({1})
    assert plan.chunk_candidates == frozenset({chunks[0]})
    manager.sweep()
    assert store.committed_generations()[0].generation == 1
    clock.advance(timedelta(hours=1))
    manager.sweep()
    assert tuple(item.generation for item in store.committed_generations()) == (2, 3)
    assert chunks[0] not in store.chunk_digests()
    assert set(chunks[1:]) <= store.chunk_digests()


def test_retention_recomputes_references_before_sweep_and_validates_policy() -> None:
    clock, _, broker, store = components()
    decoder = ManifestDecoder()
    orphan = "f" * 64
    store.upload_chunk(orphan, b"orphan")
    manager = RetentionManager(
        store,
        broker.cipher(SANDBOX_ID),
        decoder.chunk_references,
        clock=clock,
        grace_period=timedelta(seconds=1),
    )
    manager.mark()
    content = b"referenced later"
    digest = __import__("hashlib").sha256(content).hexdigest()
    assert digest != orphan
    commit(store, broker, 1, manifest(1, orphan), {orphan: b"orphan"})
    clock.advance(timedelta(seconds=1))
    plan = manager.sweep()
    assert not plan.chunk_candidates
    assert orphan in store.chunk_digests()
    with pytest.raises(ValueError, match="two generations"):
        RetentionManager(
            store, broker.cipher(SANDBOX_ID), decoder.chunk_references, retain_generations=1
        )
    with pytest.raises(ValueError, match="positive"):
        RetentionManager(
            store,
            broker.cipher(SANDBOX_ID),
            decoder.chunk_references,
            grace_period=timedelta(0),
        )


def test_offline_integrity_audit_records_success_and_failure_metrics() -> None:
    clock, objects, broker, store = components()
    decoder = ManifestDecoder()
    content = b"data"
    digest = __import__("hashlib").sha256(content).hexdigest()
    commit(store, broker, 1, manifest(1, digest), {digest: content})
    sink = InMemoryAuditSink()
    auditor = IntegrityAuditor(
        SANDBOX_ID,
        store,
        broker.cipher(SANDBOX_ID),
        decoder.chunk_references,
        sink,
        clock=clock,
    )
    successful = run_scheduled_audits([auditor])[0]
    assert successful.ok
    assert successful.checked_generations == (1,)
    assert sink.failure_metrics == 0
    objects.internal_delete(f"snapshots/{SANDBOX_ID}/chunks/{digest}.bin")
    failed = auditor.run()
    assert not failed.ok
    assert failed.failures == ("generation 1: missing referenced chunk",)
    assert sink.failure_metrics == 1
    objects.objects[f"snapshots/{SANDBOX_ID}/manifests/1.json.enc"] = b"corrupt"
    failed_manifest = auditor.run()
    assert failed_manifest.failures == ("generation 1: manifest integrity failure",)
    valid_envelope = broker.cipher(SANDBOX_ID).encrypt(b"{}")
    store.upload_manifest(1, valid_envelope)
    commit_key = f"snapshots/{SANDBOX_ID}/commits/1.json"
    commit_value = json.loads(objects.objects[commit_key])
    commit_value["manifestDigest"] = valid_envelope.digest
    objects.objects[commit_key] = json.dumps(commit_value).encode()
    malformed_plaintext = auditor.run()
    assert malformed_plaintext.failures == ("generation 1: manifest integrity failure",)
    assert len(sink.results) == 4
