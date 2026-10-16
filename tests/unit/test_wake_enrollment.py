"""Multi-owner scheduled wake, step 1: enrolment at Start (issue #39).

A Start is the one moment the owner is authenticated, so the control plane
seals their subject onto the sandbox record there. These tests pin that the
seal is bound to the sandbox, that enrolment is opt-in, and that a failing
seal never fails the Start the owner asked for.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from kirocrew_agentcore_control.api import (
    WAKE_SUBJECT_PURPOSE,
    SandboxControlService,
)
from kirocrew_agentcore_control.lambda_handler import (
    DynamoSandboxRegistry,
    KmsWakeSubjectSealer,
)
from kirocrew_agentcore_control.sandbox import SandboxUnavailableError

from tests.unit.test_control_api import (
    SUBJECT,
    response_body,
    service,
    start_event,
)


class RecordingSealer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def seal(self, subject: str, sandbox_id: str) -> str:
        self.calls.append((subject, sandbox_id))
        return f"sealed:{sandbox_id}"


class FailingSealer:
    def seal(self, subject: str, sandbox_id: str) -> str:
        raise RuntimeError("kms unavailable")


def _sandbox_id(response: dict[str, object]) -> str:
    body = response_body(response)
    assert isinstance(body, dict)
    return str(body["sandboxId"])


def _with_sealer(sealer: object) -> tuple[SandboxControlService, Any]:
    control, registry, *_ = service()
    control._wake_sealer = cast(Any, sealer)  # test seam
    return control, registry


def test_start_seals_the_subject_bound_to_the_sandbox() -> None:
    sealer = RecordingSealer()
    control, registry = _with_sealer(sealer)

    response = control.handle(start_event())

    assert response["statusCode"] == 200
    sandbox_id = _sandbox_id(response)
    assert sealer.calls == [(SUBJECT, sandbox_id)]
    assert registry.wake_subject(sandbox_id) == f"sealed:{sandbox_id}"


def test_enrolment_is_off_without_a_sealer() -> None:
    control, registry, *_ = service()

    response = control.handle(start_event())

    sandbox_id = _sandbox_id(response)
    assert registry.wake_subject(sandbox_id) is None


def test_a_failing_seal_never_fails_the_start() -> None:
    control, registry = _with_sealer(FailingSealer())

    response = control.handle(start_event())

    assert response["statusCode"] == 200
    sandbox_id = _sandbox_id(response)
    assert registry.wake_subject(sandbox_id) is None


def test_in_memory_registry_refuses_an_unknown_sandbox_and_empty_seal() -> None:
    _, registry, *_ = service()
    with pytest.raises(SandboxUnavailableError):
        registry.record_wake_subject("sbx_missing", "sealed")
    with pytest.raises(ValueError, match="required"):
        registry.record_wake_subject("sbx_missing", "")


class FakeKms:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def encrypt(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        return {"CiphertextBlob": b"\x01cipher"}


def test_kms_sealer_binds_purpose_and_sandbox_in_the_encryption_context() -> None:
    kms = FakeKms()
    sealed = KmsWakeSubjectSealer(kms, "arn:aws:kms:k").seal("sub-1", "sbx_1")

    assert sealed == "AWNpcGhlcg=="
    assert kms.requests == [
        {
            "KeyId": "arn:aws:kms:k",
            "Plaintext": b"sub-1",
            "EncryptionContext": {"purpose": WAKE_SUBJECT_PURPOSE, "sandboxId": "sbx_1"},
        }
    ]
    with pytest.raises(ValueError, match="WAKE_SUBJECT_KEY_ARN"):
        KmsWakeSubjectSealer(kms, "")

    class EmptyKms:
        def encrypt(self, **_request: Any) -> dict[str, Any]:
            return {}

    with pytest.raises(RuntimeError, match="ciphertext"):
        KmsWakeSubjectSealer(EmptyKms(), "arn").seal("sub-1", "sbx_1")


class FakeDynamo:
    def __init__(self, *, exists: bool = True, stored: str | None = None) -> None:
        self.exists = exists
        self.stored = stored
        self.updates: list[dict[str, Any]] = []

    def update_item(self, **request: Any) -> dict[str, Any]:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        if not self.exists:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        self.updates.append(request)
        return {}

    def get_item(self, **_request: Any) -> dict[str, Any]:
        if self.stored is None:
            return {"Item": {}}
        return {"Item": {"wakeSubject": {"S": self.stored}}}


def test_dynamo_registry_sets_only_the_wake_field_on_an_existing_record() -> None:
    client = FakeDynamo()
    DynamoSandboxRegistry(client, "table").record_wake_subject("sbx_1", "sealed")

    (update,) = client.updates
    assert update["UpdateExpression"] == "SET wakeSubject = :sealed"
    assert update["ConditionExpression"] == "attribute_exists(pk)"
    assert update["ExpressionAttributeValues"] == {":sealed": {"S": "sealed"}}

    with pytest.raises(SandboxUnavailableError):
        DynamoSandboxRegistry(FakeDynamo(exists=False), "table").record_wake_subject(
            "sbx_1", "sealed"
        )
    assert DynamoSandboxRegistry(FakeDynamo(stored="x"), "t").wake_subject("sbx_1") == "x"
    assert DynamoSandboxRegistry(FakeDynamo(), "t").wake_subject("sbx_1") is None
    with pytest.raises(ValueError, match="required"):
        DynamoSandboxRegistry(FakeDynamo(), "t").record_wake_subject("sbx_1", "")

    class ThrottledDynamo(FakeDynamo):
        def update_item(self, **request: Any) -> dict[str, Any]:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")

    from botocore.exceptions import ClientError

    with pytest.raises(ClientError):
        DynamoSandboxRegistry(ThrottledDynamo(), "t").record_wake_subject("sbx_1", "sealed")
