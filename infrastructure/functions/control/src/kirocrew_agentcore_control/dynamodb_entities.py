from __future__ import annotations

from datetime import datetime
from typing import Final

from kirocrew_agentcore_control.sandbox import SandboxRecord

CREATE_MAPPING_CONDITION: Final = "attribute_not_exists(pk) AND attribute_not_exists(sk)"
ACQUIRE_LEASE_CONDITION: Final = (
    "attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt <= :now OR leaseOwner = :leaseOwner"
)
STATE_VERSION_CONDITION: Final = "stateVersion = :expectedVersion AND leaseOwner = :leaseOwner"
REQUEST_IDEMPOTENCY_CONDITION: Final = "attribute_not_exists(pk) AND attribute_not_exists(sk)"


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def mapping_item(record: SandboxRecord) -> dict[str, object]:
    return {
        "pk": f"OWNER#{record.owner_hash}",
        "sk": "SANDBOX",
        "sandboxId": record.sandbox_id,
        "runtimeSessionId": record.runtime_session_id,
        "createdAt": _timestamp(record.created_at),
        "updatedAt": _timestamp(record.updated_at),
    }


def sandbox_item(record: SandboxRecord) -> dict[str, object]:
    item: dict[str, object] = {
        "pk": f"SANDBOX#{record.sandbox_id}",
        "sk": "METADATA",
        "ownerHash": record.owner_hash,
        "runtimeSessionId": record.runtime_session_id,
        "state": record.state.value,
        "stateVersion": record.state_version,
        "kiroAuthMode": record.kiro_auth_mode,
        "kiroCrewVersion": record.kirocrew_version,
        "createdAt": _timestamp(record.created_at),
        "updatedAt": _timestamp(record.updated_at),
    }
    optional = {
        "leaseOwner": record.lease_owner,
        "leaseExpiresAt": (
            _timestamp(record.lease_expires_at) if record.lease_expires_at is not None else None
        ),
        "activeRequestId": record.active_request_id,
        "lastCheckpointGeneration": record.last_checkpoint_generation,
        "lastRestore": record.last_restore,
        "deletionState": record.deletion_state,
    }
    item.update({key: value for key, value in optional.items() if value is not None})
    return item


def request_item(
    sandbox_id: str,
    request_id: str,
    payload_digest: str,
    created_at: datetime,
) -> dict[str, object]:
    return {
        "pk": f"SANDBOX#{sandbox_id}",
        "sk": f"REQUEST#{request_id}",
        "payloadDigest": payload_digest,
        "createdAt": _timestamp(created_at),
    }
