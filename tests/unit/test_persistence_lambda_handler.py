from __future__ import annotations

import base64
import json
import re
from datetime import UTC, datetime
from typing import cast

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from kirocrew_agentcore_persistence import lambda_handler as broker

SANDBOX = "sbx_01J00000000000000000000000"
SESSION = "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4"
DIGEST = "a" * 64
SUBJECT_HASH = "b" * 64


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "operation",
    )


class Body:
    def __init__(self, value: bytes) -> None:
        self.value = value

    def read(self) -> bytes:
        return self.value


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.get_errors: dict[str, ClientError] = {}
        self.put_error: ClientError | None = None
        self.put_requests: list[dict[str, object]] = []
        self.presign_requests: list[tuple[str, dict[str, object]]] = []
        self.pages: list[dict[str, object]] = []

    def get_object(self, **request: str) -> dict[str, Body]:
        key = request["Key"]
        if key in self.get_errors:
            raise self.get_errors.pop(key)
        return {"Body": Body(self.objects[key])}

    def put_object(self, **request: object) -> dict[str, object]:
        self.put_requests.append(request)
        if self.put_error is not None:
            error, self.put_error = self.put_error, None
            raise error
        self.objects[str(request["Key"])] = request["Body"]  # type: ignore[assignment]
        return {}

    def generate_presigned_url(self, operation: str, **request: object) -> str:
        self.presign_requests.append((operation, request))
        return f"https://bucket.amazonaws.com/{operation}"

    def get_paginator(self, name: str) -> FakeS3:
        assert name == "list_objects_v2"
        return self

    def paginate(self, **_request: object) -> list[dict[str, object]]:
        return self.pages


class FakeKms:
    def __init__(self) -> None:
        self.signature_valid = True
        self.signature: object = b"signature"
        self.verify_request: dict[str, object] | None = None
        self.sign_request: dict[str, object] | None = None
        self.generated = {"CiphertextBlob": b"encrypted", "Plaintext": b"p" * 32}
        self.decrypted = {"Plaintext": b"d" * 32}

    def verify(self, **request: object) -> dict[str, bool]:
        self.verify_request = request
        return {"SignatureValid": self.signature_valid}

    def sign(self, **request: object) -> dict[str, object]:
        self.sign_request = request
        return {"Signature": self.signature}

    def generate_data_key(self, **_request: object) -> dict[str, bytes]:
        return self.generated

    def decrypt(self, **_request: object) -> dict[str, bytes]:
        return self.decrypted


class FakeDynamo:
    def __init__(
        self,
        *,
        state: str = "READY",
        session: str = SESSION,
        init_expires: int | None = None,
    ) -> None:
        self.item = {"runtimeSessionId": {"S": session}, "state": {"S": state}}
        if init_expires is not None:
            self.item["initExpiresAt"] = {"N": str(init_expires)}
        self.get_request: dict[str, object] | None = None
        self.transactions: list[dict[str, object]] = []
        self.updates: list[dict[str, object]] = []
        self.update_error: ClientError | None = None
        # Set when the scripted failure should report the rejecting item back,
        # as ReturnValuesOnConditionCheckFailure=ALL_OLD does.
        self.rejecting_item: dict[str, dict[str, str]] | None = None

    def get_item(self, **request: object) -> dict[str, object]:
        self.get_request = request
        return {"Item": self.item}

    def update_item(self, **request: object) -> dict[str, object]:
        self.updates.append(request)
        # Mirror the DynamoDB validations that bit the first live deployment:
        # every declared placeholder must be used, and every used one declared.
        expressions = (
            f"{request.get('UpdateExpression', '')} {request.get('ConditionExpression', '')}"
        )
        declared = set(cast(dict[str, str], request.get("ExpressionAttributeNames", {})))
        used = set(re.findall(r"#[A-Za-z0-9_]+", expressions))
        if declared != used:
            raise client_error("ValidationException")
        declared_values = set(cast(dict[str, object], request.get("ExpressionAttributeValues", {})))
        used_values = set(re.findall(r":[A-Za-z0-9_]+", expressions))
        if declared_values != used_values:
            raise client_error("ValidationException")
        if self.update_error is not None:
            error, self.update_error = self.update_error, None
            if self.rejecting_item is not None and "ReturnValuesOnConditionCheckFailure" in request:
                error.response["Item"] = self.rejecting_item
            raise error
        return {}

    def transact_write_items(self, **request: object) -> dict[str, object]:
        self.transactions.append(request)
        return {}


@pytest.fixture(autouse=True)
def environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "BINDING_AUDIENCE": "audience",
        "BINDING_KEY_ARN": "arn:binding-key",
        "CHECKPOINT_BUCKET": "bucket",
        "KMS_KEY_ARN": "arn:snapshot-key",
        "SANDBOX_TABLE": "sandboxes",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def token(claim_changes: dict[str, object] | None = None) -> str:
    claims: dict[str, object] = {
        "aud": "audience",
        "exp": 2_000_000_000,
        "runtimeSessionId": SESSION,
        "sandboxId": SANDBOX,
        "subjectHash": SUBJECT_HASH,
        "type": "binding",
    }
    claims.update(claim_changes or {})
    payload = broker._base64url(json.dumps(claims).encode())
    return f"{payload}.{broker._base64url(b'signature')}"


def event(operation: str, **changes: object) -> dict[str, object]:
    return {
        "bindingToken": token(),
        "operation": operation,
        "runtimeSessionId": SESSION,
        "sandboxId": SANDBOX,
        **changes,
    }


def test_configuration_clients_and_base64_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert broker._required("SANDBOX_TABLE") == "sandboxes"
    monkeypatch.delenv("SANDBOX_TABLE")
    with pytest.raises(RuntimeError, match="Missing broker configuration"):
        broker._required("SANDBOX_TABLE")
    monkeypatch.setenv("SANDBOX_TABLE", "sandboxes")

    expected = (object(), object(), object())
    clients = list(reversed(expected))
    recorded: list[tuple[str, object]] = []

    def _client(name: str, *, config: object = None) -> object:
        recorded.append((name, config))
        return clients.pop()

    monkeypatch.setattr(vars(broker)["boto3"], "client", _client)
    assert broker._clients() == expected
    assert clients == []
    # The S3 client must be pinned to SigV4: a presigned PUT carries SSE-KMS as
    # signed x-amz-server-side-encryption* headers, and SigV2 leaves them out of
    # SignedHeaders, so S3 rejects the container's upload with
    # SignatureDoesNotMatch. botocore still defaults to SigV2 in regions that
    # predate SigV4-only enforcement, which makes the fault region-dependent.
    assert [name for name, _ in recorded] == ["s3", "kms", "dynamodb"]
    s3_config = recorded[0][1]
    assert getattr(s3_config, "signature_version", None) == "s3v4"
    assert [config for _, config in recorded[1:]] == [None, None]
    encoded = broker._base64url(b"payload")
    assert broker._unbase64url(encoded) == b"payload"


def test_binding_validation_success_and_failure_matrix() -> None:
    kms = FakeKms()
    claims = broker._binding(kms, token())
    assert claims["sandboxId"] == SANDBOX
    assert kms.verify_request is not None
    assert kms.verify_request["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"

    for value in ("bad", "e30.bad!", f"{broker._base64url(b'[]')}.c2ln"):
        with pytest.raises(PermissionError, match="Invalid runtime binding"):
            broker._binding(kms, value)

    assert broker._binding(kms, token({"type": "runtime-session"}))["type"] == "runtime-session"
    invalid_claims: list[dict[str, object]] = [
        {"type": "other"},
        {"type": "checkpoint-receipt"},
        {"aud": "other"},
        {"exp": "later"},
        {"exp": 1},
    ]
    for changes in invalid_claims:
        with pytest.raises(PermissionError, match="Invalid runtime binding"):
            broker._binding(kms, token(changes))
    kms.signature_valid = False
    with pytest.raises(PermissionError, match="Invalid runtime binding"):
        broker._binding(kms, token())


def test_authorization_requires_bound_active_sandbox() -> None:
    claims = broker._binding(FakeKms(), token())
    dynamo = FakeDynamo()
    assert broker._authorize(dynamo, claims, event("dataKey")) == SANDBOX
    assert dynamo.get_request is not None
    assert dynamo.get_request["ConsistentRead"] is True

    invalid_events: list[dict[str, object]] = [
        {"sandboxId": 1, "runtimeSessionId": SESSION},
        {"sandboxId": "bad", "runtimeSessionId": SESSION},
        {"sandboxId": SANDBOX, "runtimeSessionId": "other"},
    ]
    for invalid in invalid_events:
        with pytest.raises(PermissionError, match="Invalid runtime binding"):
            broker._authorize(dynamo, claims, invalid)
    with pytest.raises(PermissionError, match="Invalid runtime binding"):
        broker._authorize(FakeDynamo(session="other"), claims, event("dataKey"))
    with pytest.raises(PermissionError, match="unavailable"):
        broker._authorize(FakeDynamo(state="STOPPED"), claims, event("dataKey"))
    # A live initialization lease keeps storage access even when state churn
    # (a concurrent reset or error) removed the sandbox from the allowed set.
    future = int(datetime.now(UTC).timestamp()) + 60
    live_init = FakeDynamo(state="ERROR", init_expires=future)
    assert broker._authorize(live_init, claims, event("dataKey")) == SANDBOX
    stale_init = FakeDynamo(state="ERROR", init_expires=1)
    with pytest.raises(PermissionError, match="unavailable"):
        broker._authorize(stale_init, claims, event("dataKey"))


def test_data_key_create_load_race_and_validation() -> None:
    key_name = f"sandboxes/{SANDBOX}/data-key.json"
    s3 = FakeS3()
    s3.get_errors[key_name] = client_error("NoSuchKey")
    kms = FakeKms()
    assert broker._data_key(s3, kms, SANDBOX) == {
        "plaintextKey": base64.b64encode(b"p" * 32).decode()
    }
    assert s3.put_requests[0]["IfNoneMatch"] == "*"

    stored = json.dumps({"ciphertext": base64.b64encode(b"wrapped").decode()}).encode()
    existing = FakeS3()
    existing.objects[key_name] = stored
    assert broker._data_key(existing, kms, SANDBOX) == {
        "plaintextKey": base64.b64encode(b"d" * 32).decode()
    }

    raced = FakeS3()
    raced.get_errors[key_name] = client_error("404")
    raced.put_error = client_error("PreconditionFailed")
    raced.objects[key_name] = stored
    assert broker._data_key(raced, kms, SANDBOX)["plaintextKey"]

    for code in ("AccessDenied",):
        failed = FakeS3()
        failed.get_errors[key_name] = client_error(code)
        with pytest.raises(ClientError):
            broker._data_key(failed, kms, SANDBOX)
    failed_put = FakeS3()
    failed_put.get_errors[key_name] = client_error("NoSuchKey")
    failed_put.put_error = client_error("AccessDenied")
    with pytest.raises(ClientError):
        broker._data_key(failed_put, kms, SANDBOX)
    invalid = FakeS3()
    invalid.objects[key_name] = b"[]"
    with pytest.raises(RuntimeError, match="Stored data key is invalid"):
        broker._data_key(invalid, kms, SANDBOX)


def test_object_names_presigning_and_listing() -> None:
    assert broker._object_name("chunks", f"{DIGEST}.bin") == f"chunks/{DIGEST}.bin"
    assert broker._object_name("manifests", "2.json.enc") == "manifests/2.json.enc"
    assert broker._object_name("commits", "2.json") == "commits/2.json"
    for category, name in (("bad", "x"), ("chunks", "bad.bin"), ("manifests", "0.json.enc")):
        with pytest.raises(ValueError, match="Invalid checkpoint object name"):
            broker._object_name(category, name)

    s3 = FakeS3()
    get = broker._presign(
        s3,
        SANDBOX,
        {"category": "commits", "name": "2.json", "method": "GET"},
    )
    assert get["headers"] == {}
    put = broker._presign(
        s3,
        SANDBOX,
        {"category": "chunks", "name": f"{DIGEST}.bin", "method": "PUT"},
    )
    assert put["headers"]["x-amz-server-side-encryption"] == "aws:kms"  # type: ignore[index]
    assert s3.presign_requests[-1][0] == "put_object"
    head = broker._presign(
        s3,
        SANDBOX,
        {"category": "commits", "name": "2.json", "method": "HEAD"},
    )
    assert head["method"] == "HEAD"
    for invalid in ({}, {"category": "commits", "name": "2.json", "method": "DELETE"}):
        with pytest.raises(ValueError, match="Invalid checkpoint operation"):
            broker._presign(s3, SANDBOX, invalid)

    prefix = f"sandboxes/{SANDBOX}/commits/"
    s3.pages = [
        {"Contents": [{"Key": f"{prefix}2.json"}, {"Key": 1}, {"Key": "other"}]},
        {},
    ]
    assert broker._list(s3, SANDBOX, {"category": "commits"}) == {"names": ["2.json"]}
    with pytest.raises(ValueError, match="Invalid checkpoint category"):
        broker._list(s3, SANDBOX, {"category": "bad"})


def test_checkpoint_receipt_validates_commit_catalogs_and_signs() -> None:
    s3, kms, dynamo = FakeS3(), FakeKms(), FakeDynamo()
    key = f"sandboxes/{SANDBOX}/commits/3.json"
    s3.objects[key] = json.dumps(
        {"committedAt": "2026-08-18T08:00:00Z", "generation": 3, "manifestDigest": DIGEST}
    ).encode()
    claims = broker._binding(kms, token())
    receipt = broker._checkpoint_receipt(
        s3,
        kms,
        dynamo,
        SANDBOX,
        claims,
        {"generation": 3, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
    )
    assert receipt["generation"] == 3
    assert str(receipt["checkpointReceipt"]).count(".") == 1
    assert dynamo.transactions
    assert kms.sign_request is not None
    final_update = dynamo.transactions[0]["TransactItems"][0]["Update"]  # type: ignore[index]
    assert "#state = :stopping" in final_update["UpdateExpression"]

    # A mid-session durability checkpoint records the generation pointer
    # without pushing the sandbox state machine toward STOPPING.
    non_final = broker._checkpoint_receipt(
        s3,
        kms,
        dynamo,
        SANDBOX,
        claims,
        {"final": False, "generation": 3, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
    )
    assert non_final["generation"] == 3
    session_update = dynamo.transactions[-1]["TransactItems"][0]["Update"]  # type: ignore[index]
    assert ":stopping" not in session_update["UpdateExpression"]
    assert ":stopping" not in session_update["ConditionExpression"]
    assert ":busy" in session_update["ConditionExpression"]

    invalid_requests = [
        {},
        {"generation": 0, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
        {"generation": 1, "manifestDigest": "bad", "runtimeSessionId": SESSION},
        {"final": "yes", "generation": 3, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
    ]
    for invalid in invalid_requests:
        with pytest.raises(ValueError, match="Invalid checkpoint receipt request"):
            broker._checkpoint_receipt(s3, kms, dynamo, SANDBOX, claims, invalid)

    for value, message in [
        (b"not-json", "metadata is invalid"),
        (json.dumps([]).encode(), "does not match"),
        (json.dumps({"generation": 3, "manifestDigest": "c" * 64}).encode(), "does not match"),
        (json.dumps({"generation": 3, "manifestDigest": DIGEST}).encode(), "timestamp is invalid"),
    ]:
        s3.objects[key] = value
        with pytest.raises(ValueError, match=message):
            broker._checkpoint_receipt(
                s3,
                kms,
                dynamo,
                SANDBOX,
                claims,
                {"generation": 3, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
            )
    s3.objects[key] = json.dumps(
        {"committedAt": "2026-08-18T08:00:00Z", "generation": 3, "manifestDigest": DIGEST}
    ).encode()
    kms.signature = "not-bytes"
    with pytest.raises(RuntimeError, match="signature is unavailable"):
        broker._checkpoint_receipt(
            s3,
            kms,
            dynamo,
            SANDBOX,
            claims,
            {"generation": 3, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
        )


def test_handler_dispatch_and_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert broker.handler({"operation": "audit"}, object()) == {"status": "scheduled"}
    s3, kms, dynamo = FakeS3(), FakeKms(), FakeDynamo()
    monkeypatch.setattr(broker, "_clients", lambda: (s3, kms, dynamo))
    with pytest.raises(PermissionError, match="binding is required"):
        broker.handler({"operation": "dataKey"}, object())

    monkeypatch.setattr(broker, "_binding", lambda _kms, _token: {"ok": True})
    monkeypatch.setattr(broker, "_authorize", lambda _ddb, _claims, _event: SANDBOX)
    monkeypatch.setattr(broker, "_data_key", lambda *_args: {"data": True})
    monkeypatch.setattr(broker, "_presign", lambda *_args: {"presign": True})
    monkeypatch.setattr(broker, "_list", lambda *_args: {"list": True})
    monkeypatch.setattr(broker, "_checkpoint_receipt", lambda *_args: {"receipt": True})
    monkeypatch.setattr(broker, "_claimed_sandbox", lambda _claims, _event: SANDBOX)
    monkeypatch.setattr(broker, "_read_record", lambda _ddb, _sandbox: {"record": True})
    monkeypatch.setattr(broker, "_acquire_init", lambda *_args: {"acquire": True})
    monkeypatch.setattr(broker, "_heartbeat_init", lambda *_args: {"init": True})
    monkeypatch.setattr(broker, "_heartbeat_lease", lambda *_args: {"lease": True})
    monkeypatch.setattr(broker, "_heal_ready", lambda *_args: {"heal": True})
    monkeypatch.setattr(broker, "_mark_ready", lambda *_args: {"ready": True})
    monkeypatch.setattr(broker, "_mark_error", lambda *_args: {"error": True})
    assert broker.handler(event("readRecord"), object()) == {"record": True}
    assert broker.handler(event("lease"), object()) == {"authorized": True}
    assert broker.handler(event("acquireInit"), object()) == {"acquire": True}
    assert broker.handler(event("heartbeatInit"), object()) == {"init": True}
    assert broker.handler(event("heartbeatLease"), object()) == {"lease": True}
    assert broker.handler(event("healReady"), object()) == {"heal": True}
    assert broker.handler(event("markReady"), object()) == {"ready": True}
    assert broker.handler(event("markError"), object()) == {"error": True}
    assert broker.handler(event("dataKey"), object()) == {"data": True}
    assert broker.handler(event("presign"), object()) == {"presign": True}
    assert broker.handler(event("list"), object()) == {"list": True}
    assert broker.handler(event("checkpointReceipt"), object()) == {"receipt": True}
    with pytest.raises(ValueError, match="Unsupported"):
        broker.handler(event("unknown"), object())


OWNER = "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"


def test_read_record_needs_only_a_matching_claim_and_reports_lifecycle_fields() -> None:
    claims = broker._binding(FakeKms(), token())
    assert broker._claimed_sandbox(claims, event("readRecord")) == SANDBOX
    with pytest.raises(PermissionError, match="Invalid runtime binding"):
        broker._claimed_sandbox(claims, event("readRecord", sandboxId="sbx_" + "A" * 26))

    dynamo = FakeDynamo(state="STARTING", session="rotated")
    dynamo.item["lastCheckpointGeneration"] = {"N": "7"}
    # The record may name a different session: the runtime compares and walks away.
    assert broker._read_record(dynamo, SANDBOX) == {
        "lastCheckpointGeneration": 7,
        "runtimeSessionId": "rotated",
        "state": "STARTING",
    }
    assert dynamo.get_request is not None
    assert dynamo.get_request["ConsistentRead"] is True
    empty = FakeDynamo()
    empty.item = {}
    assert broker._read_record(empty, SANDBOX) == {
        "lastCheckpointGeneration": None,
        "runtimeSessionId": None,
        "state": None,
    }


def test_conditional_updates_report_rejected_conditions_and_raise_other_failures() -> None:
    dynamo = FakeDynamo()
    values = {":a": {"S": "x"}, ":b": {"S": "y"}}
    assert broker._conditional_update(dynamo, SANDBOX, "SET #state = :a", "b = :b", values) == {
        "applied": True
    }
    update = dynamo.updates[0]
    assert update["Key"] == {"pk": {"S": f"SANDBOX#{SANDBOX}"}, "sk": {"S": "METADATA"}}
    assert update["ExpressionAttributeNames"] == {"#state": "state"}
    # An update that never mentions the state field must not declare the alias:
    # DynamoDB rejects unused placeholders.
    assert broker._conditional_update(dynamo, SANDBOX, "SET a = :a", "b = :b", values) == {
        "applied": True
    }
    assert "ExpressionAttributeNames" not in dynamo.updates[1]
    dynamo.update_error = client_error("ConditionalCheckFailedException")
    assert broker._conditional_update(dynamo, SANDBOX, "SET a = :a", "b = :b", values) == {
        "applied": False
    }
    dynamo.update_error = client_error("ProvisionedThroughputExceededException")
    with pytest.raises(ClientError):
        broker._conditional_update(dynamo, SANDBOX, "SET a = :a", "b = :b", values)


def test_acquire_init_mints_a_runtime_session_token_only_when_the_claim_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kms = FakeKms()
    claims = broker._binding(kms, token())
    dynamo = FakeDynamo(state="STARTING")
    monkeypatch.setenv("RUNTIME_SESSION_TOKEN_TTL_SECONDS", "3600")
    result = broker._acquire_init(
        kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner=OWNER)
    )
    assert result["applied"] is True
    session_token = result["runtimeSessionToken"]
    assert isinstance(session_token, str)
    payload, signature = session_token.split(".")
    assert broker._unbase64url(signature) == b"signature"
    minted = json.loads(broker._unbase64url(payload))
    assert minted["type"] == "runtime-session"
    assert minted["sandboxId"] == SANDBOX
    assert minted["runtimeSessionId"] == SESSION
    assert minted["subjectHash"] == SUBJECT_HASH
    assert minted["aud"] == "audience"
    assert minted["exp"] - minted["iat"] == 3600
    assert kms.sign_request is not None
    assert kms.sign_request["SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
    update = dynamo.updates[0]
    assert "initOwner = :owner OR initExpiresAt < :now" in str(update["ConditionExpression"])
    values = update["ExpressionAttributeValues"]
    assert values[":owner"] == {"S": OWNER}  # type: ignore[index]
    assert values[":restoring"] == {"S": "RESTORING"}  # type: ignore[index]

    dynamo.update_error = client_error("ConditionalCheckFailedException")
    refused = broker._acquire_init(
        kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner=OWNER)
    )
    assert refused["applied"] is False
    assert "could not be claimed" in str(refused["reason"])
    with pytest.raises(ValueError, match="Invalid initialization owner"):
        broker._acquire_init(kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner="me"))
    kms.signature = "not-bytes"
    with pytest.raises(RuntimeError, match="signature is unavailable"):
        broker._acquire_init(kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner=OWNER))
    monkeypatch.delenv("RUNTIME_SESSION_TOKEN_TTL_SECONDS")
    assert broker._runtime_session_ttl().total_seconds() == 28800


def test_lifecycle_updates_are_server_defined_and_validate_their_inputs() -> None:
    claims = broker._binding(FakeKms(), token())
    dynamo = FakeDynamo()

    assert broker._heartbeat_init(dynamo, SANDBOX, {"initOwner": OWNER}) == {"applied": True}
    assert dynamo.updates[-1]["ConditionExpression"] == "initOwner = :owner"
    assert "initExpiresAt" in str(dynamo.updates[-1]["UpdateExpression"])

    assert broker._heartbeat_lease(dynamo, SANDBOX, claims) == {"applied": True}
    assert dynamo.updates[-1]["ConditionExpression"] == "leaseOwner = :owner"
    lease_values = dynamo.updates[-1]["ExpressionAttributeValues"]
    assert lease_values[":owner"] == {"S": SESSION}  # type: ignore[index]

    assert broker._heal_ready(dynamo, SANDBOX, claims) == {"applied": True}
    heal_values = dynamo.updates[-1]["ExpressionAttributeValues"]
    assert heal_values[":ready"] == {"S": "READY"}  # type: ignore[index]
    assert heal_values[":starting"] == {"S": "STARTING"}  # type: ignore[index]
    assert "#state IN (:starting)" in str(dynamo.updates[-1]["ConditionExpression"])

    ready = broker._mark_ready(
        dynamo, SANDBOX, claims, {"initOwner": OWNER, "restoreOutcome": "restored"}
    )
    assert ready == {"applied": True}
    ready_update = dynamo.updates[-1]
    assert (
        ready_update["ConditionExpression"] == "runtimeSessionId = :session AND initOwner = :owner"
    )
    assert "REMOVE initOwner, initExpiresAt" in str(ready_update["UpdateExpression"])
    assert ready_update["ExpressionAttributeValues"][":restore"] == {"S": "RESTORED"}  # type: ignore[index]
    for outcome in ("", "Restored!", 3, "x" * 40):
        with pytest.raises(ValueError, match="Invalid restore outcome"):
            broker._mark_ready(
                dynamo, SANDBOX, claims, {"initOwner": OWNER, "restoreOutcome": outcome}
            )

    owned = broker._mark_error(
        dynamo,
        SANDBOX,
        claims,
        {
            "failureDetail": "GATEWAY_EXITED",
            "failureType": "GatewayExitedError",
            "initOwner": OWNER,
            "upstreamException": "OSError",
        },
    )
    assert owned == {"applied": True}
    owned_update = dynamo.updates[-1]
    assert (
        owned_update["ConditionExpression"] == "runtimeSessionId = :session AND initOwner = :owner"
    )
    owned_values = owned_update["ExpressionAttributeValues"]
    assert owned_values[":failure"] == {"S": "GatewayExitedError"}  # type: ignore[index]
    assert owned_values[":detail"] == {"S": "GATEWAY_EXITED"}  # type: ignore[index]
    assert owned_values[":upstream"] == {"S": "OSError"}  # type: ignore[index]
    assert ":starting" not in owned_values  # type: ignore[operator]

    unowned = broker._mark_error(dynamo, SANDBOX, claims, {})
    assert unowned == {"applied": True}
    unowned_update = dynamo.updates[-1]
    assert ":starting, :restoring" in str(unowned_update["ConditionExpression"])
    assert unowned_update["ExpressionAttributeValues"][":failure"] == {"S": "UNKNOWN"}  # type: ignore[index]
    with pytest.raises(ValueError, match="Invalid initialization diagnostic"):
        broker._mark_error(dynamo, SANDBOX, claims, {"failureType": "bad\nline"})
    with pytest.raises(ValueError, match="Invalid initialization diagnostic"):
        broker._mark_error(dynamo, SANDBOX, claims, {"failureDetail": 7})
    with pytest.raises(ValueError, match="Invalid initialization owner"):
        broker._mark_error(dynamo, SANDBOX, claims, {"initOwner": "owner"})


def test_acquire_init_reclaims_a_record_left_ready_once_its_lease_is_dead() -> None:
    """An idle-reclaimed sandbox must not self-lock at READY.

    The container that was reclaimed publishes nothing, so without this the
    record stays READY, every later claim fails the state clause, and Stop -
    which travels the same path - cannot clear it either.
    """
    kms = FakeKms()
    claims = broker._binding(kms, token())
    dynamo = FakeDynamo(state="READY")
    result = broker._acquire_init(
        kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner=OWNER)
    )
    assert result["applied"] is True
    assert isinstance(result["runtimeSessionToken"], str)
    condition = str(dynamo.updates[-1]["ConditionExpression"])
    values = dynamo.updates[-1]["ExpressionAttributeValues"]
    assert "#state = :ready" in condition
    assert "attribute_not_exists(leaseExpiresAt) OR leaseExpiresAt < :deadline" in condition
    assert values[":ready"] == {"S": "READY"}  # type: ignore[index]
    # The deadline is now, so a lease that has not expired keeps the claim out.
    assert dynamo.updates[-1]["ReturnValuesOnConditionCheckFailure"] == "ALL_OLD"


def test_acquire_init_names_the_clause_that_rejected_the_claim() -> None:
    kms = FakeKms()
    claims = broker._binding(kms, token())
    dynamo = FakeDynamo(state="READY")
    live_lease = "2999-01-01T00:00:00Z"
    cases: list[tuple[dict[str, dict[str, str]], str]] = [
        ({}, "could not be claimed"),
        (
            {
                "initOwner": {"S": "8f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"},
                "initExpiresAt": {"N": "9999999999"},
            },
            "Another container owns this sandbox initialization",
        ),
        (
            {"state": {"S": "READY"}, "leaseExpiresAt": {"S": live_lease}},
            "A live container still holds this sandbox",
        ),
        ({"state": {"S": "CHECKPOINTING"}}, "not claimable from state CHECKPOINTING"),
        ({"updatedAt": {"S": "2026-09-11T00:00:00Z"}}, "could not be claimed"),
        # A stale init lease does not name a competing owner: the state clause
        # is what actually rejected the claim.
        (
            {
                "initOwner": {"S": "8f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"},
                "initExpiresAt": {"N": "1"},
                "state": {"S": "BUSY"},
            },
            "not claimable from state BUSY",
        ),
    ]
    for item, expected in cases:
        dynamo.update_error = client_error("ConditionalCheckFailedException")
        dynamo.rejecting_item = item
        result = broker._acquire_init(
            kms, dynamo, SANDBOX, claims, event("acquireInit", initOwner=OWNER)
        )
        assert result["applied"] is False
        assert "runtimeSessionToken" not in result
        assert expected in str(result["reason"]), (item, result["reason"])

    # Without a reason builder a rejection stays a plain conditional failure.
    dynamo.update_error = client_error("ConditionalCheckFailedException")
    dynamo.rejecting_item = {"state": {"S": "READY"}}
    plain = broker._conditional_update(
        dynamo, SANDBOX, "SET a = :a", "b = :b", {":a": {"S": "x"}, ":b": {"S": "y"}}
    )
    assert plain == {"applied": False}
    assert "ReturnValuesOnConditionCheckFailure" not in dynamo.updates[-1]
