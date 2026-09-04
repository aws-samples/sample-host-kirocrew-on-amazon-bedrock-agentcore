from __future__ import annotations

import json
from pathlib import Path

import pytest
from kirocrew_agentcore_adapter.generated_protocol import PROTOCOL_VERSION
from kirocrew_agentcore_adapter.protocol import (
    FrameChunk,
    IdempotencyResult,
    IdempotencyWindow,
    ProtocolContractError,
    SequenceTracker,
    chunk_payload,
    reassemble_chunks,
    sanitize_error,
    validate_envelope,
    validate_schema,
)

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "contracts/fixtures"
VALID_CORRELATION_ID = "01J00000000000000000000003"


def load_fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_valid_envelope_and_named_schema() -> None:
    fixture = load_fixture("valid-client-message.json")
    assert validate_envelope(fixture)["version"] == PROTOCOL_VERSION
    validate_schema("protocol-envelope.schema.json", fixture)


@pytest.mark.parametrize(
    ("fixture_name", "code"),
    [
        ("invalid-version-message.json", "UNSUPPORTED_PROTOCOL"),
    ],
)
def test_invalid_envelope_fixture(fixture_name: str, code: str) -> None:
    with pytest.raises(ProtocolContractError) as raised:
        validate_envelope(load_fixture(fixture_name))
    assert raised.value.code == code
    assert str(raised.value)


def test_malformed_envelope_uses_invalid_message() -> None:
    with pytest.raises(ProtocolContractError) as raised:
        validate_envelope({"version": PROTOCOL_VERSION})
    assert raised.value.code == "INVALID_MESSAGE"


def test_chunk_round_trip_and_empty_payload() -> None:
    payload = bytes(range(256)) * 250
    chunks = chunk_payload(payload)
    assert len(chunks) == 3
    assert all(len(chunk.data) <= 24 * 1024 for chunk in chunks)
    assert reassemble_chunks(chunks) == payload
    assert chunk_payload(b"") == (FrameChunk(index=0, total=1, data=b""),)


def test_chunk_validation_errors() -> None:
    with pytest.raises(ValueError, match="positive"):
        chunk_payload(b"data", 0)
    with pytest.raises(ProtocolContractError) as empty:
        reassemble_chunks(())
    assert empty.value.code == "INVALID_MESSAGE"
    with pytest.raises(ProtocolContractError) as out_of_order:
        reassemble_chunks(
            (
                FrameChunk(index=1, total=2, data=b"a"),
                FrameChunk(index=0, total=2, data=b"b"),
            )
        )
    assert out_of_order.value.code == "SEQUENCE_ERROR"


def test_sequence_tracker_requires_contiguous_messages() -> None:
    tracker = SequenceTracker()
    tracker.accept("request", 0)
    tracker.accept("request", 1)
    tracker.accept("other", 0)
    with pytest.raises(ProtocolContractError) as raised:
        tracker.accept("request", 3)
    assert raised.value.code == "SEQUENCE_ERROR"


def test_idempotency_window_classifies_replays_and_conflicts() -> None:
    window = IdempotencyWindow()
    assert window.check("request-a", {"text": "same"}) is IdempotencyResult.NEW
    assert window.check("request-b", {"text": "other"}) is IdempotencyResult.NEW
    assert window.check("request-a", {"text": "same"}) is IdempotencyResult.DUPLICATE
    with pytest.raises(ProtocolContractError) as raised:
        window.check("request-a", {"text": "changed"})
    assert raised.value.code == "IDEMPOTENCY_CONFLICT"


def test_error_sanitization_removes_secrets_and_unknown_fields() -> None:
    fixture = load_fixture("secret-bearing-error-input.json")
    assert isinstance(fixture, dict)
    sanitized = sanitize_error(fixture)
    assert sanitized == {
        "code": "INTERNAL_ERROR",
        "category": "INTERNAL",
        "message": "upstream failed",
        "retryable": False,
        "correlationId": VALID_CORRELATION_ID,
    }


def test_error_sanitization_keeps_only_safe_scalar_details() -> None:
    sanitized = sanitize_error(
        {
            "code": "CHECKPOINT_FAILED",
            "category": "PERSISTENCE",
            "message": "checkpoint unavailable",
            "retryable": True,
            "correlationId": VALID_CORRELATION_ID,
            "details": {
                "generation": 4,
                "state": "Bearer hidden",
                "limit": None,
                "operation": ["not", "scalar"],
                "unknown": "removed",
            },
        }
    )
    assert sanitized["details"] == {"generation": 4, "limit": None}


def test_error_sanitization_replaces_invalid_types_and_secret_message() -> None:
    sanitized = sanitize_error(
        {
            "code": "NOT_A_CODE",
            "category": "NOT_A_CATEGORY",
            "message": "Bearer hidden-value",
            "correlationId": VALID_CORRELATION_ID,
            "details": "not-an-object",
        }
    )
    assert sanitized["code"] == "INTERNAL_ERROR"
    assert sanitized["category"] == "INTERNAL"
    assert sanitized["message"] == "The request could not be completed."
    assert "details" not in sanitized
