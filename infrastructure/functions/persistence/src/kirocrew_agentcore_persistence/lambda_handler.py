from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Final, cast

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

_SANDBOX = re.compile(r"^sbx_[0-9A-Z]{26}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_GENERATION_OBJECT = re.compile(r"^[1-9][0-9]*\.json(?:\.enc)?$")
_INIT_OWNER = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_RESTORE_OUTCOME = re.compile(r"^[a-z][a-z-]{0,31}$")
_DIAGNOSTIC = re.compile(r"^[\x20-\x7e]{1,256}$")
_URL_TTL: Final = timedelta(minutes=5)
_RECEIPT_TTL: Final = timedelta(minutes=10)
_LIVE_STATES: Final = frozenset(
    {"STARTING", "RESTORING", "READY", "BUSY", "CHECKPOINTING", "STOPPING"}
)
# Tokens the broker honours under ``bindingToken``: the control plane's
# browser-scoped binding token, and the runtime-session token this broker mints
# for a container that won its sandbox's initialization. Both carry the same
# sandbox/session/subject claims; only the lifetime and the issuer differ.
_TOKEN_TYPES: Final = frozenset({"binding", "runtime-session"})


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Missing broker configuration: {name}")
    return value


def _runtime_session_ttl() -> timedelta:
    return timedelta(seconds=int(os.environ.get("RUNTIME_SESSION_TOKEN_TTL_SECONDS", "28800")))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


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
        or claims.get("type") not in _TOKEN_TYPES
        or claims.get("aud") != _required("BINDING_AUDIENCE")
        or type(expires) is not int
        or expires <= int(datetime.now(UTC).timestamp())
    ):
        raise PermissionError("Invalid runtime binding.")
    return claims


def _claimed_sandbox(claims: Mapping[str, object], event: Mapping[str, object]) -> str:
    """Return the sandbox the request names, once it matches the verified claims."""
    sandbox_id = event.get("sandboxId")
    runtime_session_id = event.get("runtimeSessionId")
    if (
        not isinstance(sandbox_id, str)
        or not _SANDBOX.fullmatch(sandbox_id)
        or runtime_session_id != claims.get("runtimeSessionId")
        or sandbox_id != claims.get("sandboxId")
    ):
        raise PermissionError("Invalid runtime binding.")
    return sandbox_id


def _authorize(dynamodb: Any, claims: Mapping[str, object], event: Mapping[str, object]) -> str:
    sandbox_id = _claimed_sandbox(claims, event)
    runtime_session_id = event.get("runtimeSessionId")
    response = dynamodb.get_item(
        TableName=_required("SANDBOX_TABLE"),
        Key=_record_key(sandbox_id),
        ConsistentRead=True,
        ProjectionExpression="runtimeSessionId, #state, initExpiresAt",
        ExpressionAttributeNames={"#state": "state"},
    )
    item = cast(Mapping[str, Mapping[str, str]], response.get("Item", {}))
    if item.get("runtimeSessionId", {}).get("S") != runtime_session_id:
        raise PermissionError("Invalid runtime binding.")
    if item.get("state", {}).get("S") not in _LIVE_STATES:
        # State-machine churn must never sever a live initialization: a
        # container holding an unexpired init lease keeps its storage
        # access regardless of what the state value says right now.
        expires = item.get("initExpiresAt", {}).get("N")
        now = int(datetime.now(UTC).timestamp())
        if expires is None or int(expires) <= now:
            raise PermissionError("Sandbox is unavailable.")
    return sandbox_id


def _record_key(sandbox_id: str) -> dict[str, dict[str, str]]:
    return {"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}}


def _read_record(dynamodb: Any, sandbox_id: str) -> dict[str, object]:
    """Return the lifecycle fields the runtime needs from its own sandbox record.

    A runtime reads its record before claiming initialization, so this does
    not require the record to name the caller's session: the caller compares
    the returned session id itself and walks away when it has been rotated.
    """
    response = dynamodb.get_item(
        TableName=_required("SANDBOX_TABLE"),
        Key=_record_key(sandbox_id),
        ConsistentRead=True,
        ProjectionExpression="runtimeSessionId, #state, lastCheckpointGeneration",
        ExpressionAttributeNames={"#state": "state"},
    )
    item = cast(Mapping[str, Mapping[str, str]], response.get("Item", {}))
    generation = item.get("lastCheckpointGeneration", {}).get("N")
    return {
        "lastCheckpointGeneration": None if generation is None else int(generation),
        "runtimeSessionId": item.get("runtimeSessionId", {}).get("S"),
        "state": item.get("state", {}).get("S"),
    }


def _conditional_update(
    dynamodb: Any,
    sandbox_id: str,
    update: str,
    condition: str,
    values: Mapping[str, Mapping[str, str]],
    *,
    reason: Callable[[Mapping[str, Mapping[str, str]]], str] | None = None,
) -> dict[str, object]:
    """Apply one server-defined conditional update to the caller's own record.

    A failed condition is a normal outcome (``applied: false``) that the
    runtime maps to its lifecycle decisions; every other DynamoDB failure
    propagates as a broker error. Pass ``reason`` to also read the rejecting
    item back and name the clause that failed: one message for every
    rejection sends whoever debugs it after the wrong cause.
    """
    request: dict[str, object] = {
        "TableName": _required("SANDBOX_TABLE"),
        "Key": _record_key(sandbox_id),
        "UpdateExpression": update,
        "ConditionExpression": condition,
        "ExpressionAttributeValues": dict(values),
    }
    # DynamoDB rejects a placeholder that no expression uses, so the reserved
    # word alias is attached only to the updates that touch the state field.
    if "#state" in update or "#state" in condition:
        request["ExpressionAttributeNames"] = {"#state": "state"}
    if reason is not None:
        request["ReturnValuesOnConditionCheckFailure"] = "ALL_OLD"
    try:
        dynamodb.update_item(**request)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        if reason is None:
            return {"applied": False}
        item = cast(Mapping[str, Mapping[str, str]], error.response.get("Item", {}))
        return {"applied": False, "reason": reason(item)}
    return {"applied": True}


def _init_owner(event: Mapping[str, object]) -> str:
    owner = event.get("initOwner")
    if not isinstance(owner, str) or not _INIT_OWNER.fullmatch(owner):
        raise ValueError("Invalid initialization owner.")
    return owner


def _diagnostic(event: Mapping[str, object], name: str) -> str:
    value = event.get(name, "UNKNOWN")
    if not isinstance(value, str) or not _DIAGNOSTIC.fullmatch(value):
        raise ValueError("Invalid initialization diagnostic.")
    return value


def _runtime_session_token(kms: Any, claims: Mapping[str, object], now: datetime) -> str:
    """Mint the token a container uses for its own lifecycle and persistence calls.

    The browser's binding token expires after 30 minutes and is only renewed
    while a page is open, but a sandbox keeps heartbeating, checkpointing, and
    finishing background work with no browser attached. The runtime-session
    token carries the same sandbox/session/subject claims for up to the
    platform session lifetime; every broker call still checks the record names
    this session, so a rotated session invalidates it immediately.
    """
    session_claims = {
        "aud": _required("BINDING_AUDIENCE"),
        "exp": int((now + _runtime_session_ttl()).timestamp()),
        "iat": int(now.timestamp()),
        "runtimeSessionId": claims["runtimeSessionId"],
        "sandboxId": claims["sandboxId"],
        "subjectHash": claims.get("subjectHash"),
        "type": "runtime-session",
    }
    payload = _base64url(json.dumps(session_claims, sort_keys=True, separators=(",", ":")).encode())
    signed = kms.sign(
        KeyId=_required("BINDING_KEY_ARN"),
        Message=payload.encode(),
        MessageType="RAW",
        SigningAlgorithm="RSASSA_PSS_SHA_256",
    )
    signature = signed.get("Signature")
    if not isinstance(signature, bytes):
        raise RuntimeError("Runtime session token signature is unavailable.")
    return f"{payload}.{_base64url(signature)}"


def _claim_rejection(
    item: Mapping[str, Mapping[str, str]], owner: str, epoch: int, deadline: str
) -> str:
    """Name the clause that rejected an initialization claim.

    ``initialization is in progress elsewhere`` reads as a live competing
    container even when the record is simply not claimable from its current
    state, or when a live owner is still heartbeating its lease -- two causes
    with opposite remedies (retry vs reconnect).
    """
    if not item:
        return "Sandbox initialization could not be claimed."
    init_owner = item.get("initOwner", {}).get("S")
    expires = item.get("initExpiresAt", {}).get("N")
    if init_owner and init_owner != owner and expires is not None and int(expires) >= epoch:
        return "Another container owns this sandbox initialization."
    state = item.get("state", {}).get("S")
    lease = item.get("leaseExpiresAt", {}).get("S")
    if state == "READY" and lease is not None and lease >= deadline:
        return "A live container still holds this sandbox; its lease has not expired."
    if state:
        return f"Sandbox is not claimable from state {state}."
    return "Sandbox initialization could not be claimed."


def _acquire_init(
    kms: Any,
    dynamodb: Any,
    sandbox_id: str,
    claims: Mapping[str, object],
    event: Mapping[str, object],
) -> dict[str, object]:
    owner = _init_owner(event)
    now = datetime.now(UTC)
    epoch = int(now.timestamp())
    deadline = _iso(now)
    result = _conditional_update(
        dynamodb,
        sandbox_id,
        (
            "SET #state = :restoring, initOwner = :owner, "
            "initExpiresAt = :expires, updatedAt = :updated "
            "ADD stateVersion :one"
        ),
        # READY is claimable once the start lease is dead. A container left
        # gone by idle reclaim publishes nothing, so without this the record
        # stays READY forever, every later invocation fails the state clause,
        # and the sandbox self-locks with no way out - Stop travels the same
        # path, so the user cannot even stop it to clear the record. Requiring
        # a dead lease is what separates a container that is gone (nothing has
        # heartbeated for 90s) from a live warm owner, which keeps its own
        # restore from being yanked out from under it and republishes READY
        # through healReady instead. The initOwner lease keeps two *live*
        # initializers apart, and the caller's token has already been matched
        # to this record's session, so no other tenant can reach this claim.
        (
            "runtimeSessionId = :session "
            "AND (#state IN (:starting, :restoring) "
            "OR (#state = :ready AND (attribute_not_exists(leaseExpiresAt) "
            "OR leaseExpiresAt < :deadline))) "
            "AND (attribute_not_exists(initOwner) "
            "OR initOwner = :owner OR initExpiresAt < :now)"
        ),
        {
            ":deadline": {"S": deadline},
            ":expires": {"N": str(epoch + 90)},
            ":now": {"N": str(epoch)},
            ":one": {"N": "1"},
            ":owner": {"S": owner},
            ":ready": {"S": "READY"},
            ":restoring": {"S": "RESTORING"},
            ":session": {"S": str(claims["runtimeSessionId"])},
            ":starting": {"S": "STARTING"},
            ":updated": {"S": _iso(now)},
        },
        reason=lambda item: _claim_rejection(item, owner, epoch, deadline),
    )
    if result["applied"]:
        result["runtimeSessionToken"] = _runtime_session_token(kms, claims, now)
    return result


def _heartbeat_init(
    dynamodb: Any, sandbox_id: str, event: Mapping[str, object]
) -> dict[str, object]:
    owner = _init_owner(event)
    now = datetime.now(UTC)
    return _conditional_update(
        dynamodb,
        sandbox_id,
        "SET initExpiresAt = :expires, updatedAt = :updated",
        "initOwner = :owner",
        {
            ":expires": {"N": str(int(now.timestamp()) + 90)},
            ":owner": {"S": owner},
            ":updated": {"S": _iso(now)},
        },
    )


def _heartbeat_lease(
    dynamodb: Any, sandbox_id: str, claims: Mapping[str, object]
) -> dict[str, object]:
    now = datetime.now(UTC)
    return _conditional_update(
        dynamodb,
        sandbox_id,
        "SET leaseExpiresAt = :expires, updatedAt = :updated",
        "leaseOwner = :owner",
        {
            ":expires": {"S": _iso(now + timedelta(seconds=90))},
            ":owner": {"S": str(claims["runtimeSessionId"])},
            ":updated": {"S": _iso(now)},
        },
    )


def _heal_ready(dynamodb: Any, sandbox_id: str, claims: Mapping[str, object]) -> dict[str, object]:
    return _conditional_update(
        dynamodb,
        sandbox_id,
        "SET #state = :ready, updatedAt = :updated ADD stateVersion :one",
        "runtimeSessionId = :session AND #state IN (:starting)",
        {
            ":one": {"N": "1"},
            ":ready": {"S": "READY"},
            ":session": {"S": str(claims["runtimeSessionId"])},
            ":starting": {"S": "STARTING"},
            ":updated": {"S": _iso(datetime.now(UTC))},
        },
    )


def _mark_ready(
    dynamodb: Any, sandbox_id: str, claims: Mapping[str, object], event: Mapping[str, object]
) -> dict[str, object]:
    owner = _init_owner(event)
    outcome = event.get("restoreOutcome")
    if not isinstance(outcome, str) or not _RESTORE_OUTCOME.fullmatch(outcome):
        raise ValueError("Invalid restore outcome.")
    # Ownership, not the state value, authorizes publishing READY: a lease
    # reclaim may have flipped the state to STARTING mid-flight.
    return _conditional_update(
        dynamodb,
        sandbox_id,
        (
            "SET #state = :ready, lastRestore = :restore, updatedAt = :updated "
            "ADD stateVersion :one REMOVE initOwner, initExpiresAt"
        ),
        "runtimeSessionId = :session AND initOwner = :owner",
        {
            ":one": {"N": "1"},
            ":owner": {"S": owner},
            ":ready": {"S": "READY"},
            ":restore": {"S": outcome.upper()},
            ":session": {"S": str(claims["runtimeSessionId"])},
            ":updated": {"S": _iso(datetime.now(UTC))},
        },
    )


def _mark_error(
    dynamodb: Any, sandbox_id: str, claims: Mapping[str, object], event: Mapping[str, object]
) -> dict[str, object]:
    # Only the initialization owner may poison the record: a container that
    # lost the ownership race must walk away silently instead of breaking the
    # winner's restore mid-flight.
    values: dict[str, dict[str, str]] = {
        ":detail": {"S": _diagnostic(event, "failureDetail")},
        ":error": {"S": "ERROR"},
        ":failure": {"S": _diagnostic(event, "failureType")},
        ":one": {"N": "1"},
        ":session": {"S": str(claims["runtimeSessionId"])},
        ":upstream": {"S": _diagnostic(event, "upstreamException")},
        ":updated": {"S": _iso(datetime.now(UTC))},
    }
    if event.get("initOwner") is None:
        condition = "runtimeSessionId = :session AND #state IN (:starting, :restoring)"
        values[":restoring"] = {"S": "RESTORING"}
        values[":starting"] = {"S": "STARTING"}
    else:
        condition = "runtimeSessionId = :session AND initOwner = :owner"
        values[":owner"] = {"S": _init_owner(event)}
    return _conditional_update(
        dynamodb,
        sandbox_id,
        (
            "SET #state = :error, lastInitializationFailure = :failure, "
            "lastInitializationFailureDetail = :detail, "
            "lastInitializationFailureException = :upstream, updatedAt = :updated "
            "ADD stateVersion :one REMOVE initOwner, initExpiresAt"
        ),
        condition,
        values,
    )


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
    if not isinstance(category, str) or category not in {"chunks", "manifests", "commits"}:
        raise ValueError("Invalid checkpoint category.")
    prefix = f"sandboxes/{sandbox_id}/{category}/"
    # An optional object name narrows the listing to that one key, which is how a
    # caller asks "does this object exist?" without a HeadObject: S3 answers a
    # permitted listing with an empty result for a missing key, where HeadObject
    # would answer 403 (it carries no `s3:prefix` context key, so the broker's
    # prefix-conditioned ListBucket grant cannot authorize it, and S3 hides the
    # 404 from a caller it believes may not list). Narrowing keeps `s3:prefix`
    # under `sandboxes/`, so the existing conditioned grant already allows it.
    name = event.get("name")
    scan = prefix
    if name is not None:
        if not isinstance(name, str):
            raise ValueError("Invalid checkpoint object name.")
        scan = f"sandboxes/{sandbox_id}/{_object_name(category, name)}"
    paginator = s3.get_paginator("list_objects_v2")
    names: list[str] = []
    for page in paginator.paginate(Bucket=_required("CHECKPOINT_BUCKET"), Prefix=scan):
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
    # Only a final checkpoint (Stop safely) may advance the state machine to
    # STOPPING; a mid-session durability checkpoint records the generation
    # pointer while the sandbox keeps serving.
    final = event.get("final", True)
    if (
        type(generation) is not int
        or generation <= 0
        or not isinstance(manifest_digest, str)
        or not _DIGEST.fullmatch(manifest_digest)
        or not isinstance(runtime_session_id, str)
        or not isinstance(subject_hash, str)
        or not _DIGEST.fullmatch(subject_hash)
        or type(final) is not bool
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
    if final:
        update_expression = (
            "SET #state = :stopping, lastCheckpointGeneration = :generation, "
            "lastCheckpointManifestDigest = :digest, updatedAt = :updated "
            "ADD stateVersion :one"
        )
        condition_expression = (
            "runtimeSessionId = :session AND "
            "#state IN (:ready, :checkpointing, :stopping) AND "
            "(attribute_not_exists(lastCheckpointGeneration) OR "
            "lastCheckpointGeneration < :generation OR "
            "(lastCheckpointGeneration = :generation AND "
            "lastCheckpointManifestDigest = :digest))"
        )
        state_values: dict[str, dict[str, str]] = {
            ":checkpointing": {"S": "CHECKPOINTING"},
            ":ready": {"S": "READY"},
            ":stopping": {"S": "STOPPING"},
        }
    else:
        update_expression = (
            "SET lastCheckpointGeneration = :generation, "
            "lastCheckpointManifestDigest = :digest, updatedAt = :updated "
            "ADD stateVersion :one"
        )
        condition_expression = (
            "runtimeSessionId = :session AND "
            "#state IN (:ready, :busy, :checkpointing) AND "
            "(attribute_not_exists(lastCheckpointGeneration) OR "
            "lastCheckpointGeneration < :generation OR "
            "(lastCheckpointGeneration = :generation AND "
            "lastCheckpointManifestDigest = :digest))"
        )
        state_values = {
            ":busy": {"S": "BUSY"},
            ":checkpointing": {"S": "CHECKPOINTING"},
            ":ready": {"S": "READY"},
        }
    dynamodb.transact_write_items(
        TransactItems=[
            {
                "Update": {
                    "TableName": table_name,
                    "Key": {
                        "pk": {"S": f"SANDBOX#{sandbox_id}"},
                        "sk": {"S": "METADATA"},
                    },
                    "UpdateExpression": update_expression,
                    "ConditionExpression": condition_expression,
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": {
                        ":digest": {"S": manifest_digest},
                        ":generation": {"N": str(generation)},
                        ":one": {"N": "1"},
                        ":session": {"S": runtime_session_id},
                        ":updated": {"S": now.isoformat().replace("+00:00", "Z")},
                        **state_values,
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
    if operation == "readRecord":
        return _read_record(dynamodb, _claimed_sandbox(claims, event))
    sandbox_id = _authorize(dynamodb, claims, event)
    if operation == "lease":
        return {"authorized": True}
    if operation == "acquireInit":
        return _acquire_init(kms, dynamodb, sandbox_id, claims, event)
    if operation == "heartbeatInit":
        return _heartbeat_init(dynamodb, sandbox_id, event)
    if operation == "heartbeatLease":
        return _heartbeat_lease(dynamodb, sandbox_id, claims)
    if operation == "healReady":
        return _heal_ready(dynamodb, sandbox_id, claims)
    if operation == "markReady":
        return _mark_ready(dynamodb, sandbox_id, claims, event)
    if operation == "markError":
        return _mark_error(dynamodb, sandbox_id, claims, event)
    if operation == "dataKey":
        return _data_key(s3, kms, sandbox_id)
    if operation == "presign":
        return _presign(s3, sandbox_id, event)
    if operation == "list":
        return _list(s3, sandbox_id, event)
    if operation == "checkpointReceipt":
        return _checkpoint_receipt(s3, kms, dynamodb, sandbox_id, claims, event)
    raise ValueError("Unsupported persistence broker operation.")
