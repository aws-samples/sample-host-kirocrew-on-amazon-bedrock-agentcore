from __future__ import annotations

import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from kirocrew_agentcore_control.sandbox import (
    IdempotencyConflictError,
    InMemorySandboxRegistry,
    LeaseConflictError,
    OwnerHasher,
    RequestDisposition,
    SandboxState,
    SandboxUnavailableError,
    StateConflictError,
    new_runtime_session_id,
    new_sandbox_id,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


def registry(clock: MutableClock | None = None) -> InMemorySandboxRegistry:
    return InMemorySandboxRegistry(
        sandbox_id_factory=lambda: "sbx_01J00000000000000000000000",
        runtime_session_id_factory=lambda: "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
        clock=clock or MutableClock(),
        kirocrew_version="1.2.3",
    )


def test_acquire_start_leaves_record_alone_while_init_lease_is_live() -> None:
    clock = MutableClock()
    store = registry(clock)
    record = store.get_or_create("subject")
    lease = store.acquire_start("subject", "browser-1", ttl=timedelta(seconds=90))
    assert lease.authoritative
    # The runtime container claims initialization ownership.
    from dataclasses import replace

    initializing = replace(
        lease.record,
        init_expires_at=clock() + timedelta(seconds=60),
        lease_expires_at=clock() - timedelta(seconds=1),
    )
    store._by_owner[record.owner_hash] = initializing
    held = store.acquire_start("subject", "browser-2", ttl=timedelta(seconds=90))
    assert held.record is initializing
    assert held.record.state_version == initializing.state_version


def test_owner_hash_is_stable_and_opaque() -> None:
    hasher = OwnerHasher()
    first = hasher.derive("cognito-subject")
    assert first == hasher.derive("cognito-subject")
    assert first != hasher.derive("other-subject")
    assert "cognito" not in first
    with pytest.raises(ValueError, match="must not be empty"):
        hasher.derive("")


def test_default_identifier_factories_match_external_contracts() -> None:
    assert re.fullmatch(r"sbx_[0-9A-HJKMNP-TV-Z]{26}", new_sandbox_id())
    generated_session_id = new_runtime_session_id()
    assert str(uuid.UUID(generated_session_id)) == generated_session_id


def test_get_or_create_is_stable_and_missing_get_is_non_enumerating() -> None:
    store = registry()
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        store.get("alice")
    first = store.get_or_create("alice")
    assert store.get_or_create("alice") is first
    assert first.state is SandboxState.STOPPED
    assert first.kirocrew_version == "1.2.3"
    assert first.owner_hash != "alice"


def test_authorization_checks_both_stable_identifiers() -> None:
    store = registry()
    record = store.get_or_create("alice")
    assert store.authorize("alice", record.sandbox_id) is record
    assert store.authorize("alice", record.sandbox_id, record.runtime_session_id) is record
    for sandbox_id, session_id in [
        ("sbx_01J00000000000000000000001", None),
        (record.sandbox_id, "00000000-0000-0000-0000-000000000000"),
    ]:
        with pytest.raises(SandboxUnavailableError, match="unavailable"):
            store.authorize("alice", sandbox_id, session_id)


def test_start_lease_serializes_owners_and_supports_heartbeat() -> None:
    clock = MutableClock()
    store = registry(clock)
    with pytest.raises(ValueError, match="positive TTL"):
        store.acquire_start("alice", "", ttl=timedelta(seconds=90))
    first = store.acquire_start("alice", "runtime-a", ttl=timedelta(seconds=90))
    assert first.authoritative
    assert first.record.state is SandboxState.STARTING
    # The lease owner is the rotated session id: the container that owns
    # this start authenticates every later call with it.
    owner = first.record.lease_owner
    assert owner == first.record.runtime_session_id
    assert store.acquire_start("alice", owner, ttl=timedelta(seconds=90)).authoritative
    assert not store.acquire_start("alice", "runtime-b", ttl=timedelta(seconds=90)).authoritative
    with pytest.raises(ValueError, match="positive"):
        store.heartbeat("alice", owner, ttl=timedelta(0))
    heartbeat = store.heartbeat("alice", owner, ttl=timedelta(seconds=90))
    assert heartbeat.state_version == first.record.state_version + 1
    with pytest.raises(LeaseConflictError, match="not owned"):
        store.heartbeat("alice", "runtime-b", ttl=timedelta(seconds=90))
    clock.advance(timedelta(seconds=91))
    with pytest.raises(LeaseConflictError, match="not owned"):
        store.heartbeat("alice", owner, ttl=timedelta(seconds=90))


def test_expired_active_lease_requires_confirmation_before_reclaim() -> None:
    clock = MutableClock()
    store = registry(clock)
    store.acquire_start("alice", "runtime-a", ttl=timedelta(seconds=10))
    clock.advance(timedelta(seconds=11))
    with pytest.raises(LeaseConflictError, match="confirmation"):
        store.acquire_start("alice", "runtime-b", ttl=timedelta(seconds=10))
    reclaimed = store.acquire_start(
        "alice",
        "runtime-b",
        ttl=timedelta(seconds=10),
        confirmed_inactive=True,
    )
    assert reclaimed.authoritative
    # The reclaim rotates the session; the new session owns the lease.
    assert reclaimed.record.lease_owner == reclaimed.record.runtime_session_id


def test_authoritative_owner_reclaims_its_own_expired_lease_without_confirmation() -> None:
    clock = MutableClock()
    store = registry(clock)
    held = store.acquire_start("alice", "runtime-a", ttl=timedelta(seconds=10))
    clock.advance(timedelta(seconds=11))
    reclaimed = store.acquire_start(
        "alice", held.record.lease_owner or "", ttl=timedelta(seconds=10)
    )
    assert reclaimed.authoritative
    assert reclaimed.record.lease_owner == reclaimed.record.runtime_session_id
    assert reclaimed.record.state is SandboxState.STARTING


def test_lifecycle_version_owner_and_transition_guards() -> None:
    store = registry()
    start = store.acquire_start("alice", "runtime", ttl=timedelta(seconds=90)).record
    runtime = start.lease_owner or ""
    with pytest.raises(StateConflictError, match="version"):
        store.transition("alice", SandboxState.RESTORING, expected_version=0, lease_owner=runtime)
    with pytest.raises(LeaseConflictError, match="not owned"):
        store.transition(
            "alice",
            SandboxState.RESTORING,
            expected_version=start.state_version,
            lease_owner="other",
        )
    with pytest.raises(StateConflictError, match="not allowed"):
        store.transition(
            "alice",
            SandboxState.BUSY,
            expected_version=start.state_version,
            lease_owner=runtime,
        )


def test_complete_lifecycle_request_checkpoint_and_stop() -> None:
    store = registry()
    record = store.acquire_start("alice", "runtime", ttl=timedelta(seconds=90)).record
    runtime = record.lease_owner or ""
    record = store.transition(
        "alice",
        SandboxState.RESTORING,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    record = store.transition(
        "alice",
        SandboxState.READY,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    with pytest.raises(ValueError, match="required"):
        store.accept_request("alice", runtime, "", "digest")
    assert store.accept_request("alice", runtime, "request", "digest") is RequestDisposition.NEW
    assert (
        store.accept_request("alice", runtime, "request", "digest") is RequestDisposition.DUPLICATE
    )
    with pytest.raises(IdempotencyConflictError, match="another payload"):
        store.accept_request("alice", runtime, "request", "changed")
    with pytest.raises(LeaseConflictError, match="not owned"):
        store.accept_request("alice", "other", "other", "digest")
    record = store.get("alice")
    record = store.transition(
        "alice",
        SandboxState.BUSY,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    assert record.active_request_id == "request"
    record = store.transition(
        "alice",
        SandboxState.CHECKPOINTING,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    assert record.active_request_id is None
    with pytest.raises(ValueError, match="positive"):
        store.record_checkpoint("alice", runtime, 0)
    with pytest.raises(LeaseConflictError, match="not owned"):
        store.record_checkpoint("alice", "other", 1)
    record = store.record_checkpoint("alice", runtime, 7, "RESTORED")
    assert record.last_checkpoint_generation == 7
    assert record.last_restore == "RESTORED"
    record = store.transition(
        "alice",
        SandboxState.STOPPING,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    record = store.transition(
        "alice",
        SandboxState.STOPPED,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    assert record.lease_owner is None
    assert record.lease_expires_at is None


def test_error_state_can_restart_or_stop() -> None:
    store = registry()
    record = store.acquire_start("alice", "runtime", ttl=timedelta(seconds=90)).record
    runtime = record.lease_owner or ""
    record = store.transition(
        "alice", SandboxState.ERROR, expected_version=record.state_version, lease_owner=runtime
    )
    restarted = store.transition(
        "alice",
        SandboxState.STARTING,
        expected_version=record.state_version,
        lease_owner=runtime,
    )
    assert restarted.state is SandboxState.STARTING

    second = registry()
    failed = second.acquire_start("alice", "runtime", ttl=timedelta(seconds=90)).record
    second_runtime = failed.lease_owner or ""
    failed = second.transition(
        "alice",
        SandboxState.ERROR,
        expected_version=failed.state_version,
        lease_owner=second_runtime,
    )
    stopped = second.transition(
        "alice",
        SandboxState.STOPPED,
        expected_version=failed.state_version,
        lease_owner=second_runtime,
    )
    assert stopped.state is SandboxState.STOPPED


def test_parallel_starts_create_one_sandbox_and_one_authority() -> None:
    store = registry()

    def start(index: int) -> tuple[str, bool]:
        result = store.acquire_start("alice", f"runtime-{index}", ttl=timedelta(seconds=90))
        return result.record.sandbox_id, result.authoritative

    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(start, range(100)))
    assert len({sandbox_id for sandbox_id, _ in results}) == 1
    assert sum(authoritative for _, authoritative in results) == 1


def test_two_subjects_have_independent_process_and_storage_identity() -> None:
    sequence = iter(
        [
            "sbx_01J00000000000000000000001",
            "sbx_01J00000000000000000000002",
        ]
    )
    sessions = iter(
        [
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ]
    )
    store = InMemorySandboxRegistry(
        sandbox_id_factory=lambda: next(sequence),
        runtime_session_id_factory=lambda: next(sessions),
    )
    alice = store.get_or_create("alice")
    bob = store.get_or_create("bob")
    assert alice.owner_hash != bob.owner_hash
    assert alice.sandbox_id != bob.sandbox_id
    assert alice.runtime_session_id != bob.runtime_session_id
    with pytest.raises(SandboxUnavailableError):
        store.authorize("bob", alice.sandbox_id)
