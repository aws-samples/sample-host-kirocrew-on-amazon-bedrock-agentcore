from __future__ import annotations

from datetime import UTC, datetime

from kirocrew_agentcore_control.dynamodb_entities import (
    ACQUIRE_LEASE_CONDITION,
    CREATE_MAPPING_CONDITION,
    REQUEST_IDEMPOTENCY_CONDITION,
    STATE_VERSION_CONDITION,
    mapping_item,
    request_item,
    sandbox_item,
)
from kirocrew_agentcore_control.sandbox import SandboxRecord, SandboxState

NOW = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)


def record(*, with_optional: bool) -> SandboxRecord:
    return SandboxRecord(
        owner_hash="a" * 64,
        sandbox_id="sbx_01J00000000000000000000000",
        runtime_session_id="7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
        state=SandboxState.READY,
        state_version=4,
        lease_owner="runtime" if with_optional else None,
        lease_expires_at=NOW if with_optional else None,
        active_request_id="request" if with_optional else None,
        last_checkpoint_generation=3 if with_optional else None,
        last_restore="RESTORED" if with_optional else None,
        deletion_state="RETAINING" if with_optional else None,
        kiro_auth_mode="device-flow",
        kirocrew_version="1.2.3",
        created_at=NOW,
        updated_at=NOW,
    )


def test_mapping_and_sandbox_entities_separate_owner_lookup_from_metadata() -> None:
    mapping = mapping_item(record(with_optional=False))
    assert mapping["pk"] == f"OWNER#{'a' * 64}"
    assert mapping["sk"] == "SANDBOX"
    assert "email" not in mapping and "subject" not in mapping

    minimal = sandbox_item(record(with_optional=False))
    assert minimal["pk"] == "SANDBOX#sbx_01J00000000000000000000000"
    assert minimal["state"] == "READY"
    assert "leaseOwner" not in minimal

    complete = sandbox_item(record(with_optional=True))
    assert complete["leaseOwner"] == "runtime"
    assert complete["leaseExpiresAt"] == "2026-08-17T16:00:00Z"
    assert complete["lastCheckpointGeneration"] == 3
    assert complete["deletionState"] == "RETAINING"


def test_request_entity_and_conditions_enforce_conditional_writes() -> None:
    item = request_item("sandbox", "request", "digest", NOW)
    assert item == {
        "pk": "SANDBOX#sandbox",
        "sk": "REQUEST#request",
        "payloadDigest": "digest",
        "createdAt": "2026-08-17T16:00:00Z",
    }
    assert CREATE_MAPPING_CONDITION == ("attribute_not_exists(pk) AND attribute_not_exists(sk)")
    assert ":now" in ACQUIRE_LEASE_CONDITION
    assert ":expectedVersion" in STATE_VERSION_CONDITION
    assert REQUEST_IDEMPOTENCY_CONDITION == (
        "attribute_not_exists(pk) AND attribute_not_exists(sk)"
    )
