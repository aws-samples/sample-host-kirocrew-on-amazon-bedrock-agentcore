from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from kirocrew_agentcore_control import lambda_handler as control
from kirocrew_agentcore_control.api import AgentCoreStopError
from kirocrew_agentcore_control.sandbox import (
    LeaseConflictError,
    SandboxRecord,
    SandboxState,
    SandboxUnavailableError,
    StateConflictError,
)

NOW = datetime(2026, 8, 18, 8, tzinfo=UTC)
SANDBOX = "sbx_01J00000000000000000000000"
SESSION = "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4"


def client_error(code: str, status: int = 400) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "operation",
    )


def record(**changes: object) -> SandboxRecord:
    values: dict[str, object] = {
        "owner_hash": "a" * 64,
        "sandbox_id": SANDBOX,
        "runtime_session_id": SESSION,
        "state": SandboxState.STOPPED,
        "state_version": 0,
        "lease_owner": None,
        "lease_expires_at": None,
        "active_request_id": None,
        "last_checkpoint_generation": None,
        "last_restore": None,
        "deletion_state": None,
        "kiro_auth_mode": "device-flow",
        "kirocrew_version": "0.2.0",
        "created_at": NOW,
        "updated_at": NOW,
    }
    values.update(changes)
    return SandboxRecord(**values)  # type: ignore[arg-type]


class FakeKms:
    def __init__(self) -> None:
        self.signature: object = b"signature"
        self.valid = True
        self.requests: list[tuple[str, dict[str, object]]] = []

    def sign(self, **request: object) -> dict[str, object]:
        self.requests.append(("sign", request))
        return {"Signature": self.signature}

    def verify(self, **request: object) -> dict[str, object]:
        self.requests.append(("verify", request))
        return {"SignatureValid": self.valid}


class FakeDynamo:
    def __init__(self) -> None:
        self.get_responses: list[dict[str, object]] = []
        self.transaction_error: ClientError | None = None
        self.put_error: ClientError | None = None
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.query_items: list[dict[str, dict[str, str]]] = []

    def get_item(self, **request: object) -> dict[str, object]:
        self.requests.append(("get", request))
        return self.get_responses.pop(0)

    def transact_write_items(self, **request: object) -> dict[str, object]:
        self.requests.append(("transact", request))
        if self.transaction_error is not None:
            raise self.transaction_error
        return {}

    def put_item(self, **request: object) -> dict[str, object]:
        self.requests.append(("put", request))
        if self.put_error is not None:
            raise self.put_error
        return {}

    def query(self, **request: object) -> dict[str, object]:
        self.requests.append(("query", request))
        return {"Items": self.query_items}


def registry(fake: FakeDynamo) -> control.DynamoSandboxRegistry:
    return control.DynamoSandboxRegistry(fake, "sandboxes", clock=lambda: NOW)


def metadata_item(generation: int, *, status: str = "COMMITTED") -> dict[str, dict[str, str]]:
    return {
        "createdAt": {"S": "2026-08-18T08:00:00Z"},
        "generation": {"N": str(generation)},
        "manifestDigest": {"S": "a" * 64},
        "schemaVersion": {"N": "1"},
        "status": {"S": status},
    }


def test_kms_signer_rsa_pss_validation() -> None:
    with pytest.raises(ValueError, match="BINDING_KEY_ARN"):
        control.KmsRsaPssSigner(FakeKms(), "")
    fake = FakeKms()
    signer = control.KmsRsaPssSigner(fake, "arn:key")
    assert signer.sign(b"message") == b"signature"
    signer.verify(b"message", b"signature")
    assert all(request["SigningAlgorithm"] == "RSASSA_PSS_SHA_256" for _, request in fake.requests)
    fake.signature = "bad"
    with pytest.raises(RuntimeError, match="signature is unavailable"):
        signer.sign(b"message")
    fake.valid = False
    with pytest.raises(ValueError, match="signature is invalid"):
        signer.verify(b"message", b"signature")


def test_registry_create_get_and_conflict_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="SANDBOX_TABLE"):
        control.DynamoSandboxRegistry(FakeDynamo(), "")
    fake = FakeDynamo()
    store = registry(fake)
    fake.get_responses = [{}, {}]
    monkeypatch.setattr(control, "new_sandbox_id", lambda: SANDBOX)
    monkeypatch.setattr(control, "new_runtime_session_id", lambda: SESSION)
    created = store.get_or_create("subject")
    assert created.sandbox_id == SANDBOX
    assert fake.requests[-1][0] == "transact"

    persisted = record()
    item = store._record_item(persisted)
    fake.get_responses = [
        {"Item": {"sandboxId": {"S": SANDBOX}}},
        {"Item": item},
    ]
    assert store.get("subject") == persisted

    response_sets: tuple[list[dict[str, object]], ...] = (
        [{}],
        [{"Item": {}}, {}],
        [{"Item": {"sandboxId": {"S": SANDBOX}}}, {}],
    )
    for responses in response_sets:
        fake.get_responses = list(responses)
        with pytest.raises(SandboxUnavailableError, match="unavailable"):
            store.get("subject")
    with pytest.raises(SandboxUnavailableError, match="unavailable"):
        store._owner_hash("")

    fake.transaction_error = client_error("TransactionCanceledException")
    fake.get_responses = [
        {},
        {"Item": {"sandboxId": {"S": SANDBOX}}},
        {"Item": item},
    ]
    assert store.get_or_create("subject") == persisted
    fake.transaction_error = client_error("AccessDenied")
    fake.get_responses = [{}, {}]
    with pytest.raises(ClientError):
        store.get_or_create("subject")


def test_registry_start_transition_delete_and_conditional_updates() -> None:
    fake = FakeDynamo()
    store = registry(fake)

    active = record(
        state=SandboxState.STARTING,
        state_version=1,
        lease_owner="owner",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    store.get_or_create = lambda cognito_subject: active  # type: ignore[method-assign]
    assert store.acquire_start("subject", "owner", ttl=timedelta(minutes=1)).authoritative
    assert not store.acquire_start("subject", "other", ttl=timedelta(minutes=1)).authoritative

    initializing = replace(
        active,
        lease_expires_at=NOW - timedelta(seconds=1),
        init_expires_at=NOW + timedelta(seconds=45),
    )
    store.get_or_create = lambda cognito_subject: initializing  # type: ignore[method-assign]
    # A live initialization owner keeps the record untouched even though the
    # browser lease expired: resetting it would poison the running restore.
    held = store.acquire_start("subject", "owner", ttl=timedelta(minutes=1))
    assert held.record is initializing
    assert held.record.state is SandboxState.STARTING

    expired = replace(active, lease_expires_at=NOW - timedelta(seconds=1))
    store.get_or_create = lambda cognito_subject: expired  # type: ignore[method-assign]
    with pytest.raises(LeaseConflictError, match="confirmation"):
        store.acquire_start("subject", "other", ttl=timedelta(minutes=1))
    reclaimed_by_owner = store.acquire_start("subject", "owner", ttl=timedelta(minutes=1))
    assert reclaimed_by_owner.authoritative
    # The lease owner follows the rotated session id.
    assert reclaimed_by_owner.record.lease_owner == reclaimed_by_owner.record.runtime_session_id
    started = store.acquire_start(
        "subject", "other", ttl=timedelta(minutes=1), confirmed_inactive=True
    )
    # Confirmed reclaim also rotates: the new session owns the lease.
    assert started.record.lease_owner == started.record.runtime_session_id

    stopping = record(
        state=SandboxState.STOPPING,
        state_version=4,
        lease_owner="owner",
        lease_expires_at=NOW + timedelta(minutes=1),
    )
    store.get = lambda cognito_subject: stopping  # type: ignore[method-assign]
    with pytest.raises(StateConflictError, match="version or lease"):
        store.transition("subject", SandboxState.STOPPED, expected_version=3, lease_owner="owner")
    with pytest.raises(StateConflictError, match="not allowed"):
        store.transition("subject", SandboxState.READY, expected_version=4, lease_owner="owner")
    stopped = store.transition(
        "subject", SandboxState.STOPPED, expected_version=4, lease_owner="owner"
    )
    assert stopped.lease_owner is None

    store.get = lambda cognito_subject: replace(  # type: ignore[method-assign]
        stopping, state=SandboxState.READY
    )
    with pytest.raises(StateConflictError, match="must be stopped"):
        store.request_deletion("subject")
    store.get = lambda cognito_subject: record()  # type: ignore[method-assign]
    assert store.request_deletion("subject").deletion_state == "REQUESTED"

    fake.put_error = client_error("ConditionalCheckFailedException")
    with pytest.raises(StateConflictError, match="version changed"):
        store._put_updated(record(), replace(record(), state_version=1))
    fake.put_error = client_error("AccessDenied")
    with pytest.raises(ClientError):
        store._put_updated(record(), replace(record(), state_version=1))


def test_registry_item_serialization_and_invalid_metadata() -> None:
    full = record(
        state=SandboxState.READY,
        lease_owner="owner",
        lease_expires_at=NOW,
        active_request_id="request",
        last_checkpoint_generation=3,
        last_restore="RESTORED",
        deletion_state="REQUESTED",
    )
    assert control.DynamoSandboxRegistry._key(SANDBOX)["pk"]["S"].endswith(SANDBOX)
    assert control.DynamoSandboxRegistry._mapping_item(full)["sandboxId"] == {"S": SANDBOX}
    item = control.DynamoSandboxRegistry._record_item(full)
    assert item["lastCheckpointGeneration"] == {"N": "3"}
    assert control.DynamoSandboxRegistry._record(item) == full

    invalid_items = [
        {},
        {**item, "state": {"S": "INVALID"}},
        {**item, "stateVersion": {"N": "bad"}},
        {**item, "createdAt": {"S": "2026-08-18T08:00:00"}},
    ]
    for invalid in invalid_items:
        with pytest.raises(SandboxUnavailableError, match="metadata is invalid"):
            control.DynamoSandboxRegistry._record(invalid)


def test_checkpoint_catalog_get_list_and_validation() -> None:
    fake = FakeDynamo()
    with pytest.raises(ValueError):
        control.DynamoCheckpointCatalog._metadata({})
    catalog = control.DynamoCheckpointCatalog(fake, "sandboxes")
    fake.get_responses = [{}]
    assert catalog.get(SANDBOX, 1) is None
    fake.get_responses = [{"Item": metadata_item(2)}]
    assert catalog.get(SANDBOX, 2).generation == 2  # type: ignore[union-attr]
    fake.query_items = [metadata_item(3), metadata_item(1, status="RESTORE_FAILED")]
    assert [item.generation for item in catalog.list(SANDBOX)] == [1, 3]
    for item in (
        metadata_item(1, status="INVALID"),
        {**metadata_item(1), "generation": {"N": "bad"}},
    ):
        with pytest.raises(ValueError, match="metadata is invalid"):
            catalog._metadata(item)


def test_agentcore_stopper_and_datetime_helpers() -> None:
    class AgentCore:
        def __init__(self, error: ClientError | None = None) -> None:
            self.error = error
            self.requests: list[dict[str, object]] = []

        def stop_runtime_session(self, **request: object) -> None:
            self.requests.append(request)
            if self.error:
                raise self.error

    fake = AgentCore()
    stopper = control.BotoAgentCoreStopper(fake, "arn:runtime", "LIVE")
    stopper.stop(SESSION)
    assert fake.requests == [
        {"agentRuntimeArn": "arn:runtime", "runtimeSessionId": SESSION, "qualifier": "LIVE"}
    ]
    with pytest.raises(AgentCoreStopError) as captured:
        control.BotoAgentCoreStopper(
            AgentCore(client_error("Internal", 429)), "arn:runtime", "LIVE"
        ).stop(SESSION)
    assert captured.value.status_code == 429
    with pytest.raises(AgentCoreStopError) as defaulted:
        control.BotoAgentCoreStopper(
            AgentCore(ClientError({"Error": {"Code": "Internal"}}, "stop")),
            "arn:runtime",
            "LIVE",
        ).stop(SESSION)
    assert defaulted.value.status_code == 500

    assert control._timestamp(NOW) == "2026-08-18T08:00:00Z"
    assert control._datetime("2026-08-18T08:00:00Z") == NOW
    with pytest.raises(ValueError, match="timezone"):
        control._datetime("2026-08-18T08:00:00")


def test_build_service_and_cached_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "ALLOWED_ORIGIN": "https://app.example.com",
        "APP_CLIENT_ID": "client",
        "BINDING_AUDIENCE": "audience",
        "BINDING_KEY_ARN": "arn:key",
        "DEPLOYMENT_MODE": "microvm",
        "FRONTEND_COMPATIBILITY_VERSION": "0.2.0",
        "ISSUER": "https://issuer.example.com",
        "REGION": "us-east-2",
        "RUNTIME_ARN": "arn:aws:bedrock-agentcore:us-east-2:123:runtime/name/id",
        "RUNTIME_QUALIFIER": "LIVE value",
        "SANDBOX_TABLE": "sandboxes",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MISSING", raising=False)
    with pytest.raises(ValueError, match="MISSING"):
        control._required("MISSING")

    clients = {"dynamodb": object(), "kms": FakeKms(), "bedrock-agentcore": object()}

    class Session:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == "us-east-2"

        def client(self, name: str) -> object:
            return clients[name]

    monkeypatch.setattr(vars(control)["boto3"], "Session", Session)
    service = control._build_service()
    config = service._config
    assert "%2F" in config.http_url
    assert config.http_url.endswith("qualifier=LIVE%20value")
    assert config.websocket_url.startswith("wss://")
    assert "/ws?" in config.websocket_url
    assert "/invocations" not in config.websocket_url
    assert config.websocket_url.endswith("qualifier=LIVE%20value")

    class FakeService:
        def __init__(self) -> None:
            self.events: list[object] = []

        def handle(self, event: object) -> dict[str, object]:
            self.events.append(event)
            return {"ok": True}

    fake_service = FakeService()
    builds: list[int] = []

    def build_fake_service() -> FakeService:
        builds.append(1)
        return fake_service

    monkeypatch.setattr(control, "_build_service", build_fake_service)
    monkeypatch.setattr(control, "_SERVICE", None)
    assert control.handler({"event": 1}, object()) == {"ok": True}
    assert control.handler({"event": 2}, object()) == {"ok": True}
    assert builds == [1]


def test_registry_records_observations_idempotently_with_ttl() -> None:
    fake = FakeDynamo()
    store = registry(fake)
    observed = record(state=SandboxState.READY, state_version=3)
    store.record_observation(observed)
    kind, request = fake.requests[-1]
    assert kind == "put"
    item = cast(dict[str, dict[str, str]], request["Item"])
    assert item["pk"]["S"] == f"SANDBOX#{SANDBOX}"
    assert item["sk"]["S"] == "EVENT#000000000003"
    assert item["state"]["S"] == "READY"
    assert item["at"]["S"] == NOW.isoformat()
    assert int(item["expiresAt"]["N"]) == int(NOW.timestamp()) + 7 * 24 * 3600
    assert request["ConditionExpression"] == "attribute_not_exists(pk)"

    # A poll seeing the same version again is a silent no-op.
    fake.put_error = ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
    store.record_observation(observed)

    fake.put_error = ClientError({"Error": {"Code": "InternalError"}}, "PutItem")
    with pytest.raises(ClientError):
        store.record_observation(observed)


def test_registry_history_queries_events_newest_first() -> None:
    fake = FakeDynamo()
    store = registry(fake)
    fake.query_items = [
        {
            "pk": {"S": f"SANDBOX#{SANDBOX}"},
            "sk": {"S": "EVENT#000000000004"},
            "state": {"S": "READY"},
            "at": {"S": NOW.isoformat()},
        },
        # Malformed rows never break the panel.
        {"pk": {"S": f"SANDBOX#{SANDBOX}"}, "sk": {"S": "EVENT#000000000003"}},
        {"pk": {"S": f"SANDBOX#{SANDBOX}"}, "sk": {"S": "OTHER"}, "state": {"S": "X"}},
    ]
    events = store.history(SANDBOX)
    assert len(events) == 1
    assert events[0].state_version == 4
    assert events[0].state is SandboxState.READY
    assert events[0].at == NOW
    kind, request = fake.requests[-1]
    assert kind == "query"
    assert request["ScanIndexForward"] is False
    assert request["Limit"] == 20
    with pytest.raises(ValueError, match="positive"):
        store.history(SANDBOX, limit=0)
