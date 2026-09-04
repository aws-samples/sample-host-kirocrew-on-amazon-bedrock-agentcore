from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

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

    def get_item(self, **request: object) -> dict[str, object]:
        self.get_request = request
        return {"Item": self.item}

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
    monkeypatch.setattr(vars(broker)["boto3"], "client", lambda _name: clients.pop())
    assert broker._clients() == expected
    assert clients == []
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

    invalid_claims: list[dict[str, object]] = [
        {"type": "other"},
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

    invalid_requests = [
        {},
        {"generation": 0, "manifestDigest": DIGEST, "runtimeSessionId": SESSION},
        {"generation": 1, "manifestDigest": "bad", "runtimeSessionId": SESSION},
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
    assert broker.handler(event("dataKey"), object()) == {"data": True}
    assert broker.handler(event("presign"), object()) == {"presign": True}
    assert broker.handler(event("list"), object()) == {"list": True}
    assert broker.handler(event("checkpointReceipt"), object()) == {"receipt": True}
    with pytest.raises(ValueError, match="Unsupported"):
        broker.handler(event("unknown"), object())
