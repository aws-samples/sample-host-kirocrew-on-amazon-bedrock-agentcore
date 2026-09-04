from __future__ import annotations

import base64
import io
import json
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from typing import Any, cast

import pytest
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    PresignedOperation,
    StorageOperation,
)
from kirocrew_agentcore_persistence.remote import LambdaBrokerClient, LambdaPersistenceBroker

SANDBOX = "sbx_01J00000000000000000000000"
SESSION = "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4"


class Payload:
    def __init__(self, value: object) -> None:
        self.value = value

    def read(self) -> bytes:
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

    for value in ({}, {"checkpointReceipt": 1}):
        broken, _ = client(value)
        with pytest.raises(BrokerAuthorizationError, match="receipt is unavailable"):
            broken.checkpoint_receipt(1, "a" * 64)

    failed, _ = client({}, function_error=True)
    with pytest.raises(BrokerAuthorizationError, match="rejected"):
        failed.call("dataKey")
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


def test_https_request_success_head_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    broker_client, _ = client()
    broker = LambdaPersistenceBroker(broker_client, SANDBOX)
    expires = datetime(2026, 8, 18, tzinfo=UTC)

    class Response:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    captured: list[Any] = []

    def open_ok(request: Any, timeout: int) -> Response:
        captured.append((request, timeout))
        return Response(b"body")

    monkeypatch.setattr("urllib.request.urlopen", open_ok)
    get = PresignedOperation(
        "https://bucket.s3.us-east-2.amazonaws.com/object",
        StorageOperation.GET,
        expires,
        {"x-header": "value"},
    )
    assert broker.request(get, StorageOperation.GET) == b"body"
    assert captured[0][0].get_method() == "GET"
    head = PresignedOperation(get.url, StorageOperation.HEAD, expires, {})
    assert broker.request(head, StorageOperation.HEAD) is True

    with pytest.raises(BrokerAuthorizationError, match="does not match"):
        broker.request(get, StorageOperation.PUT)
    for url in ("http://bucket.amazonaws.com/key", "https://example.com/key", "https:///key"):
        grant = PresignedOperation(url, StorageOperation.GET, expires, {})
        with pytest.raises(BrokerAuthorizationError, match="not trusted"):
            broker.request(grant, StorageOperation.GET)
    china = PresignedOperation(
        "https://bucket.s3.cn-north-1.amazonaws.com.cn/key",
        StorageOperation.GET,
        expires,
        {},
    )
    assert broker.request(china, StorageOperation.GET) == b"body"

    def http_404(_request: Any, timeout: int) -> Response:
        del timeout
        raise urllib.error.HTTPError("url", 404, "missing", Message(), io.BytesIO())

    monkeypatch.setattr("urllib.request.urlopen", http_404)
    assert broker.request(head, StorageOperation.HEAD) is False
    with pytest.raises(BrokerAuthorizationError, match="operation failed"):
        broker.request(get, StorageOperation.GET)

    def url_error(_request: Any, timeout: int) -> Response:
        del timeout
        raise urllib.error.URLError("network")

    monkeypatch.setattr("urllib.request.urlopen", url_error)
    with pytest.raises(BrokerAuthorizationError, match="operation failed"):
        broker.request(get, StorageOperation.GET)
