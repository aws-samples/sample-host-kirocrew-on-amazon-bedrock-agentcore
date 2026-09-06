from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from kirocrew_agentcore_adapter.identity import KiroAuthState
from kirocrew_agentcore_adapter.transport import (
    AdapterConfig,
    AdapterRequestError,
    AgentCoreAdapter,
    BindingClaims,
    EchoRuntimeBackend,
    LeaseAuthorizationError,
    RuntimeReadiness,
    SessionInitializationError,
)
from kirocrew_agentcore_persistence.checkpoint import (
    CheckpointEngine,
    CheckpointError,
    CheckpointReceipt,
    SystemFlusher,
)
from kirocrew_agentcore_persistence.durability import BrokeredCheckpointStore
from kirocrew_agentcore_persistence.remote import LambdaBrokerClient
from kirocrew_agentcore_persistence.restore import RestoreError, RestoreReport
from kirocrew_agentcore_runtime.aws_runtime import (
    AwsKmsSignatureVerifier,
    AwsSessionInitializer,
    DynamoLeaseAuthorizer,
    DynamoSandboxStateStore,
    InitOwnershipError,
    ProductionRuntimeBackend,
    StagedStateFlusher,
    _required,
)
from kirocrew_agentcore_runtime.supervisor import (
    GatewayExitedError,
    KiroCrewSupervisor,
    RuntimeMetadata,
)


class FakeKms:
    def __init__(self, valid: bool) -> None:
        self.valid = valid
        self.request: dict[str, Any] | None = None

    def verify(self, **request: Any) -> dict[str, bool]:
        self.request = request
        return {"SignatureValid": self.valid}


class FakeDynamo:
    def __init__(self, item: dict[str, dict[str, str]]) -> None:
        self.item = item
        self.request: dict[str, Any] | None = None
        self.updates: list[dict[str, Any]] = []

    def get_item(self, **request: Any) -> dict[str, object]:
        self.request = request
        return {"Item": self.item}

    def update_item(self, **request: Any) -> dict[str, object]:
        self.updates.append(request)
        return {}


def test_kms_verifier_uses_rsa_pss_and_fails_closed() -> None:
    client = FakeKms(True)
    verifier = AwsKmsSignatureVerifier(client, "arn:aws:kms:us-east-2:123456789012:key/test")
    verifier.verify(b"payload", b"signature")
    assert client.request == {
        "KeyId": "arn:aws:kms:us-east-2:123456789012:key/test",
        "Message": b"payload",
        "MessageType": "RAW",
        "Signature": b"signature",
        "SigningAlgorithm": "RSASSA_PSS_SHA_256",
    }
    with pytest.raises(InvalidSignature):
        AwsKmsSignatureVerifier(FakeKms(False), "key").verify(b"payload", b"signature")
    with pytest.raises(ValueError, match="BINDING_KEY_ARN"):
        AwsKmsSignatureVerifier(client, "")


def test_dynamo_authorizer_requires_matching_active_session() -> None:
    client = FakeDynamo({"runtimeSessionId": {"S": "session-1"}, "state": {"S": "READY"}})
    authorizer = DynamoLeaseAuthorizer(client, "sandboxes")
    authorizer.authorize("subject", "sbx_0123456789ABCDEFGHJKMNPQ", "session-1")
    assert client.request == {
        "TableName": "sandboxes",
        "Key": {
            "pk": {"S": "SANDBOX#sbx_0123456789ABCDEFGHJKMNPQ"},
            "sk": {"S": "METADATA"},
        },
        "ConsistentRead": True,
        "ProjectionExpression": "runtimeSessionId, #state",
        "ExpressionAttributeNames": {"#state": "state"},
    }

    for item in (
        {"runtimeSessionId": {"S": "other"}, "state": {"S": "READY"}},
        {"runtimeSessionId": {"S": "session-1"}, "state": {"S": "STOPPED"}},
        {},
    ):
        with pytest.raises(LeaseAuthorizationError):
            DynamoLeaseAuthorizer(FakeDynamo(item), "sandboxes").authorize(
                "subject", "sandbox", "session-1"
            )
    with pytest.raises(ValueError, match="SANDBOX_TABLE"):
        DynamoLeaseAuthorizer(client, "")


def test_required_environment_and_native_invocation_routes() -> None:
    assert _required({"VALUE": "configured"}, "VALUE") == "configured"
    with pytest.raises(ValueError, match="MISSING"):
        _required({}, "MISSING")

    class AllowLease:
        def authorize(self, _subject: str, _sandbox: str, _session: str) -> None:
            return

    class AllowSignature:
        def verify(self, _message: bytes, _signature: bytes) -> None:
            return

    from kirocrew_agentcore_adapter.transport import KmsBindingVerifier

    adapter = AgentCoreAdapter(
        AdapterConfig("audience", "issuer"),
        KmsBindingVerifier(AllowSignature(), "audience", "issuer"),
        AllowLease(),
        EchoRuntimeBackend(),
        RuntimeReadiness(restore_complete=True, loopback_ready=True),
    )
    routes = {
        (route.method, route.resource.canonical)
        for route in adapter.application.router.routes()
        if route.resource is not None
    }
    assert ("POST", "/invocations") in routes
    assert ("GET", "/invocations") in routes
    assert ("GET", "/ping") in routes


class FakeStateStore:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str, str, str, str]] = []
        self.checkpoints: list[tuple[str, str, int, str]] = []
        self.healed: list[tuple[str, str]] = []

    def heal_ready(self, sandbox_id: str, runtime_session_id: str) -> None:
        self.healed.append((sandbox_id, runtime_session_id))

    def mark_error(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        failure_type: str = "UNKNOWN",
        failure_detail: str = "UNKNOWN",
        upstream_exception: str = "UNKNOWN",
        init_owner: str | None = None,
    ) -> None:
        del init_owner
        self.errors.append(
            (
                sandbox_id,
                runtime_session_id,
                failure_type,
                failure_detail,
                upstream_exception,
            )
        )

    def record_checkpoint(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        generation: int,
        manifest_digest: str,
    ) -> None:
        self.checkpoints.append((sandbox_id, runtime_session_id, generation, manifest_digest))


class FakeSupervisor:
    def __init__(self) -> None:
        self.ready = True
        self.terminated = 0

    def terminate(self, *, grace_seconds: float = 10.0) -> None:
        del grace_seconds
        self.terminated += 1
        self.ready = False


class FakeProductionBackend:
    def __init__(self) -> None:
        self.configurations: list[tuple[object, object, object]] = []

    def configure(
        self,
        engine: object,
        store: object,
        broker_client: object,
    ) -> None:
        self.configurations.append((engine, store, broker_client))


class StubAwsSessionInitializer(AwsSessionInitializer):
    def __init__(self, *, fail: bool = False) -> None:
        self.state_store = FakeStateStore()
        self.supervisor = FakeSupervisor()
        self.backend = FakeProductionBackend()
        self.sync_calls = 0
        self.fail = fail
        readiness = RuntimeReadiness()
        self.readiness = readiness
        super().__init__(
            object(),
            "arn:aws:lambda:us-east-2:123456789012:function:broker",
            cast(DynamoSandboxStateStore, self.state_store),
            Path.cwd() / "test-workspace",
            RuntimeMetadata("0.2.0", "a" * 64, "kirocrew-agentcore.v1"),
            cast(KiroCrewSupervisor, self.supervisor),
            cast(ProductionRuntimeBackend, self.backend),
            readiness,
        )

    def _initialize_sync(
        self,
        binding_token: str,
        claims: BindingClaims,
    ) -> tuple[
        LambdaBrokerClient,
        RestoreReport,
        BrokeredCheckpointStore,
        CheckpointEngine,
    ]:
        del claims
        self.sync_calls += 1
        if self.fail:
            raise RestoreError("PERSISTENCE_RESTORE_FAILED")
        client = cast(LambdaBrokerClient, SimpleNamespace(binding_token=binding_token))
        report = RestoreReport(
            "initialized",
            None,
            False,
            ("new-sandbox",),
            (),
            datetime(2026, 8, 17, tzinfo=UTC),
        )
        return (
            client,
            report,
            cast(BrokeredCheckpointStore, object()),
            cast(CheckpointEngine, object()),
        )


class FakeLoopbackBackend:
    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        del operation, request_id, payload
        yield "request.completed", {"loopback": True}

    async def cancel(self, request_id: str) -> bool:
        del request_id
        return True


class FakeCheckpointEngine:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def checkpoint(self, generation: int, *, final: bool = False) -> CheckpointReceipt:
        self.calls.append((generation, final))
        return CheckpointReceipt(
            generation,
            "b" * 64,
            datetime(2026, 8, 17, tzinfo=UTC),
            frozenset(),
        )


class FakeCheckpointStore:
    def latest_committed(self) -> int:
        return 4


class FakeBrokerClient:
    def __init__(self) -> None:
        self.receipts: list[tuple[int, str, bool]] = []

    def checkpoint_receipt(
        self, generation: int, manifest_digest: str, *, final: bool = True
    ) -> str:
        self.receipts.append((generation, manifest_digest, final))
        return f"receipt-{generation}"


def test_dynamo_state_store_validates_metadata_and_uses_conditional_updates() -> None:
    client = FakeDynamo(
        {
            "runtimeSessionId": {"S": "session-1"},
            "state": {"S": "STARTING"},
            "lastCheckpointGeneration": {"N": "3"},
        }
    )
    store = DynamoSandboxStateStore(client, "sandboxes")
    metadata = store.read("sbx_0123456789ABCDEFGHJKMNPQ")
    assert metadata.runtime_session_id == "session-1"
    assert metadata.last_checkpoint_generation == 3
    store.acquire_init("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", "owner-1")
    report = RestoreReport(
        "restored",
        3,
        False,
        ("missing-or-empty-managed-storage",),
        (3,),
        datetime(2026, 8, 17, tzinfo=UTC),
    )
    store.mark_ready("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", report, "owner-1")
    store.heartbeat_init("sbx_0123456789ABCDEFGHJKMNPQ", "owner-1")
    store.mark_error("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", init_owner="owner-1")
    store.heal_ready("sbx_0123456789ABCDEFGHJKMNPQ", "session-1")
    assert len(client.updates) == 5
    heartbeat_update = client.updates[2]
    assert heartbeat_update["ConditionExpression"] == "initOwner = :owner"
    owned_error = client.updates[3]
    assert "initOwner = :owner" in owned_error["ConditionExpression"]
    assert "REMOVE initOwner, initExpiresAt" in owned_error["UpdateExpression"]
    assert "runtimeSessionId = :session" in client.updates[0]["ConditionExpression"]
    assert client.updates[1]["ExpressionAttributeValues"][":restore"] == {"S": "RESTORED"}
    assert client.updates[4]["ExpressionAttributeValues"][":state"] == {"S": "READY"}
    assert client.updates[4]["ExpressionAttributeValues"][":allowed0"] == {"S": "STARTING"}

    for item in (
        {},
        {
            "runtimeSessionId": {"S": "session-1"},
            "state": {"S": "READY"},
            "lastCheckpointGeneration": {"N": "0"},
        },
        {
            "runtimeSessionId": {"S": "session-1"},
            "state": {"S": "READY"},
            "lastCheckpointGeneration": {"N": "invalid"},
        },
    ):
        with pytest.raises(SessionInitializationError):
            DynamoSandboxStateStore(FakeDynamo(item), "sandboxes").read("sandbox")


def test_session_initializer_is_exactly_once_and_rejects_cross_session_reuse() -> None:
    async def scenario() -> None:
        initializer = StubAwsSessionInitializer()
        claims = BindingClaims("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", 2_000_000_000)
        await initializer.initialize("subject", "binding-1", claims)
        rotated_binding = f"binding-{2}"
        await initializer.initialize("subject", rotated_binding, claims)
        assert initializer.sync_calls == 1
        assert initializer.readiness.healthy
        assert cast(Any, initializer).state_store.healed == [
            (claims.sandbox_id, claims.runtime_session_id)
        ]

        # A ConditionalCheckFailedException (record not STARTING) is benign.
        def conditional_failure(sandbox: str, session: str) -> None:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")

        cast(Any, initializer).state_store.heal_ready = conditional_failure
        await initializer.initialize("subject", "binding-2b", claims)

        # Any other Dynamo failure surfaces.
        def hard_failure(sandbox: str, session: str) -> None:
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "UpdateItem")

        cast(Any, initializer).state_store.heal_ready = hard_failure
        with pytest.raises(ClientError):
            await initializer.initialize("subject", "binding-2c", claims)

        # An unhealthy runtime must not publish READY.
        initializer.readiness.read_only = True
        cast(Any, initializer).state_store.heal_ready = conditional_failure
        healed_calls: list[str] = []
        cast(Any, initializer).state_store.heal_ready = lambda *_: healed_calls.append("x")
        final_binding = f"binding-{2}d"
        await initializer.initialize("subject", final_binding, claims)
        assert healed_calls == []
        initializer.readiness.read_only = False
        assert len(initializer.backend.configurations) == 1
        client = cast(Any, initializer)._broker_client
        assert client.binding_token == final_binding
        with pytest.raises(SessionInitializationError):
            await initializer.initialize(
                "subject",
                "binding-3",
                BindingClaims(claims.sandbox_id, "session-2", claims.expires_at),
            )

    asyncio.run(scenario())


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "UpdateItem")


def test_acquire_init_translates_conditional_failure_to_ownership_error() -> None:
    client = FakeDynamo(
        {
            "runtimeSessionId": {"S": "session-1"},
            "state": {"S": "STARTING"},
        }
    )

    def failing_update(**kwargs: object) -> dict[str, object]:
        raise _client_error("ConditionalCheckFailedException")

    client.update_item = failing_update  # type: ignore[method-assign]
    store = DynamoSandboxStateStore(client, "sandboxes")
    with pytest.raises(InitOwnershipError):
        store.acquire_init("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", "owner-2")

    def failing_other(**kwargs: object) -> dict[str, object]:
        raise _client_error("ProvisionedThroughputExceededException")

    client.update_item = failing_other  # type: ignore[method-assign]
    with pytest.raises(ClientError):
        store.acquire_init("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", "owner-2")


def test_session_initializer_failure_remains_read_only_and_never_ready(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        initializer = StubAwsSessionInitializer(fail=True)
        claims = BindingClaims("sbx_0123456789ABCDEFGHJKMNPQ", "session-1", 2_000_000_000)
        with pytest.raises(SessionInitializationError):
            await initializer.initialize("subject", "binding", claims)
        assert initializer.readiness.read_only
        assert not initializer.readiness.healthy
        assert initializer.state_store.errors == [
            (claims.sandbox_id, claims.runtime_session_id, "RestoreError", "UNKNOWN", "UNKNOWN")
        ]
        assert initializer.supervisor.terminated == 1
        assert not initializer.backend.configurations

    asyncio.run(scenario())
    assert "Sandbox initialization failed (RestoreError)" in caplog.text


def test_losing_the_init_ownership_race_never_poisons_the_record() -> None:
    class OwnedElsewhereInitializer(StubAwsSessionInitializer):
        def _initialize_sync(
            self,
            binding_token: str,
            claims: BindingClaims,
        ) -> tuple[
            LambdaBrokerClient,
            RestoreReport,
            BrokeredCheckpointStore,
            CheckpointEngine,
        ]:
            del binding_token, claims
            raise InitOwnershipError("owned elsewhere")

    async def scenario() -> None:
        initializer = OwnedElsewhereInitializer()
        claims = BindingClaims("sandbox", "session", 2_000_000_000)
        with pytest.raises(SessionInitializationError, match="in progress elsewhere"):
            await initializer.initialize("subject", "binding", claims)
        # The loser walks away: no ERROR write, no gateway termination side
        # effects beyond its own cleanup.
        assert initializer.state_store.errors == []
        assert initializer.readiness.read_only

    asyncio.run(scenario())


def test_init_heartbeat_extends_the_lease_and_survives_beat_failures() -> None:
    async def scenario() -> None:
        initializer = StubAwsSessionInitializer()
        beats: list[str] = []

        def heartbeat(sandbox: str, owner: str, *, ttl_seconds: int = 90) -> None:
            beats.append(sandbox)
            if len(beats) == 2:
                raise RuntimeError("dynamo hiccup")
            if len(beats) == 3:
                # Cancellation during a beat propagates instead of being
                # swallowed by the resilience handler.
                raise asyncio.CancelledError

        initializer.state_store.heartbeat_init = heartbeat  # type: ignore[attr-defined]
        sleeps: list[float] = []
        original_sleep = asyncio.sleep

        async def fast_sleep(delay: float) -> None:
            sleeps.append(delay)
            await original_sleep(0)

        real_sleep = asyncio.sleep
        asyncio.sleep = fast_sleep  # type: ignore[assignment]
        try:
            with pytest.raises(asyncio.CancelledError):
                await initializer._init_heartbeat("sandbox")
        finally:
            asyncio.sleep = real_sleep
        assert beats == ["sandbox", "sandbox", "sandbox"]
        assert sleeps == [20, 20, 20]

    asyncio.run(scenario())


def test_gateway_exit_persists_only_allowlisted_diagnostic_code() -> None:
    class GatewayFailInitializer(StubAwsSessionInitializer):
        def _initialize_sync(
            self,
            binding_token: str,
            claims: BindingClaims,
        ) -> tuple[
            LambdaBrokerClient,
            RestoreReport,
            BrokeredCheckpointStore,
            CheckpointEngine,
        ]:
            del binding_token, claims
            raise GatewayExitedError(
                "sensitive upstream message",
                "SANDBOX_UNAVAILABLE",
                "kiro_crew.sandbox.SandboxUnavailableError",
            )

    async def scenario() -> None:
        initializer = GatewayFailInitializer()
        claims = BindingClaims("sandbox", "session", 2_000_000_000)
        with pytest.raises(SessionInitializationError):
            await initializer.initialize("subject", "binding", claims)
        assert initializer.state_store.errors == [
            (
                "sandbox",
                "session",
                "GatewayExitedError",
                "SANDBOX_UNAVAILABLE",
                "kiro_crew.sandbox.SandboxUnavailableError",
            )
        ]
        assert "sensitive upstream message" not in str(initializer.state_store.errors)

    asyncio.run(scenario())


def test_prepare_stop_commits_next_generation_and_records_metadata() -> None:
    async def scenario() -> None:
        readiness = RuntimeReadiness(True, True, False)
        backend = ProductionRuntimeBackend(
            cast(Any, FakeLoopbackBackend()),
            readiness,
        )
        engine = FakeCheckpointEngine()
        broker_client = FakeBrokerClient()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, broker_client),
        )
        events = [event async for event in backend.execute("sandbox.prepare_stop", "request-1", {})]
        assert engine.calls == [(5, True)]
        assert events[0] == (
            "checkpoint.committed",
            {
                "checkpointReceipt": "receipt-5",
                "generation": 5,
                "manifestDigest": "b" * 64,
            },
        )
        assert broker_client.receipts == [(5, "b" * 64, True)]

    asyncio.run(scenario())


def test_kiro_identity_mutations_commit_background_durability_checkpoints() -> None:
    async def scenario() -> None:
        readiness = RuntimeReadiness(True, True, False)

        class Identity:
            def __init__(self) -> None:
                self.flow_events: list[tuple[str, Mapping[str, object]]] = [
                    ("kiro.auth_required", {"userCode": "ABCD-EFGH"}),
                    ("kiro.authenticated", {"state": "authenticated"}),
                ]

            def status(self) -> Any:
                return SimpleNamespace(
                    state=KiroAuthState.REQUIRED,
                    payload=lambda: {"state": "required"},
                )

            async def cancel_device_flow(self) -> bool:
                return False

            async def logout(self) -> Any:
                return SimpleNamespace(payload=lambda: {"state": "required"})

            async def device_flow_events(
                self,
                organization: object = None,
            ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
                del organization
                for event in self.flow_events:
                    yield event

        identity = Identity()
        backend = ProductionRuntimeBackend(
            cast(Any, FakeLoopbackBackend()),
            readiness,
            identity=cast(Any, identity),
        )
        engine = FakeCheckpointEngine()
        broker_client = FakeBrokerClient()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, broker_client),
        )

        # A fresh sign-in commits a background, non-final checkpoint.
        events = [e async for e in backend.execute("kiro.login.start", "request-1", {})]
        assert [operation for operation, _ in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        await asyncio.gather(*backend._durability_tasks)
        assert engine.calls == [(5, False)]
        assert broker_client.receipts == [(5, "b" * 64, False)]

        # A sign-out persists the credential mutation the same way.
        events = [e async for e in backend.execute("kiro.logout", "request-2", {})]
        assert events == [("kiro.auth_status", {"state": "required"})]
        await asyncio.gather(*backend._durability_tasks)
        assert engine.calls == [(5, False), (5, False)]

        # A flow that never authenticates does not checkpoint.
        identity.flow_events = [("kiro.auth_status", {"state": "failed"})]
        _ = [e async for e in backend.execute("kiro.login.start", "request-3", {})]
        await asyncio.gather(*backend._durability_tasks)
        assert len(engine.calls) == 2

        # A failing background checkpoint is swallowed: the session keeps
        # working and readiness is not degraded.
        def broken(generation: int, *, final: bool = False) -> CheckpointReceipt:
            raise RuntimeError("durable store unavailable")

        cast(Any, engine).checkpoint = broken
        identity.flow_events = [("kiro.authenticated", {"state": "authenticated"})]
        _ = [e async for e in backend.execute("kiro.login.start", "request-4", {})]
        await asyncio.gather(*backend._durability_tasks)
        assert not readiness.read_only

    asyncio.run(scenario())


def test_periodic_checkpoints_commit_only_when_the_workspace_changed() -> None:
    async def scenario() -> None:
        readiness = RuntimeReadiness(True, True, False)
        backend = ProductionRuntimeBackend(cast(Any, FakeLoopbackBackend()), readiness)
        engine = FakeCheckpointEngine()
        broker_client = FakeBrokerClient()

        with pytest.raises(ValueError, match="interval"):
            await backend.run_periodic_checkpoints(0)

        working_checkpoint = engine.checkpoint

        def broken(generation: int, *, final: bool = False) -> CheckpointReceipt:
            raise RuntimeError("durable store unavailable")

        fingerprints = iter(["skip", "skip", "a", "a", "b", "b"])
        tick = 0

        async def sleep(seconds: float) -> None:
            nonlocal tick
            assert seconds == 30.0
            tick += 1
            if tick == 2:
                # Tick 1 observed the unconfigured backend; configure now.
                backend.configure(
                    cast(CheckpointEngine, engine),
                    cast(BrokeredCheckpointStore, FakeCheckpointStore()),
                    cast(LambdaBrokerClient, broker_client),
                )
                readiness.read_only = True
            if tick == 3:
                readiness.read_only = False
            if tick == 5:
                cast(Any, engine).checkpoint = broken
            if tick == 6:
                cast(Any, engine).checkpoint = working_checkpoint
            if tick == 7:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await backend.run_periodic_checkpoints(
                30.0,
                fingerprint=lambda: next(fingerprints),
                sleep=sleep,
            )
        # Tick 1: unconfigured. Tick 2: read-only. Tick 3: "a" commits.
        # Tick 4: "a" unchanged, skipped. Tick 5: "b" fails, fingerprint not
        # recorded. Tick 6: "b" retried and commits.
        assert engine.calls == [(5, False), (5, False)]
        assert broker_client.receipts == [(5, "b" * 64, False), (5, "b" * 64, False)]

        # Without a fingerprint every interval commits.
        plain = ProductionRuntimeBackend(cast(Any, FakeLoopbackBackend()), readiness)
        plain_engine = FakeCheckpointEngine()
        plain.configure(
            cast(CheckpointEngine, plain_engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, FakeBrokerClient()),
        )
        plain_ticks = 0

        async def plain_sleep(_seconds: float) -> None:
            nonlocal plain_ticks
            plain_ticks += 1
            if plain_ticks == 2:
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await plain.run_periodic_checkpoints(10.0, sleep=plain_sleep)
        assert plain_engine.calls == [(5, False)]

    asyncio.run(scenario())


def test_shutdown_checkpoint_is_final_best_effort_and_bounded() -> None:
    async def scenario() -> None:
        readiness = RuntimeReadiness(True, True, False)
        unconfigured = ProductionRuntimeBackend(cast(Any, FakeLoopbackBackend()), readiness)
        await unconfigured.checkpoint_on_shutdown()

        backend = ProductionRuntimeBackend(cast(Any, FakeLoopbackBackend()), readiness)
        engine = FakeCheckpointEngine()
        broker_client = FakeBrokerClient()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, broker_client),
        )
        await backend.checkpoint_on_shutdown()
        assert engine.calls == [(5, True)]
        assert broker_client.receipts == [(5, "b" * 64, True)]

        def broken(generation: int, *, final: bool = False) -> CheckpointReceipt:
            raise RuntimeError("durable store unavailable")

        cast(Any, engine).checkpoint = broken
        await backend.checkpoint_on_shutdown()
        assert len(broker_client.receipts) == 1

    asyncio.run(scenario())


def test_remaining_state_store_and_backend_branches() -> None:
    with pytest.raises(ValueError, match="SANDBOX_TABLE"):
        DynamoSandboxStateStore(FakeDynamo({}), "")
    client = FakeDynamo({})
    store = DynamoSandboxStateStore(client, "sandboxes")
    store.mark_error("sandbox", "session")
    assert client.updates[0]["ExpressionAttributeValues"][":failure"] == {"S": "UNKNOWN"}
    assert client.updates[0]["ExpressionAttributeValues"][":detail"] == {"S": "UNKNOWN"}
    assert ":starting, :restoring" in client.updates[0]["ConditionExpression"]
    store.record_checkpoint("sandbox", "session", 2, "a" * 64)
    assert len(client.updates) == 2

    async def scenario() -> None:
        readiness = RuntimeReadiness(True, True, False)
        loopback = FakeLoopbackBackend()
        backend = ProductionRuntimeBackend(cast(Any, loopback), readiness)
        normal = [event async for event in backend.execute("chat.submit", "request", {})]
        assert normal == [("request.completed", {"loopback": True})]
        assert await backend.cancel("request")
        with pytest.raises(SessionInitializationError, match="not initialized"):
            backend._checkpoint_context()

        # kiro.login.start without a configured identity manager is refused.
        with pytest.raises(AdapterRequestError, match="not configured"):
            _ = [e async for e in backend.execute("kiro.login.start", "request-l0", {})]

        class FakeIdentity:
            def __init__(self, authenticated: bool) -> None:
                self._authenticated = authenticated
                self.cancelled = 0
                self.logged_out = 0
                self.organizations: list[object] = []

            def status(self) -> Any:
                return SimpleNamespace(
                    state=KiroAuthState.AUTHENTICATED
                    if self._authenticated
                    else KiroAuthState.REQUIRED,
                    payload=lambda: {"state": "authenticated"},
                )

            async def cancel_device_flow(self) -> bool:
                self.cancelled += 1
                return False

            async def logout(self) -> Any:
                self.logged_out += 1
                self._authenticated = False
                return SimpleNamespace(payload=lambda: {"state": "required"})

            async def device_flow_events(
                self,
                organization: object = None,
            ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
                self.organizations.append(organization)
                yield "kiro.auth_required", {"userCode": "ABCD-EFGH"}
                yield "kiro.authenticated", {"state": "authenticated"}

        authed = ProductionRuntimeBackend(
            cast(Any, loopback), readiness, identity=cast(Any, FakeIdentity(True))
        )
        events = [e async for e in authed.execute("kiro.login.start", "request-l1", {})]
        assert events == [("kiro.authenticated", {"state": "authenticated"})]

        flow_identity = FakeIdentity(False)
        flow = ProductionRuntimeBackend(
            cast(Any, loopback), readiness, identity=cast(Any, flow_identity)
        )
        events = [e async for e in flow.execute("kiro.login.start", "request-l2", {})]
        assert [operation for operation, _ in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        assert flow_identity.cancelled == 1
        assert flow_identity.organizations == [None]

        # kiro.status reports the current identity state without side effects.
        events = [e async for e in flow.execute("kiro.status", "request-l3", {})]
        assert events == [("kiro.auth_status", {"state": "authenticated"})]

        # kiro.logout clears credentials and reports the resulting state.
        events = [e async for e in flow.execute("kiro.logout", "request-l4", {})]
        assert events == [("kiro.auth_status", {"state": "required"})]
        assert flow_identity.logged_out == 1

        # SSO login forwards validated Identity Center parameters.
        events = [
            e
            async for e in flow.execute(
                "kiro.login.start",
                "request-l5",
                {
                    "method": "sso",
                    "startUrl": "https://example.awsapps.com/start",
                    "region": "us-east-1",
                },
            )
        ]
        assert [operation for operation, _ in events] == [
            "kiro.auth_required",
            "kiro.authenticated",
        ]
        organization = flow_identity.organizations[-1]
        assert organization is not None
        assert getattr(organization, "start_url", None) == "https://example.awsapps.com/start"
        assert getattr(organization, "region", None) == "us-east-1"

        # Invalid login payloads are rejected with a client error.
        for payload in (
            {"method": "password"},
            {"method": "sso"},
            {"method": "sso", "startUrl": "http://insecure", "region": "us-east-1"},
            {"method": "sso", "startUrl": "https://example.awsapps.com/start", "region": "bad"},
        ):
            with pytest.raises(AdapterRequestError) as excinfo:
                _ = [
                    e
                    async for e in flow.execute(
                        "kiro.login.start", "request-l6", cast(Any, payload)
                    )
                ]
            assert excinfo.value.status == 400

        engine = FakeCheckpointEngine()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, FakeBrokerClient()),
        )
        with pytest.raises(SessionInitializationError, match="already configured"):
            backend.configure(
                cast(CheckpointEngine, engine),
                cast(BrokeredCheckpointStore, FakeCheckpointStore()),
                cast(LambdaBrokerClient, FakeBrokerClient()),
            )

        class BrokenEngine(FakeCheckpointEngine):
            def checkpoint(self, generation: int, *, final: bool = False) -> CheckpointReceipt:
                del generation, final
                raise RuntimeError("checkpoint failed")

        failed = ProductionRuntimeBackend(cast(Any, loopback), readiness)
        failed.configure(
            cast(CheckpointEngine, BrokenEngine()),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, FakeBrokerClient()),
        )
        with pytest.raises(Exception, match="final checkpoint"):
            _ = [event async for event in failed.execute("sandbox.prepare_stop", "request", {})]
        assert readiness.read_only

    asyncio.run(scenario())


def test_initializer_constructor_and_error_update_failure() -> None:
    readiness = RuntimeReadiness()
    with pytest.raises(ValueError, match="positive startup timeout"):
        AwsSessionInitializer(
            object(),
            "",
            cast(DynamoSandboxStateStore, object()),
            Path.cwd(),
            RuntimeMetadata("0.2.0", "a" * 64, "kirocrew-agentcore.v1"),
            cast(KiroCrewSupervisor, FakeSupervisor()),
            cast(ProductionRuntimeBackend, FakeProductionBackend()),
            readiness,
        )

    class BrokenStateStore(FakeStateStore):
        def mark_error(
            self,
            sandbox_id: str,
            runtime_session_id: str,
            failure_type: str = "UNKNOWN",
            failure_detail: str = "UNKNOWN",
            upstream_exception: str = "UNKNOWN",
            init_owner: str | None = None,
        ) -> None:
            del sandbox_id, runtime_session_id, failure_type, failure_detail
            del upstream_exception, init_owner
            raise RuntimeError("state update failed")

    initializer = StubAwsSessionInitializer(fail=True)
    initializer._state_store = cast(DynamoSandboxStateStore, BrokenStateStore())

    async def scenario() -> None:
        claims = BindingClaims("sandbox", "session", 2_000_000_000)
        with pytest.raises(SessionInitializationError) as captured:
            await initializer.initialize("subject", "binding", claims)
        cause = captured.value.__cause__
        assert cause is not None
        assert any("ERROR lifecycle update failed" in note for note in cause.__notes__)

    asyncio.run(scenario())


def test_initialize_sync_restore_composition(monkeypatch: pytest.MonkeyPatch) -> None:
    import kirocrew_agentcore_runtime.aws_runtime as module

    metadata = SimpleNamespace(runtime_session_id="session", last_checkpoint_generation=3)

    class State:
        def __init__(self) -> None:
            self.ready: list[object] = []
            self.restoring: list[tuple[str, str]] = []

        def read(self, _sandbox: str) -> object:
            return metadata

        def acquire_init(self, sandbox: str, session: str, owner: str) -> None:
            self.restoring.append((sandbox, session))

        def heartbeat_init(self, sandbox: str, owner: str, *, ttl_seconds: int = 90) -> None:
            return None

        def mark_ready(self, sandbox: str, session: str, report: object, owner: str) -> None:
            self.ready.append((sandbox, session, report))

    class Supervisor(FakeSupervisor):
        def start(self, *, timeout_seconds: float) -> object:
            self.ready = True
            self.timeout = timeout_seconds
            return object()

    state = State()
    supervisor = Supervisor()
    initializer = AwsSessionInitializer(
        object(),
        "arn:broker",
        cast(DynamoSandboxStateStore, state),
        Path("/workspace"),
        RuntimeMetadata("0.2.0", "a" * 64, "kirocrew-agentcore.v1"),
        cast(KiroCrewSupervisor, supervisor),
        cast(ProductionRuntimeBackend, FakeProductionBackend()),
        RuntimeReadiness(),
        startup_timeout_seconds=12,
    )
    claims = BindingClaims("sandbox", "session", 2_000_000_000)

    class Client:
        def __init__(self, *_args: object) -> None:
            self.binding_token = "binding"  # noqa: S105 - opaque binding fixture

    class Broker:
        def __init__(self, _client: object, _sandbox: str) -> None:
            pass

        def cipher(self, _sandbox: str) -> object:
            return object()

    class Store:
        def __init__(self, _broker: object, _sandbox: str) -> None:
            pass

        def committed_generations(self) -> tuple[object, ...]:
            return (SimpleNamespace(generation=3),)

    report = RestoreReport("restored", 3, False, (), (3,), datetime(2026, 8, 18, tzinfo=UTC))

    class Restore:
        def __init__(self, *_args: object) -> None:
            pass

        def restore(self, *, existing_sandbox: bool) -> RestoreReport:
            assert existing_sandbox
            return report

    sentinel_engine = object()
    monkeypatch.setattr(module, "LambdaBrokerClient", Client)
    monkeypatch.setattr(module, "LambdaPersistenceBroker", Broker)
    monkeypatch.setattr(module, "BrokeredCheckpointStore", Store)
    monkeypatch.setattr(module, "RestoreEngine", Restore)
    monkeypatch.setattr(module, "CheckpointEngine", lambda *_args, **_kwargs: sentinel_engine)
    client, actual_report, store, engine = initializer._initialize_sync("binding", claims)
    assert client.binding_token == "binding"  # noqa: S105 - opaque binding fixture
    assert actual_report is report
    assert isinstance(store, Store)
    assert engine is sentinel_engine
    assert state.restoring == [("sandbox", "session")]
    assert state.ready == [("sandbox", "session", report)]
    assert supervisor.timeout == 12

    metadata.runtime_session_id = "changed"
    with pytest.raises(SessionInitializationError, match="session changed"):
        initializer._initialize_sync("binding", claims)
    metadata.runtime_session_id = "session"
    metadata.last_checkpoint_generation = 4
    with pytest.raises(RestoreError, match="PERSISTENCE_RESTORE_FAILED"):
        initializer._initialize_sync("binding", claims)
    # The durable store may be AHEAD of the pointer: a background checkpoint
    # committed to S3 but died before its receipt landed. Restoring the newer
    # committed generation is safe.
    metadata.last_checkpoint_generation = 2
    initializer._initialize_sync("binding", claims)
    metadata.last_checkpoint_generation = 3
    supervisor.ready = False

    def start_unready(*, timeout_seconds: float) -> object:
        del timeout_seconds
        supervisor.ready = False
        return object()

    supervisor.start = start_unready  # type: ignore[method-assign]
    with pytest.raises(SessionInitializationError, match="gateway did not become ready"):
        initializer._initialize_sync("binding", claims)


def test_build_runtime_application_cleanup_and_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    import kirocrew_agentcore_runtime.aws_runtime as module
    from aiohttp import web

    image = SimpleNamespace(
        kirocrew_version="0.2.0",
        kirocrew_artifact_sha256="a" * 64,
        protocol_version="kirocrew-agentcore.v1",
    )
    monkeypatch.setattr(
        vars(module)["ImageMetadata"], "from_environment", lambda _environment: image
    )

    class Supervisor:
        def __init__(self, *_args: object) -> None:
            self.ready = False
            self.terminated = 0

        def terminate(self) -> None:
            self.terminated += 1

    supervisor_instances: list[Supervisor] = []

    def make_supervisor(*args: object) -> Supervisor:
        value = Supervisor(*args)
        supervisor_instances.append(value)
        return value

    clients = {"kms": object(), "dynamodb": object(), "lambda": object()}

    class Session:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == "us-east-2"

        def client(self, name: str) -> object:
            return clients[name]

    class LoopbackSession:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    loopback_sessions: list[LoopbackSession] = []

    def make_session() -> LoopbackSession:
        value = LoopbackSession()
        loopback_sessions.append(value)
        return value

    captured: dict[str, object] = {}

    class Adapter:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs
            self.application = web.Application()

    monkeypatch.setattr(module, "KiroCrewSupervisor", make_supervisor)
    monkeypatch.setattr(vars(module)["boto3"], "Session", Session)
    monkeypatch.setattr(module, "ClientSession", make_session)
    monkeypatch.setattr(module, "LoopbackKiroCrewBackend", lambda *_args: object())
    monkeypatch.setattr(module, "AgentCoreAdapter", Adapter)
    environment = {
        "AWS_REGION": "us-east-2",
        "BINDING_AUDIENCE": "audience",
        "BINDING_KEY_ARN": "arn:key",
        "COGNITO_ISSUER": "https://issuer",
        "PERSISTENCE_BROKER_ARN": "arn:broker",
        "SANDBOX_TABLE": "sandboxes",
        "WORKSPACE_ROOT": str(Path.cwd() / "workspace"),
        "KIROCREW_START_TIMEOUT": "5",
    }

    async def scenario() -> None:
        application = await module.build_runtime_application(environment)
        assert captured["kwargs"]
        for startup in application.on_startup:
            await startup(application)
        for shutdown in application.on_shutdown:
            await shutdown(application)
        for cleanup in application.on_cleanup:
            await cleanup(application)
        assert loopback_sessions[0].closed
        assert supervisor_instances[0].terminated == 1

        # Interval 0 disables the periodic checkpoint loop entirely.
        disabled = await module.build_runtime_application(
            {**environment, "KIROCREW_CHECKPOINT_INTERVAL_SECONDS": "0"}
        )
        for startup in disabled.on_startup:
            await startup(disabled)
        for shutdown in disabled.on_shutdown:
            await shutdown(disabled)
        for cleanup in disabled.on_cleanup:
            await cleanup(disabled)

    asyncio.run(scenario())

    calls: list[tuple[object, str, int]] = []

    def run_app(application: object, *, host: str, port: int) -> None:
        calls.append((application, host, port))
        if asyncio.iscoroutine(application):
            application.close()

    monkeypatch.setattr(vars(module)["web"], "run_app", run_app)
    module.serve_runtime(environment)
    assert calls[0][1:] == ("0.0.0.0", 8080)  # noqa: S104 - required ingress


def test_initializer_rejects_successful_restore_without_ready_gateway() -> None:
    initializer = StubAwsSessionInitializer()
    initializer.supervisor.ready = False

    async def scenario() -> None:
        claims = BindingClaims("sandbox", "session", 2_000_000_000)
        with pytest.raises(SessionInitializationError, match="did not become ready"):
            await initializer.initialize("subject", "binding", claims)
        assert initializer.readiness.read_only
        assert initializer.supervisor.terminated == 1

    asyncio.run(scenario())


def test_staged_state_flusher_writes_state_back_before_flushing(tmp_path: Path) -> None:
    order: list[str] = []

    class FakeSupervisor:
        def synchronize_state(self) -> None:
            order.append("synchronized")

    class RecordingInner:
        def flush(self, workspace: Path) -> None:
            order.append(f"flushed:{workspace.name}")

    flusher = StagedStateFlusher(
        cast(KiroCrewSupervisor, FakeSupervisor()),
        inner=cast(SystemFlusher, RecordingInner()),
    )
    flusher.flush(tmp_path / "workspace")
    assert order == ["synchronized", "flushed:workspace"]

    default = StagedStateFlusher(cast(KiroCrewSupervisor, FakeSupervisor()))
    with pytest.raises(CheckpointError):
        default.flush(tmp_path / "missing")


class ProbeLoopback(FakeLoopbackBackend):
    """FakeLoopbackBackend with programmable probe answers per path."""

    def __init__(self) -> None:
        self.responses: dict[str, object] = {}
        self.calls: list[str] = []

    async def fetch_json(self, path: str, *, timeout_seconds: float = 2.0) -> object:
        del timeout_seconds
        self.calls.append(path)
        value = self.responses.get(path, RuntimeError("probe unavailable"))
        if isinstance(value, Exception):
            raise value
        return value


def probe_backend(
    loopback: ProbeLoopback,
    *,
    ttl: float = 10.0,
    busy_max: float = 14400.0,
    clock: Callable[[], float] | None = None,
) -> ProductionRuntimeBackend:
    readiness = RuntimeReadiness(True, True, False)
    return ProductionRuntimeBackend(
        cast(Any, loopback),
        readiness,
        busy_probe_ttl_seconds=ttl,
        busy_max_seconds=busy_max,
        monotonic=clock or (lambda: 0.0),
    )


def test_background_busy_detects_each_activity_source_and_tolerates_failures() -> None:
    async def scenario() -> None:
        loopback = ProbeLoopback()
        # Task runner run in flight.
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        assert await probe_backend(loopback).background_busy() is True
        # Running subagents count.
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": False}]}
        loopback.responses["/api/status"] = {"subagents": 2}
        assert await probe_backend(loopback).background_busy() is True
        # Workflow run marked running.
        loopback.responses["/api/status"] = {"subagents": 0}
        loopback.responses["/api/workflows/runs"] = {"runs": [{"status": "running"}]}
        assert await probe_backend(loopback).background_busy() is True
        # Everything quiet.
        loopback.responses["/api/workflows/runs"] = {"runs": [{"status": "completed"}]}
        assert await probe_backend(loopback).background_busy() is False
        # Malformed answers and dead endpoints count as idle, never busy.
        loopback.responses = {"/api/taskrunner": {"runs": "nope"}, "/api/status": []}
        assert await probe_backend(loopback).background_busy() is False
        loopback.responses = {"/api/taskrunner": [1], "/api/workflows/runs": [1]}
        assert await probe_backend(loopback).background_busy() is False
        loopback.responses = {}
        assert await probe_backend(loopback).background_busy() is False

    asyncio.run(scenario())


def test_background_busy_caches_probes_and_honours_disable() -> None:
    async def scenario() -> None:
        loopback = ProbeLoopback()
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        clock = {"now": 0.0}
        backend = probe_backend(loopback, ttl=10.0, clock=lambda: clock["now"])
        assert await backend.background_busy() is True
        assert await backend.background_busy() is True
        # Two answers, one probe: the second call hit the cache.
        assert loopback.calls.count("/api/taskrunner") == 1
        clock["now"] = 11.0
        assert await backend.background_busy() is True
        assert loopback.calls.count("/api/taskrunner") == 2
        # A zero TTL disables probing entirely.
        disabled = probe_backend(loopback, ttl=0.0)
        assert await disabled.background_busy() is False
        assert loopback.calls.count("/api/taskrunner") == 2

    asyncio.run(scenario())


def test_background_busy_probe_errors_and_timeouts_count_as_idle() -> None:
    async def scenario() -> None:
        loopback = ProbeLoopback()
        backend = probe_backend(loopback)

        async def exploding_probe() -> bool:
            raise RuntimeError("probe blew up")

        backend._probe_background_activity = exploding_probe  # type: ignore[method-assign]
        assert await backend.background_busy() is False

    asyncio.run(scenario())


def test_background_busy_fuse_reports_idle_and_checkpoint_fires_on_transition() -> None:
    async def scenario() -> None:
        loopback = ProbeLoopback()
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        clock = {"now": 0.0}
        backend = probe_backend(loopback, ttl=1.0, busy_max=100.0, clock=lambda: clock["now"])
        engine = FakeCheckpointEngine()
        broker_client = FakeBrokerClient()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, broker_client),
        )
        assert await backend.background_busy() is True
        # Still busy beyond the fuse: reported idle so the platform can
        # reclaim, and the forced transition commits a checkpoint first.
        clock["now"] = 102.0
        assert await backend.background_busy() is False
        await asyncio.sleep(0)
        await asyncio.gather(*backend._durability_tasks)
        assert engine.calls == [(5, False)]
        # The fuse warning is logged once; staying busy repeats the verdict.
        clock["now"] = 104.0
        assert await backend.background_busy() is False
        # Work finishing resets the fuse for the next task.
        loopback.responses["/api/taskrunner"] = {"runs": []}
        clock["now"] = 106.0
        assert await backend.background_busy() is False
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        clock["now"] = 108.0
        assert await backend.background_busy() is True

    asyncio.run(scenario())


def test_background_idle_transition_commits_a_durability_checkpoint() -> None:
    async def scenario() -> None:
        loopback = ProbeLoopback()
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        clock = {"now": 0.0}
        backend = probe_backend(loopback, ttl=1.0, clock=lambda: clock["now"])
        engine = FakeCheckpointEngine()
        backend.configure(
            cast(CheckpointEngine, engine),
            cast(BrokeredCheckpointStore, FakeCheckpointStore()),
            cast(LambdaBrokerClient, FakeBrokerClient()),
        )
        assert await backend.background_busy() is True
        # The task finishes; the very next answer is Healthy and the state
        # is committed before the platform gets a chance to reclaim.
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": False}]}
        clock["now"] = 2.0
        assert await backend.background_busy() is False
        await asyncio.gather(*backend._durability_tasks)
        assert engine.calls == [(5, False)]
        # Idle staying idle does not checkpoint again.
        clock["now"] = 4.0
        assert await backend.background_busy() is False
        assert engine.calls == [(5, False)]
        # An unconfigured backend just skips the commit.
        bare = probe_backend(loopback, ttl=1.0, clock=lambda: clock["now"])
        loopback.responses["/api/taskrunner"] = {"runs": [{"running": True}]}
        assert await bare.background_busy() is True
        clock["now"] = 6.0
        loopback.responses["/api/taskrunner"] = {"runs": []}
        assert await bare.background_busy() is False
        assert bare._durability_tasks == set()

    asyncio.run(scenario())


def test_initializer_evicts_the_legacy_model_cache(tmp_path: Path) -> None:
    initializer = StubAwsSessionInitializer()
    initializer._workspace = tmp_path
    legacy = tmp_path / "home" / ".kiro" / "crew" / "models"
    legacy.mkdir(parents=True)
    (legacy / "qwen3-embedding-0.6b.gguf").write_bytes(b"weights")
    neighbour = tmp_path / "home" / ".kiro" / "crew" / "skills"
    neighbour.mkdir(parents=True)
    (neighbour / "keep.md").write_text("kept")
    initializer._evict_legacy_model_cache()
    assert not legacy.exists()
    assert (neighbour / "keep.md").read_text() == "kept"
    # Absent directory: a no-op, not an error.
    initializer._evict_legacy_model_cache()
