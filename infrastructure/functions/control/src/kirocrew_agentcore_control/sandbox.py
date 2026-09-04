from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

_CROCKFORD: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class SandboxState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RESTORING = "RESTORING"
    READY = "READY"
    BUSY = "BUSY"
    CHECKPOINTING = "CHECKPOINTING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


_ALLOWED_TRANSITIONS: Final[Mapping[SandboxState, frozenset[SandboxState]]] = {
    SandboxState.STOPPED: frozenset({SandboxState.STARTING}),
    SandboxState.STARTING: frozenset(
        {SandboxState.RESTORING, SandboxState.STOPPING, SandboxState.ERROR}
    ),
    SandboxState.RESTORING: frozenset({SandboxState.READY, SandboxState.ERROR}),
    SandboxState.READY: frozenset(
        {
            SandboxState.BUSY,
            SandboxState.CHECKPOINTING,
            SandboxState.STOPPING,
            SandboxState.ERROR,
        }
    ),
    SandboxState.BUSY: frozenset(
        {SandboxState.READY, SandboxState.CHECKPOINTING, SandboxState.ERROR}
    ),
    SandboxState.CHECKPOINTING: frozenset(
        {SandboxState.READY, SandboxState.STOPPING, SandboxState.ERROR}
    ),
    SandboxState.STOPPING: frozenset({SandboxState.STOPPED, SandboxState.ERROR}),
    SandboxState.ERROR: frozenset({SandboxState.STARTING, SandboxState.STOPPED}),
}


class SandboxRegistryError(RuntimeError):
    """Base error for stable sandbox registry failures."""


class SandboxUnavailableError(SandboxRegistryError):
    """Non-enumerating response for missing or unauthorized sandboxes."""


class LeaseConflictError(SandboxRegistryError):
    """A different authoritative runtime owns the sandbox lease."""


class StateConflictError(SandboxRegistryError):
    """The requested lifecycle transition conflicts with current state."""


class IdempotencyConflictError(SandboxRegistryError):
    """A request identifier was reused for a different operation."""


class RequestDisposition(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"


@dataclass(frozen=True, slots=True)
class SandboxRecord:
    owner_hash: str
    sandbox_id: str
    runtime_session_id: str
    state: SandboxState
    state_version: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    active_request_id: str | None
    last_checkpoint_generation: int | None
    last_restore: str | None
    deletion_state: str | None
    kiro_auth_mode: str
    kirocrew_version: str
    created_at: datetime
    updated_at: datetime
    init_expires_at: datetime | None = None


def init_lease_active(record: SandboxRecord, now: datetime) -> bool:
    """Whether a container currently owns this sandbox's initialization."""
    return record.init_expires_at is not None and record.init_expires_at > now


@dataclass(frozen=True, slots=True)
class StartLease:
    record: SandboxRecord
    authoritative: bool


class OwnerHasher:
    """Derives a stable, opaque owner hash from a Cognito subject.

    A Cognito subject is a 122-bit random identifier, so a domain-separated
    SHA-256 already resists recovery of the subject from the hash. Deriving it
    without key material means no secret has to be provisioned, rotated, or
    shipped with this sample, and the in-memory and DynamoDB registries agree on
    the same value.
    """

    _DOMAIN: Final = b"kirocrew-owner-v1\x00"

    def derive(self, cognito_subject: str) -> str:
        if not cognito_subject:
            raise ValueError("Cognito subject must not be empty.")
        return hashlib.sha256(self._DOMAIN + cognito_subject.encode()).hexdigest()


def new_sandbox_id() -> str:
    value = int.from_bytes(secrets.token_bytes(16))
    encoded = "".join(_CROCKFORD[(value >> (5 * shift)) & 31] for shift in range(25, -1, -1))
    return f"sbx_{encoded}"


def new_runtime_session_id() -> str:
    return str(uuid.uuid4())


class InMemorySandboxRegistry:
    def __init__(
        self,
        *,
        sandbox_id_factory: Callable[[], str] = new_sandbox_id,
        runtime_session_id_factory: Callable[[], str] = new_runtime_session_id,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        kirocrew_version: str = "unconfigured",
    ) -> None:
        self._hasher = OwnerHasher()
        self._sandbox_id_factory = sandbox_id_factory
        self._runtime_session_id_factory = runtime_session_id_factory
        self._clock = clock
        self._kirocrew_version = kirocrew_version
        self._by_owner: dict[str, SandboxRecord] = {}
        self._owner_by_sandbox: dict[str, str] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._lock = threading.RLock()

    def get_or_create(self, cognito_subject: str) -> SandboxRecord:
        owner_hash = self._hasher.derive(cognito_subject)
        with self._lock:
            existing = self._by_owner.get(owner_hash)
            if existing is not None:
                return existing
            now = self._clock()
            record = SandboxRecord(
                owner_hash=owner_hash,
                sandbox_id=self._sandbox_id_factory(),
                runtime_session_id=self._runtime_session_id_factory(),
                state=SandboxState.STOPPED,
                state_version=0,
                lease_owner=None,
                lease_expires_at=None,
                active_request_id=None,
                last_checkpoint_generation=None,
                last_restore=None,
                deletion_state=None,
                kiro_auth_mode="device-flow",
                kirocrew_version=self._kirocrew_version,
                created_at=now,
                updated_at=now,
            )
            self._by_owner[owner_hash] = record
            self._owner_by_sandbox[record.sandbox_id] = owner_hash
            return record

    def get(self, cognito_subject: str) -> SandboxRecord:
        owner_hash = self._hasher.derive(cognito_subject)
        with self._lock:
            record = self._by_owner.get(owner_hash)
            if record is None:
                raise SandboxUnavailableError("Sandbox is unavailable.")
            return record

    def authorize(
        self,
        cognito_subject: str,
        sandbox_id: str,
        runtime_session_id: str | None = None,
    ) -> SandboxRecord:
        record = self.get(cognito_subject)
        if record.sandbox_id != sandbox_id or (
            runtime_session_id is not None and record.runtime_session_id != runtime_session_id
        ):
            raise SandboxUnavailableError("Sandbox is unavailable.")
        return record

    def acquire_start(
        self,
        cognito_subject: str,
        lease_owner: str,
        *,
        ttl: timedelta,
        confirmed_inactive: bool = False,
    ) -> StartLease:
        if not lease_owner or ttl <= timedelta(0):
            raise ValueError("Lease owner and positive TTL are required.")
        with self._lock:
            record = self.get_or_create(cognito_subject)
            now = self._clock()
            active = record.lease_expires_at is not None and record.lease_expires_at > now
            if active:
                return StartLease(record, authoritative=record.lease_owner == lease_owner)
            if init_lease_active(record, now):
                # A container is mid-initialization; resetting the state
                # machine underneath it would poison a healthy cold start.
                return StartLease(record, authoritative=record.lease_owner == lease_owner)
            if (
                record.lease_owner is not None
                and record.lease_owner != lease_owner
                and record.state is not SandboxState.STOPPED
                and not confirmed_inactive
            ):
                raise LeaseConflictError("Expired lease requires authoritative-owner confirmation.")
            updated = replace(
                record,
                state=SandboxState.STARTING,
                state_version=record.state_version + 1,
                lease_owner=lease_owner,
                lease_expires_at=now + ttl,
                active_request_id=None,
                updated_at=now,
            )
            self._store(updated)
            return StartLease(updated, authoritative=True)

    def heartbeat(self, cognito_subject: str, lease_owner: str, *, ttl: timedelta) -> SandboxRecord:
        if ttl <= timedelta(0):
            raise ValueError("Heartbeat TTL must be positive.")
        with self._lock:
            record = self.get(cognito_subject)
            now = self._clock()
            if (
                record.lease_owner != lease_owner
                or record.lease_expires_at is None
                or record.lease_expires_at <= now
            ):
                raise LeaseConflictError("Sandbox lease is not owned by this runtime.")
            updated = replace(
                record,
                state_version=record.state_version + 1,
                lease_expires_at=now + ttl,
                updated_at=now,
            )
            self._store(updated)
            return updated

    def transition(
        self,
        cognito_subject: str,
        target: SandboxState,
        *,
        expected_version: int,
        lease_owner: str,
    ) -> SandboxRecord:
        with self._lock:
            record = self.get(cognito_subject)
            if record.state_version != expected_version:
                raise StateConflictError("Sandbox state version changed.")
            if record.lease_owner != lease_owner:
                raise LeaseConflictError("Sandbox lease is not owned by this runtime.")
            if target not in _ALLOWED_TRANSITIONS[record.state]:
                raise StateConflictError("Lifecycle transition is not allowed.")
            now = self._clock()
            stopped = target is SandboxState.STOPPED
            updated = replace(
                record,
                state=target,
                state_version=record.state_version + 1,
                lease_owner=None if stopped else record.lease_owner,
                lease_expires_at=None if stopped else record.lease_expires_at,
                active_request_id=None
                if target is not SandboxState.BUSY
                else record.active_request_id,
                updated_at=now,
            )
            self._store(updated)
            return updated

    def accept_request(
        self,
        cognito_subject: str,
        lease_owner: str,
        request_id: str,
        payload_digest: str,
    ) -> RequestDisposition:
        if not request_id or not payload_digest:
            raise ValueError("Request ID and payload digest are required.")
        with self._lock:
            record = self.get(cognito_subject)
            if record.lease_owner != lease_owner:
                raise LeaseConflictError("Sandbox lease is not owned by this runtime.")
            key = (record.sandbox_id, request_id)
            existing = self._requests.get(key)
            if existing is None:
                self._requests[key] = payload_digest
                self._store(
                    replace(
                        record,
                        active_request_id=request_id,
                        state_version=record.state_version + 1,
                        updated_at=self._clock(),
                    )
                )
                return RequestDisposition.NEW
            if hmac.compare_digest(existing, payload_digest):
                return RequestDisposition.DUPLICATE
            raise IdempotencyConflictError("Request ID was reused with another payload.")

    def record_checkpoint(
        self,
        cognito_subject: str,
        lease_owner: str,
        generation: int,
        restore_result: str | None = None,
    ) -> SandboxRecord:
        if generation <= 0:
            raise ValueError("Checkpoint generation must be positive.")
        with self._lock:
            record = self.get(cognito_subject)
            if record.lease_owner != lease_owner:
                raise LeaseConflictError("Sandbox lease is not owned by this runtime.")
            updated = replace(
                record,
                state_version=record.state_version + 1,
                last_checkpoint_generation=generation,
                last_restore=restore_result,
                updated_at=self._clock(),
            )
            self._store(updated)
            return updated

    def request_deletion(self, cognito_subject: str) -> SandboxRecord:
        with self._lock:
            record = self.get(cognito_subject)
            if record.state is not SandboxState.STOPPED:
                raise StateConflictError("Sandbox must be stopped before deletion.")
            updated = replace(
                record,
                state_version=record.state_version + 1,
                deletion_state="REQUESTED",
                updated_at=self._clock(),
            )
            self._store(updated)
            return updated

    def _store(self, record: SandboxRecord) -> None:
        self._by_owner[record.owner_hash] = record
        self._owner_by_sandbox[record.sandbox_id] = record.owner_hash
