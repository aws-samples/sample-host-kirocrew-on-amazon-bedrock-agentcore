from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from typing import Final, cast

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from kirocrew_agentcore_adapter.generated_protocol import (
    ERROR_CATEGORIES,
    ERROR_CODES,
    PROTOCOL_VERSION,
    ErrorCategory,
    ErrorCode,
    ProtocolEnvelope,
    ProtocolError,
)

FRAME_PAYLOAD_LIMIT: Final = 24 * 1024
_SECRET_TEXT = re.compile(
    r"(?i)(?:bearer\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"-----BEGIN\s+(?:RSA\s+|EC\s+|OPENSSH\s+)?PRIVATE KEY-----)"
)
_SAFE_DETAIL_KEYS: Final = frozenset(
    {"feature", "generation", "limit", "operation", "retryAfterSeconds", "state"}
)


class ProtocolContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FrameChunk:
    index: int
    total: int
    data: bytes


class IdempotencyResult(StrEnum):
    NEW = "NEW"
    DUPLICATE = "DUPLICATE"


def _schema(name: str) -> dict[str, object]:
    resource = files("kirocrew_agentcore_adapter.schemas").joinpath(name)
    return cast(dict[str, object], json.loads(resource.read_text(encoding="utf-8")))


def validate_schema(name: str, value: object) -> None:
    try:
        Draft202012Validator(_schema(name), format_checker=FormatChecker()).validate(value)
    except ValidationError as error:
        if isinstance(value, Mapping) and value.get("version") != PROTOCOL_VERSION:
            raise ProtocolContractError(
                "UNSUPPORTED_PROTOCOL", "Unsupported protocol version."
            ) from error
        raise ProtocolContractError(
            "INVALID_MESSAGE", "Message does not match the protocol contract."
        ) from error


def validate_envelope(value: object) -> ProtocolEnvelope:
    validate_schema("protocol-envelope.schema.json", value)
    return cast(ProtocolEnvelope, value)


def chunk_payload(payload: bytes, limit: int = FRAME_PAYLOAD_LIMIT) -> tuple[FrameChunk, ...]:
    if limit <= 0:
        raise ValueError("Chunk limit must be positive.")
    parts = [payload[offset : offset + limit] for offset in range(0, len(payload), limit)] or [b""]
    total = len(parts)
    return tuple(
        FrameChunk(index=index, total=total, data=data) for index, data in enumerate(parts)
    )


def reassemble_chunks(chunks: tuple[FrameChunk, ...]) -> bytes:
    if not chunks:
        raise ProtocolContractError("INVALID_MESSAGE", "At least one frame chunk is required.")
    total = chunks[0].total
    if total != len(chunks) or any(
        chunk.total != total or chunk.index != index for index, chunk in enumerate(chunks)
    ):
        raise ProtocolContractError(
            "SEQUENCE_ERROR", "Frame chunks are incomplete or out of order."
        )
    return b"".join(chunk.data for chunk in chunks)


class SequenceTracker:
    def __init__(self) -> None:
        self._last_seen: dict[str, int] = {}

    def accept(self, stream_id: str, sequence: int) -> None:
        expected = self._last_seen.get(stream_id, -1) + 1
        if sequence != expected:
            raise ProtocolContractError("SEQUENCE_ERROR", "Message sequence is not contiguous.")
        self._last_seen[stream_id] = sequence


class IdempotencyWindow:
    def __init__(self) -> None:
        self._digests: dict[str, bytes] = {}

    def check(self, request_id: str, payload: Mapping[str, object]) -> IdempotencyResult:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(encoded).digest()
        existing = self._digests.get(request_id)
        if existing is None:
            self._digests[request_id] = digest
            return IdempotencyResult.NEW
        if existing == digest:
            return IdempotencyResult.DUPLICATE
        raise ProtocolContractError(
            "IDEMPOTENCY_CONFLICT", "Request ID was reused with another payload."
        )


def sanitize_error(value: Mapping[str, object]) -> ProtocolError:
    code = str(value.get("code", "INTERNAL_ERROR"))
    category = str(value.get("category", "INTERNAL"))
    if code not in ERROR_CODES:
        code = "INTERNAL_ERROR"
    if category not in ERROR_CATEGORIES:
        category = "INTERNAL"
    raw_message = str(value.get("message", "The request could not be completed."))
    message = (
        "The request could not be completed."
        if _SECRET_TEXT.search(raw_message)
        else raw_message[:512]
    )
    correlation_id = str(value.get("correlationId", ""))
    details: dict[str, str | int | float | bool | None] = {}
    raw_details = value.get("details")
    if isinstance(raw_details, Mapping):
        for key, detail in raw_details.items():
            if key in _SAFE_DETAIL_KEYS and (
                detail is None or isinstance(detail, str | int | float | bool)
            ):
                text = str(detail)
                if not _SECRET_TEXT.search(text):
                    details[key] = detail
    result: ProtocolError = {
        "code": cast(ErrorCode, code),
        "category": cast(ErrorCategory, category),
        "message": message,
        "retryable": bool(value.get("retryable", False)),
        "correlationId": correlation_id,
    }
    if details:
        result["details"] = details
    validate_schema("error.schema.json", result)
    return result
