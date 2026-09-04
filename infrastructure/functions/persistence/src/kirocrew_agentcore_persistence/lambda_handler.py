from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

_SANDBOX = re.compile(r"^sbx_[0-9A-Z]{26}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_OBJECT = re.compile(r"^[1-9][0-9]*\.json(?:\.enc)?$")
_URL_TTL: Final = timedelta(minutes=5)
_RECEIPT_TTL: Final = timedelta(minutes=10)


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing broker configuration: {name}")
    return value


def _clients() -> tuple[Any, Any, Any]:
    return boto3.client("s3"), boto3.client("kms"), boto3.client("dynamodb")


def _unbase64url(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _binding(kms: Any, token: str) -> dict[str, object]:
    try:
        payload_encoded, signature_encoded = token.split(".", 1)
        payload = cast(object, json.loads(_unbase64url(payload_encoded)))
        if not isinstance(payload, dict):
            raise ValueError
        claims = cast(dict[str, object], payload)
        verified = kms.verify(
            KeyId=_required("BINDING_KEY_ARN"),
            Message=payload_encoded.encode(),
            MessageType="RAW",
            Signature=_unbase64url(signature_encoded),
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ClientError) as error:
        raise PermissionError("Invalid runtime binding.") from error
    expires = claims.get("exp")
    if (
        verified.get("SignatureValid") is not True
        or claims.get("type") != "binding"
        or claims.get("aud") != _required("BINDING_AUDIENCE")
        or type(expires) is not int
        or expires <= int(datetime.now(UTC).timestamp())
    ):
        raise PermissionError("Invalid runtime binding.")
    return claims


def _authorize(dynamodb: Any, claims: Mapping[str, object], event: Mapping[str, object]) -> str:
    sandbox_id = event.get("sandboxId")
    runtime_session_id = event.get("runtimeSessionId")
    if (
        not isinstance(sandbox_id, str)
        or not _SANDBOX.fullmatch(sandbox_id)
        or runtime_session_id != claims.get("runtimeSessionId")
        or sandbox_id != claims.get("sandboxId")
    ):
        raise PermissionError("Invalid runtime binding.")
    response = dynamodb.get_item(
        TableName=_required("SANDBOX_TABLE"),
        Key={"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}},
        ConsistentRead=True,
        ProjectionExpression="runtimeSessionId, #state, initExpiresAt",
        ExpressionAttributeNames={"#state": "state"},
    )
    item = cast(Mapping[str, Mapping[str, str]], response.get("Item", {}))
    if item.get("runtimeSessionId", {}).get("S") != runtime_session_id:
        raise PermissionError("Invalid runtime binding.")
    if item.get("state", {}).get("S") not in {
        "STARTING",
        "RESTORING",
        "READY",
        "BUSY",
        "CHECKPOINTING",
        "STOPPING",
    }:
        # State-machine churn must never sever a live initialization: a
        # container holding an unexpired init lease keeps its storage
        # access regardless of what the state value says right now.
        expires = item.get("initExpiresAt", {}).get("N")
        now = int(datetime.now(UTC).timestamp())
        if expires is None or int(expires) <= now:
            raise PermissionError("Sandbox is unavailable.")
    return sandbox_id


def _context(sandbox_id: str) -> dict[str, str]:
    return {
        "application": "kirocrew-agentcore",
        "purpose": "sandbox-checkpoint",
        "sandboxId": sandbox_id,
    }


def _data_key(s3: Any, kms: Any, sandbox_id: str) -> dict[str, object]:
    bucket = _required("CHECKPOINT_BUCKET")
    key = f"sandboxes/{sandbox_id}/data-key.json"
    context = _context(sandbox_id)
    try:
        stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {"NoSuchKey", "404"}:
            raise
        generated = kms.generate_data_key(
            KeyId=_required("KMS_KEY_ARN"),
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        encrypted = cast(bytes, generated["CiphertextBlob"])
        body = json.dumps(
            {"ciphertext": base64.b64encode(encrypted).decode(), "schemaVersion": 1},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        try:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                ServerSideEncryption="aws:kms",
                SSEKMSKeyId=_required("KMS_KEY_ARN"),
                SSEKMSEncryptionContext=base64.b64encode(
                    json.dumps(context, sort_keys=True).encode()
                ).decode(),
                IfNoneMatch="*",
            )
            return {"plaintextKey": base64.b64encode(cast(bytes, generated["Plaintext"])).decode()}
        except ClientError as race:
            if race.response.get("Error", {}).get("Code") not in {
                "ConditionalRequestConflict",
                "PreconditionFailed",
            }:
                raise
            stored = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    value = json.loads(stored)
    encrypted_value = value.get("ciphertext") if isinstance(value, dict) else None
    if not isinstance(encrypted_value, str):
        raise RuntimeError("Stored data key is invalid.")
    decrypted = kms.decrypt(
        KeyId=_required("KMS_KEY_ARN"),
        CiphertextBlob=base64.b64decode(encrypted_value, validate=True),
        EncryptionContext=context,
    )
    return {"plaintextKey": base64.b64encode(cast(bytes, decrypted["Plaintext"])).decode()}


def _object_name(category: str, name: str) -> str:
    valid = {
        "chunks": name.endswith(".bin") and _DIGEST.fullmatch(name.removesuffix(".bin")),
        "manifests": _GENERATION_OBJECT.fullmatch(name) and name.endswith(".json.enc"),
        "commits": _GENERATION_OBJECT.fullmatch(name) and name.endswith(".json"),
    }
    if category not in valid or not valid[category]:
        raise ValueError("Invalid checkpoint object name.")
    return f"{category}/{name}"


def _presign(s3: Any, sandbox_id: str, event: Mapping[str, object]) -> dict[str, object]:
    category = event.get("category")
    name = event.get("name")
    method = event.get("method")
    if (
        not isinstance(category, str)
        or not isinstance(name, str)
        or not isinstance(method, str)
        or method
        not in {
            "GET",
            "HEAD",
            "PUT",
        }
    ):
        raise ValueError("Invalid checkpoint operation.")
    suffix = _object_name(category, name)
    params: dict[str, object] = {
        "Bucket": _required("CHECKPOINT_BUCKET"),
        "Key": f"sandboxes/{sandbox_id}/{suffix}",
    }
    headers: dict[str, str] = {}
    operation = {"GET": "get_object", "HEAD": "head_object", "PUT": "put_object"}[method]
    if method == "PUT":
        context = _context(sandbox_id)
        encoded_context = base64.b64encode(json.dumps(context, sort_keys=True).encode()).decode()
        params.update(
            ServerSideEncryption="aws:kms",
            SSEKMSKeyId=_required("KMS_KEY_ARN"),
            SSEKMSEncryptionContext=encoded_context,
        )
        headers = {
            "x-amz-server-side-encryption": "aws:kms",
            "x-amz-server-side-encryption-aws-kms-key-id": _required("KMS_KEY_ARN"),
            "x-amz-server-side-encryption-context": encoded_context,
        }
    return {
        "expiresAt": (datetime.now(UTC) + _URL_TTL).isoformat().replace("+00:00", "Z"),
        "headers": headers,
        "method": method,
        "url": s3.generate_presigned_url(
            operation,
            Params=params,
            ExpiresIn=int(_URL_TTL.total_seconds()),
            HttpMethod=method,
        ),
    }


def _list(s3: Any, sandbox_id: str, event: Mapping[str, object]) -> dict[str, object]:
    category = event.get("category")
    if category not in {"chunks", "manifests", "commits"}:
        raise ValueError("Invalid checkpoint category.")
    prefix = f"sandboxes/{sandbox_id}/{category}/"
    paginator = s3.get_paginator("list_objects_v2")
    names: list[str] = []
    for page in paginator.paginate(Bucket=_required("CHECKPOINT_BUCKET"), Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if isinstance(key, str) and key.startswith(prefix):
                names.append(key.removeprefix(prefix))
    return {"names": sorted(names)}


def _checkpoint_receipt(
    s3: Any,
    kms: Any,
    dynamodb: Any,
    sandbox_id: str,
    claims: Mapping[str, object],
    event: Mapping[str, object],
) -> dict[str, object]:
    generation = event.get("generation")
    manifest_digest = event.get("manifestDigest")
    runtime_session_id = event.get("runtimeSessionId")
    subject_hash = claims.get("subjectHash")
    if (
        type(generation) is not int
        or generation <= 0
        or not isinstance(manifest_digest, str)
        or not _DIGEST.fullmatch(manifest_digest)
        or not isinstance(runtime_session_id, str)
        or not isinstance(subject_hash, str)
        or not _DIGEST.fullmatch(subject_hash)
    ):
        raise ValueError("Invalid checkpoint receipt request.")
    raw_commit = s3.get_object(
        Bucket=_required("CHECKPOINT_BUCKET"),
        Key=f"sandboxes/{sandbox_id}/commits/{generation}.json",
    )["Body"].read()
    try:
        commit = cast(object, json.loads(raw_commit))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Committed checkpoint metadata is invalid.") from error
    if (
        not isinstance(commit, dict)
        or commit.get("generation") != generation
        or commit.get("manifestDigest") != manifest_digest
    ):
        raise ValueError("Committed checkpoint metadata does not match.")
    created_at = commit.get("committedAt")
    if not isinstance(created_at, str):
        raise ValueError("Committed checkpoint timestamp is invalid.")
    now = datetime.now(UTC)
    table_name = _required("SANDBOX_TABLE")
    dynamodb.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {
                        "pk": {"S": f"SANDBOX#{sandbox_id}"},
                        "sk": {"S": "METADATA"},
                    },
                    "UpdateExpression": (
                        "SET #state = :stopping, lastCheckpointGeneration = :generation, "
                        "lastCheckpointManifestDigest = :digest, updatedAt = :updated "
                        "ADD stateVersion :one"
                    ),
                    "ConditionExpression": (
                        "runtimeSessionId = :session AND "
                        "#state IN (:ready, :checkpointing, :stopping) AND "
                        "(attribute_not_exists(lastCheckpointGeneration) OR "
                        "lastCheckpointGeneration < :generation OR "
                        "(lastCheckpointGeneration = :generation AND "
                        "lastCheckpointManifestDigest = :digest))"
                    ),
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {
                        ":checkpointing": {"S": "CHECKPOINTING"},
                        ":digest": {"S": manifest_digest},
                        ":generation": {"N": str(generation)},
                        ":one": {"N": "1"},
                        ":ready": {"S": "READY"},
                        ":session": {"S": runtime_session_id},
                        ":stopping": {"S": "STOPPING"},
                        ":updated": {"S": now.isoformat().replace("+00:00", "Z")},
                    },
                }
            },
            {
                "Put": {
                    "TableName": table_name,
                    "Item": {
                        "pk": {"S": f"SANDBOX#{sandbox_id}"},
                        "sk": {"S": f"CHECKPOINT#{generation:020d}"},
                        "createdAt": {"S": created_at},
                        "generation": {"N": str(generation)},
                        "manifestDigest": {"S": manifest_digest},
                        "schemaVersion": {"N": "1"},
                        "status": {"S": "COMMITTED"},
                    },
                }
            },
        ]
    )
    receipt_claims = {
        "aud": _required("BINDING_AUDIENCE"),
        "exp": int((now + _RECEIPT_TTL).timestamp()),
        "generation": generation,
        "iat": int(now.timestamp()),
        "manifestDigest": manifest_digest,
        "runtimeSessionId": runtime_session_id,
        "sandboxId": sandbox_id,
        "subjectHash": subject_hash,
        "type": "checkpoint-receipt",
    }
    payload = _base64url(json.dumps(receipt_claims, sort_keys=True, separators=(",", ":")).encode())
    signed = kms.sign(
        KeyId=_required("BINDING_KEY_ARN"),
        Message=payload.encode(),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PSS_SHA_256",
    )
    signature = signed.get("Signature")
    if not isinstance(signature, bytes):
        raise RuntimeError("Checkpoint receipt signature is unavailable.")
    return {
        "checkpointReceipt": f"{payload}.{_base64url(signature)}",
        "generation": generation,
        "manifestDigest": manifest_digest,
    }


def handler(event: Mapping[str, object], _context_value: object) -> dict[str, object]:
    operation = event.get("operation")
    if operation == "audit":
        return {"status": "scheduled"}
    s3, kms, dynamodb = _clients()
    token = event.get("bindingToken")
    if not isinstance(token, str):
        raise PermissionError("Runtime binding is required.")
    claims = _binding(kms, token)
    sandbox_id = _authorize(dynamodb, claims, event)
    if operation == "dataKey":
        return _data_key(s3, kms, sandbox_id)
    if operation == "presign":
        return _presign(s3, sandbox_id, event)
    if operation == "list":
        return _list(s3, sandbox_id, event)
    if operation == "checkpointReceipt":
        return _checkpoint_receipt(s3, kms, dynamodb, sandbox_id, claims, event)
    raise ValueError("Unsupported persistence broker operation.")
