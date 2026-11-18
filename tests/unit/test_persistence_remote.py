from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

import pytest
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    BrokerRefusalError,
    PresignedOperation,
    StorageOperation,
)
from kirocrew_agentcore_persistence.remote import LambdaBrokerClient, LambdaPersistenceBroker

SANDBOX = "sbx_01J00000000000000000000000"
SESSION = "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4"
CHUNK_DIGEST = "b" * 64


class Payload:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self) -> bytes:
        if isinstance(self.value, str):
            return self.value.encode()
        return json.dumps(self.value).encode()


class FakeLambda:
    def __init__(self, value: object, *, function_error: bool = False) -> None:
        self.value = value
        self.function_error = function_error
        self.requests: list[dict[str, object]] = []

    def invoke(self, **request: object) -> dict[str, object]:
        self.requests.append(request)
        return {
            "Payload": Payload(self.value),
            **({"FunctionError": "Unhandled"} if self.function_error else {}),
        }


def client(
    value: object = None, *, function_error: bool = False
) -> tuple[LambdaBrokerClient, FakeLambda]:
    fake = FakeLambda({} if value is None else value, function_error=function_error)
    return LambdaBrokerClient(fake, "arn:broker", SANDBOX, SESSION, "binding"), fake


def test_lambda_client_validates_binding_calls_and_receipts() -> None:
    with pytest.raises(ValueError, match="Complete persistence broker binding"):
        LambdaBrokerClient(object(), "", SANDBOX, SESSION, "binding")

    broker_client, fake = client({"checkpointReceipt": "receipt"})
    assert broker_client.checkpoint_receipt(3, "a" * 64) == "receipt"
    request = json.loads(cast(bytes, fake.requests[0]["Payload"]))
    assert request == {
        "bindingToken": "binding",
        "final": True,
        "generation": 3,
        "manifestDigest": "a" * 64,
        "operation": "checkpointReceipt",
        "runtimeSessionId": SESSION,
        "sandboxId": SANDBOX,
    }
    assert broker_client.checkpoint_receipt(4, "b" * 64, final=False) == "receipt"
    session_request = json.loads(cast(bytes, fake.requests[1]["Payload"]))
    assert session_request["final"] is False
    # Until a restore sets it the client sends no base, then each committed
    # receipt becomes the base the next one must descend from.
    assert broker_client.base_generation == 4
    assert broker_client.checkpoint_receipt(5, "c" * 64) == "receipt"
    based = json.loads(cast(bytes, fake.requests[2]["Payload"]))
    assert based["baseGeneration"] == 4
    assert broker_client.base_generation == 5
    # Wake hints ride along once the runtime sets them.
    assert "nextDueAt" not in based and "restoreSeconds" not in based
    broker_client.next_due_at, broker_client.next_due_job = 1_800_000_000, "job1"
    broker_client.restore_seconds = 42
    broker_client.checkpoint_receipt(6, "d" * 64)
    hinted = json.loads(cast(bytes, fake.requests[3]["Payload"]))
    assert hinted["nextDueAt"] == 1_800_000_000
    assert hinted["nextDueJob"] == "job1"
    assert hinted["restoreSeconds"] == 42
    assert "holderInstance" not in hinted
    broker_client.holder_instance = "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"
    broker_client.checkpoint_receipt(7, "e" * 64)
    fenced = json.loads(cast(bytes, fake.requests[4]["Payload"]))
    assert fenced["holderInstance"] == "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"

    for value in ({}, {"checkpointReceipt": 1}):
        broken, _ = client(value)
        with pytest.raises(BrokerAuthorizationError, match="receipt is unavailable"):
            broken.checkpoint_receipt(1, "a" * 64)

    failed, _ = client({"errorMessage": "boom", "errorType": "ClientError"}, function_error=True)
    with pytest.raises(BrokerAuthorizationError, match="rejected") as other:
        failed.call("dataKey")
    assert not isinstance(other.value, BrokerRefusalError)
    refused, _ = client(
        {"errorMessage": "Invalid runtime binding.", "errorType": "PermissionError"},
        function_error=True,
    )
    with pytest.raises(BrokerRefusalError, match="refused"):
        refused.call("dataKey")
    for unreadable in ("not json", ["list"], {"errorType": 7}):
        unparsed, _ = client(unreadable, function_error=True)
        with pytest.raises(BrokerAuthorizationError, match="rejected") as generic:
            unparsed.call("dataKey")
        assert not isinstance(generic.value, BrokerRefusalError)
    invalid, _ = client(["not", "an", "object"])
    with pytest.raises(BrokerAuthorizationError, match="response is invalid"):
        invalid.call("dataKey")


def test_remote_broker_cipher_context_scope_and_listing() -> None:
    broker_client, _ = client({"plaintextKey": base64.b64encode(b"k" * 32).decode()})
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    cipher = broker.cipher(SANDBOX)
    assert cipher.decrypt(cipher.encrypt(b"payload")) == b"payload"
    assert broker.encryption_context(SANDBOX) == {
        "application": "kirocrew-agentcore",
        "purpose": "sandbox-checkpoint",
        "sandboxId": SANDBOX,
    }
    with pytest.raises(BrokerAuthorizationError, match="Cross-sandbox"):
        broker.encryption_context("other")

    for value, message in [
        ({}, "data key is unavailable"),
        ({"plaintextKey": "not-base64!"}, "data key is invalid"),
    ]:
        invalid_client, _ = client(value)
        with pytest.raises(BrokerAuthorizationError, match=message):
            LambdaPersistenceBroker(invalid_client, SANDBOX).cipher(SANDBOX)

    listing_client, _ = client({"names": ["2.json", "1.json"]})
    listing = LambdaPersistenceBroker(listing_client, SANDBOX)
    assert listing.internal_keys(SANDBOX, "commits") == (
        f"sandboxes/{SANDBOX}/commits/2.json",
        f"sandboxes/{SANDBOX}/commits/1.json",
    )
    for invalid_value in ({}, {"names": [1]}):
        invalid_client, _ = client(invalid_value)
        with pytest.raises(BrokerAuthorizationError, match="listing is invalid"):
            LambdaPersistenceBroker(invalid_client, SANDBOX).internal_keys(SANDBOX, "commits")

    # Existence is probed with a narrowed listing, not a presigned HeadObject: S3
    # answers a permitted listing with an empty result for a missing key, where
    # HeadObject answers 403 because it carries no `s3:prefix` context key for the
    # broker's prefix-conditioned grant to match.
    present_client, present_fake = client({"names": [f"{CHUNK_DIGEST}.bin"]})
    assert (
        LambdaPersistenceBroker(present_client, SANDBOX).chunk_exists(SANDBOX, CHUNK_DIGEST) is True
    )
    probe = json.loads(cast(bytes, present_fake.requests[-1]["Payload"]))
    assert probe["category"] == "chunks"
    assert probe["name"] == f"{CHUNK_DIGEST}.bin"
    absent_client, _ = client({"names": []})
    assert (
        LambdaPersistenceBroker(absent_client, SANDBOX).chunk_exists(SANDBOX, CHUNK_DIGEST) is False
    )
    for invalid_value in ({}, {"names": [1]}):
        invalid_client, _ = client(invalid_value)
        with pytest.raises(BrokerAuthorizationError, match="listing is invalid"):
            LambdaPersistenceBroker(invalid_client, SANDBOX).chunk_exists(SANDBOX, CHUNK_DIGEST)
    with pytest.raises(BrokerAuthorizationError, match="Cross-sandbox"):
        LambdaPersistenceBroker(present_client, SANDBOX).chunk_exists("other", CHUNK_DIGEST)
    with pytest.raises(BrokerAuthorizationError, match="deletion is not permitted"):
        broker.internal_delete(SANDBOX, "commits", "1.json")


def test_presign_variants_and_validation() -> None:
    expires = "2026-08-18T10:00:00Z"
    value = {
        "url": "https://bucket.s3.us-east-2.amazonaws.com/object",
        "expiresAt": expires,
        "headers": {"x-amz-test": "value"},
    }
    broker_client, fake = client(value)
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    assert (
        broker.presign_chunk(SANDBOX, "a" * 64, StorageOperation.PUT).method is StorageOperation.PUT
    )
    assert broker.presign_manifest(SANDBOX, 2, StorageOperation.GET).expires_at == datetime(
        2026, 8, 18, 10, tzinfo=UTC
    )
    assert broker.presign_commit(SANDBOX, 3, StorageOperation.HEAD).headers == {
        "x-amz-test": "value"
    }
    payloads = [json.loads(cast(bytes, request["Payload"])) for request in fake.requests]
    assert [(item["category"], item["name"], item["method"]) for item in payloads] == [
        ("chunks", f"{'a' * 64}.bin", "PUT"),
        ("manifests", "2.json.enc", "GET"),
        ("commits", "3.json", "HEAD"),
    ]

    invalid_values = [
        ({}, "grant is invalid"),
        ({"url": 1, "expiresAt": expires, "headers": {}}, "grant is invalid"),
        ({"url": "https://x", "expiresAt": "bad", "headers": {}}, "expiry is invalid"),
        (
            {"url": "https://x", "expiresAt": expires, "headers": {"x": 1}},
            "headers are invalid",
        ),
    ]
    for response, message in invalid_values:
        invalid_client, _ = client(response)
        with pytest.raises(BrokerAuthorizationError, match=message):
            LambdaPersistenceBroker(invalid_client, SANDBOX).presign_commit(
                SANDBOX, 1, StorageOperation.GET
            )


class FakeResponse:
    def __init__(self, status: int, body: bytes, *, will_close: bool = False) -> None:
        self.status = status
        self.body = body
        self.will_close = will_close

    def read(self) -> bytes:
        return self.body


class FakeConnection:
    """Stands in for http.client.HTTPSConnection, answering from a shared script."""

    made: ClassVar[list[FakeConnection]] = []
    script: ClassVar[list[object]] = []

    def __init__(self, host: str, port: int | None, *, timeout: int, context: object) -> None:
        del context
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sent: list[tuple[str, str, bytes | None, dict[str, str]]] = []
        self.closed = False
        FakeConnection.made.append(self)

    def request(
        self, method: str, target: str, *, body: bytes | None, headers: dict[str, str]
    ) -> None:
        self.sent.append((method, target, body, headers))
        step = FakeConnection.script[0]
        if isinstance(step, BaseException):
            FakeConnection.script.pop(0)
            raise step

    def getresponse(self) -> FakeResponse:
        return cast(FakeResponse, FakeConnection.script.pop(0))

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_https(monkeypatch: pytest.MonkeyPatch) -> type[FakeConnection]:
    import http.client
    import threading

    import kirocrew_agentcore_persistence.remote as remote

    FakeConnection.made = []
    FakeConnection.script = []
    monkeypatch.setattr(http.client, "HTTPSConnection", FakeConnection)
    monkeypatch.setattr(remote, "_CONNECTIONS", threading.local())
    return FakeConnection


def test_https_request_reuses_one_connection_per_host(fake_https: type[FakeConnection]) -> None:
    broker_client, _ = client()
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    expires = datetime(2026, 8, 18, tzinfo=UTC)
    get = PresignedOperation(
        "https://bucket.s3.us-east-2.amazonaws.com/object?X-Amz-Signature=s",
        StorageOperation.GET,
        expires,
        {"x-header": "value"},
    )
    head = PresignedOperation(
        "https://bucket.s3.us-east-2.amazonaws.com", StorageOperation.HEAD, expires, {}
    )
    put = PresignedOperation(get.url, StorageOperation.PUT, expires, {})
    fake_https.script = [
        FakeResponse(200, b"body"),
        FakeResponse(200, b""),
        FakeResponse(404, b""),
        FakeResponse(204, b""),
    ]

    assert broker.request(get, StorageOperation.GET) == b"body"
    assert broker.request(head, StorageOperation.HEAD) is True
    assert broker.request(head, StorageOperation.HEAD) is False
    assert broker.request(put, StorageOperation.PUT, body=b"data") == b""

    [connection] = fake_https.made
    assert (connection.host, connection.port, connection.timeout) == (
        "bucket.s3.us-east-2.amazonaws.com",
        None,
        30,
    )
    assert connection.sent[0] == (
        "GET",
        "/object?X-Amz-Signature=s",
        None,
        {"x-header": "value"},
    )
    assert connection.sent[1][:2] == ("HEAD", "/")
    assert connection.sent[3][:3] == ("PUT", "/object?X-Amz-Signature=s", b"data")


def test_https_request_retries_once_on_a_dropped_connection(
    fake_https: type[FakeConnection],
) -> None:
    broker_client, _ = client()
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    expires = datetime(2026, 8, 18, tzinfo=UTC)
    get = PresignedOperation(
        "https://bucket.s3.cn-north-1.amazonaws.com.cn:443/key", StorageOperation.GET, expires, {}
    )

    # An idle connection the server closed fails once; a fresh one succeeds.
    fake_https.script = [ConnectionResetError(), FakeResponse(200, b"ok", will_close=True)]
    assert broker.request(get, StorageOperation.GET) == b"ok"
    assert len(fake_https.made) == 2
    assert fake_https.made[0].closed and fake_https.made[1].closed

    # Two failures in a row surface as a broker error.
    fake_https.script = [OSError("down"), OSError("still down")]
    with pytest.raises(BrokerAuthorizationError, match="operation failed"):
        broker.request(get, StorageOperation.GET)


def test_https_request_rejects_bad_grants_and_statuses(fake_https: type[FakeConnection]) -> None:
    broker_client, _ = client()
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    expires = datetime(2026, 8, 18, tzinfo=UTC)
    get = PresignedOperation(
        "https://bucket.s3.us-east-2.amazonaws.com/object", StorageOperation.GET, expires, {}
    )
    head = PresignedOperation(get.url, StorageOperation.HEAD, expires, {})

    with pytest.raises(BrokerAuthorizationError, match="does not match"):
        broker.request(get, StorageOperation.PUT)
    for url in ("http://bucket.amazonaws.com/key", "https://example.com/key", "https:///key"):
        grant = PresignedOperation(url, StorageOperation.GET, expires, {})
        with pytest.raises(BrokerAuthorizationError, match="not trusted"):
            broker.request(grant, StorageOperation.GET)
    assert fake_https.made == []

    fake_https.script = [FakeResponse(404, b""), FakeResponse(403, b"")]
    with pytest.raises(BrokerAuthorizationError, match="operation failed"):
        broker.request(get, StorageOperation.GET)
    with pytest.raises(BrokerAuthorizationError, match="operation failed"):
        broker.request(head, StorageOperation.HEAD)


class ScriptedLambda:
    """Answers each broker operation from a handler, recording every request."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.requests: list[dict[str, object]] = []

    def invoke(self, **request: object) -> dict[str, object]:
        body = json.loads(cast(bytes, request["Payload"]))
        self.requests.append(body)
        value, error = self.answer(body)
        return {"Payload": Payload(value), **({"FunctionError": "Unhandled"} if error else {})}


def _grant(name: str, expires: str = "2026-08-18T10:00:00Z") -> dict[str, object]:
    return {
        "name": name,
        "url": f"https://bucket.s3.us-east-2.amazonaws.com/{name}",
        "expiresAt": expires,
        "headers": {},
        "method": "GET",
    }


def _batch_broker(answer: Any) -> tuple[LambdaPersistenceBroker, ScriptedLambda]:
    fake = ScriptedLambda(answer)
    broker_client = LambdaBrokerClient(fake, "arn:broker", SANDBOX, SESSION, "binding")
    now = datetime(2026, 8, 18, 9, 50, tzinfo=UTC)
    return LambdaPersistenceBroker(broker_client, SANDBOX, clock=lambda: now), fake


def _single(body: dict[str, object]) -> tuple[object, bool]:
    return _grant(cast(str, body["name"])), False


def test_announced_chunks_are_granted_in_batches() -> None:
    def answer(body: dict[str, object]) -> tuple[object, bool]:
        if body["operation"] == "presignBatch":
            return {"grants": [_grant(name) for name in cast(list[str], body["names"])]}, False
        return _single(body)

    broker, fake = _batch_broker(answer)
    digests = [f"{index:064x}" for index in range(300)]
    broker.prefetch_chunks(SANDBOX, digests, StorageOperation.GET)
    broker.prefetch_chunks(SANDBOX, ["x"], StorageOperation.HEAD)  # not batchable: ignored

    urls = [broker.presign_chunk(SANDBOX, digest, StorageOperation.GET).url for digest in digests]

    assert urls == [f"https://bucket.s3.us-east-2.amazonaws.com/{d}.bin" for d in digests]
    # 300 chunks in three broker calls (128 + 128 + 44), not 300.
    assert [len(cast(list[str], r["names"])) for r in fake.requests] == [128, 128, 44]
    # An unannounced chunk still gets its own single grant.
    broker.presign_chunk(SANDBOX, "f" * 64, StorageOperation.GET)
    assert fake.requests[-1]["operation"] == "presign"


def test_grant_close_to_expiry_is_not_used() -> None:
    def answer(body: dict[str, object]) -> tuple[object, bool]:
        if body["operation"] == "presignBatch":
            names = cast(list[str], body["names"])
            return {"grants": [_grant(name, "2026-08-18T09:50:30Z") for name in names]}, False
        return _single(body)

    broker, fake = _batch_broker(answer)
    broker.prefetch_chunks(SANDBOX, ["a" * 64, "b" * 64], StorageOperation.GET)
    broker.presign_chunk(SANDBOX, "a" * 64, StorageOperation.GET)
    assert [r["operation"] for r in fake.requests] == ["presignBatch", "presign"]


def test_broker_without_batch_falls_back_to_single_grants() -> None:
    def answer(body: dict[str, object]) -> tuple[object, bool]:
        if body["operation"] == "presignBatch":
            return {"errorType": "ValueError"}, True
        return _single(body)

    broker, fake = _batch_broker(answer)
    broker.prefetch_chunks(SANDBOX, ["a" * 64, "b" * 64], StorageOperation.GET)
    broker.presign_chunk(SANDBOX, "a" * 64, StorageOperation.GET)
    broker.presign_chunk(SANDBOX, "b" * 64, StorageOperation.GET)
    broker.prefetch_chunks(SANDBOX, ["c" * 64], StorageOperation.GET)
    broker.presign_chunk(SANDBOX, "c" * 64, StorageOperation.GET)
    assert [r["operation"] for r in fake.requests] == [
        "presignBatch",
        "presign",
        "presign",
        "presign",
    ]


def test_refused_batch_propagates_and_bad_batches_are_rejected() -> None:
    refused, _ = _batch_broker(lambda _body: ({"errorType": "PermissionError"}, True))
    refused.prefetch_chunks(SANDBOX, ["a" * 64], StorageOperation.GET)
    with pytest.raises(BrokerRefusalError):
        refused.presign_chunk(SANDBOX, "a" * 64, StorageOperation.GET)

    for bad in ({}, {"grants": [1]}, {"grants": [{"name": 1}]}, {"grants": [{"name": "x"}]}):
        broker, _ = _batch_broker(lambda _body, bad=bad: (bad, False))
        broker.prefetch_chunks(SANDBOX, ["a" * 64], StorageOperation.GET)
        with pytest.raises(BrokerAuthorizationError, match="batch grant is invalid"):
            broker.presign_chunk(SANDBOX, "a" * 64, StorageOperation.GET)

    with pytest.raises(BrokerAuthorizationError, match="Cross-sandbox"):
        broker.prefetch_chunks("sbx_other", ["a" * 64], StorageOperation.GET)
