from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import boto3  # type: ignore[import-untyped]
from aiohttp import ClientSession, web
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from cryptography.exceptions import InvalidSignature
from kirocrew_agentcore_adapter.identity import (
    KiroAuthState,
    KiroIdentityConfig,
    KiroIdentityManager,
    LocalKiroCommandRunner,
    OrganizationLogin,
)
from kirocrew_agentcore_adapter.loopback import LoopbackKiroCrewBackend
from kirocrew_agentcore_adapter.transport import (
    AdapterConfig,
    AdapterRequestError,
    AgentCoreAdapter,
    BindingClaims,
    KmsBindingVerifier,
    LeaseAuthorizationError,
    RuntimeReadiness,
    SessionInitializationError,
)
from kirocrew_agentcore_persistence.checkpoint import (
    CheckpointEngine,
    CheckpointReceipt,
    SystemFlusher,
)
from kirocrew_agentcore_persistence.durability import BrokeredCheckpointStore
from kirocrew_agentcore_persistence.journal import DirtyJournal
from kirocrew_agentcore_persistence.manifest import ManifestBuilder, workspace_fingerprint
from kirocrew_agentcore_persistence.remote import LambdaBrokerClient, LambdaPersistenceBroker
from kirocrew_agentcore_persistence.restore import RestoreEngine, RestoreError, RestoreReport

from kirocrew_agentcore_runtime.image_runtime import ImageMetadata
from kirocrew_agentcore_runtime.supervisor import (
    GatewayExitedError,
    KiroCrewSupervisor,
    RuntimeMetadata,
    WorkspaceLayout,
)

_LOGGER = logging.getLogger(__name__)


class AwsKmsSignatureVerifier:
    def __init__(self, client: Any, key_arn: str) -> None:
        if not key_arn:
            raise ValueError("BINDING_KEY_ARN is required.")
        self._client = client
        self._key_arn = key_arn

    def verify(self, message: bytes, signature: bytes) -> None:
        response = self._client.verify(
            KeyId=self._key_arn,
            Message=message,
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if response.get("SignatureValid") is not True:
            raise InvalidSignature


class DynamoLeaseAuthorizer:
    def __init__(self, client: Any, table_name: str) -> None:
        if not table_name:
            raise ValueError("SANDBOX_TABLE is required.")
        self._client = client
        self._table_name = table_name

    def authorize(
        self,
        cognito_subject: str,
        sandbox_id: str,
        runtime_session_id: str,
    ) -> None:
        del cognito_subject  # The signed binding already binds the Cognito subject hash.
        response = self._client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}},
            ConsistentRead=True,
            ProjectionExpression="runtimeSessionId, #state",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = cast(Mapping[str, Mapping[str, str]], response.get("Item", {}))
        stored_session = item.get("runtimeSessionId", {}).get("S")
        state = item.get("state", {}).get("S")
        if stored_session != runtime_session_id or state not in {
            "STARTING",
            "RESTORING",
            "READY",
            "BUSY",
            "CHECKPOINTING",
        }:
            raise LeaseAuthorizationError("Sandbox lease is unavailable.")


class StagedStateFlusher:
    """Writes staged gateway state into the captured workspace, then flushes to disk.

    ``CheckpointEngine`` calls this after quiescing the gateway and before building the
    manifest, which is exactly the window where the staged SQLite working set can be copied
    back consistently. Encrypted checkpoints therefore stay the authoritative record even
    though the live databases run on a lock-capable local filesystem.
    """

    def __init__(
        self,
        supervisor: KiroCrewSupervisor,
        *,
        inner: SystemFlusher | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._inner = inner or SystemFlusher()

    def flush(self, workspace: Path) -> None:
        self._supervisor.synchronize_state()
        self._inner.flush(workspace)


@dataclass(frozen=True, slots=True)
class SandboxPersistenceMetadata:
    runtime_session_id: str
    state: str
    last_checkpoint_generation: int | None


class InitOwnershipError(Exception):
    """Another live container owns this sandbox initialization."""


class DynamoSandboxStateStore:
    def __init__(self, client: Any, table_name: str) -> None:
        if not table_name:
            raise ValueError("SANDBOX_TABLE is required.")
        self._client = client
        self._table_name = table_name

    def read(self, sandbox_id: str) -> SandboxPersistenceMetadata:
        response = self._client.get_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            ConsistentRead=True,
            ProjectionExpression="runtimeSessionId, #state, lastCheckpointGeneration",
            ExpressionAttributeNames={"#state": "state"},
        )
        item = cast(Mapping[str, Mapping[str, str]], response.get("Item", {}))
        runtime_session_id = item.get("runtimeSessionId", {}).get("S")
        state = item.get("state", {}).get("S")
        raw_generation = item.get("lastCheckpointGeneration", {}).get("N")
        if not runtime_session_id or not state:
            raise SessionInitializationError("Sandbox persistence metadata is unavailable.")
        try:
            generation = int(raw_generation) if raw_generation is not None else None
        except ValueError as error:
            raise SessionInitializationError("Sandbox checkpoint generation is invalid.") from error
        if generation is not None and generation <= 0:
            raise SessionInitializationError("Sandbox checkpoint generation is invalid.")
        return SandboxPersistenceMetadata(runtime_session_id, state, generation)

    def acquire_init(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        init_owner: str,
        *,
        ttl_seconds: int = 90,
    ) -> None:
        """Claim exclusive initialization ownership for this container.

        Cold-start initialization is a long transaction (restore plus gateway
        startup). The claim is a heartbeat-extended lease so a replacement
        container can take over only once the previous owner is provably
        dead, and nothing else may reset the record underneath a live owner.
        """
        now = int(datetime.now(UTC).timestamp())
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=self._key(sandbox_id),
                UpdateExpression=(
                    "SET #state = :restoring, initOwner = :owner, "
                    "initExpiresAt = :expires, updatedAt = :updated "
                    "ADD stateVersion :one"
                ),
                ConditionExpression=(
                    "runtimeSessionId = :session "
                    "AND #state IN (:starting, :restoring) "
                    "AND (attribute_not_exists(initOwner) "
                    "OR initOwner = :owner OR initExpiresAt < :now)"
                ),
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":expires": {"N": str(now + ttl_seconds)},
                    ":now": {"N": str(now)},
                    ":one": {"N": "1"},
                    ":owner": {"S": init_owner},
                    ":restoring": {"S": "RESTORING"},
                    ":session": {"S": runtime_session_id},
                    ":starting": {"S": "STARTING"},
                    ":updated": {"S": _timestamp(datetime.now(UTC))},
                },
            )
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "ConditionalCheckFailedException":
                raise InitOwnershipError(
                    "Another container owns this sandbox initialization."
                ) from error
            raise

    def heartbeat_init(
        self,
        sandbox_id: str,
        init_owner: str,
        *,
        ttl_seconds: int = 90,
    ) -> None:
        now = int(datetime.now(UTC).timestamp())
        self._client.update_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            UpdateExpression="SET initExpiresAt = :expires, updatedAt = :updated",
            ConditionExpression="initOwner = :owner",
            ExpressionAttributeValues={
                ":expires": {"N": str(now + ttl_seconds)},
                ":owner": {"S": init_owner},
                ":updated": {"S": _timestamp(datetime.now(UTC))},
            },
        )

    def heal_ready(self, sandbox_id: str, runtime_session_id: str) -> None:
        """Flip a reconnect-induced STARTING back to READY.

        The control plane marks the record STARTING whenever the lease is
        (re)acquired, but a warm runtime session short-circuits
        initialization and nothing else would ever publish READY again —
        leaving the browser stuck on the starting view forever.
        """
        self._update_state(
            sandbox_id,
            runtime_session_id,
            "READY",
            allowed_states=("STARTING",),
        )

    def mark_ready(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        report: RestoreReport,
        init_owner: str,
    ) -> None:
        # Ownership, not the state value, authorizes publishing READY: a
        # lease reclaim may have flipped the state to STARTING mid-flight.
        self._client.update_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            UpdateExpression=(
                "SET #state = :ready, lastRestore = :restore, updatedAt = :updated "
                "ADD stateVersion :one REMOVE initOwner, initExpiresAt"
            ),
            ConditionExpression=("runtimeSessionId = :session AND initOwner = :owner"),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":one": {"N": "1"},
                ":owner": {"S": init_owner},
                ":ready": {"S": "READY"},
                ":restore": {"S": report.outcome.upper()},
                ":session": {"S": runtime_session_id},
                ":updated": {"S": _timestamp(report.completed_at)},
            },
        )

    def mark_error(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        failure_type: str = "UNKNOWN",
        failure_detail: str = "UNKNOWN",
        upstream_exception: str = "UNKNOWN",
        init_owner: str | None = None,
    ) -> None:
        # Only the initialization owner may poison the record: a container
        # that lost the ownership race must walk away silently instead of
        # breaking the winner's restore mid-flight.
        condition = "runtimeSessionId = :session AND #state IN (:starting, :restoring)"
        values = {
            ":detail": {"S": failure_detail},
            ":error": {"S": "ERROR"},
            ":failure": {"S": failure_type},
            ":one": {"N": "1"},
            ":restoring": {"S": "RESTORING"},
            ":upstream": {"S": upstream_exception},
            ":session": {"S": runtime_session_id},
            ":starting": {"S": "STARTING"},
            ":updated": {"S": _timestamp(datetime.now(UTC))},
        }
        if init_owner is not None:
            condition = "runtimeSessionId = :session AND initOwner = :owner"
            values = {
                key: value
                for key, value in values.items()
                if key not in (":starting", ":restoring")
            }
            values[":owner"] = {"S": init_owner}
        self._client.update_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            UpdateExpression=(
                "SET #state = :error, lastInitializationFailure = :failure, "
                "lastInitializationFailureDetail = :detail, "
                "lastInitializationFailureException = :upstream, updatedAt = :updated "
                "ADD stateVersion :one REMOVE initOwner, initExpiresAt"
            ),
            ConditionExpression=condition,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
        )

    def record_checkpoint(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        generation: int,
        manifest_digest: str,
    ) -> None:
        self._client.update_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            UpdateExpression=(
                "SET #state = :stopping, lastCheckpointGeneration = :generation, "
                "lastCheckpointManifestDigest = :digest, updatedAt = :updated "
                "ADD stateVersion :one"
            ),
            ConditionExpression=(
                "runtimeSessionId = :session AND #state IN (:ready, :checkpointing) AND "
                "(attribute_not_exists(lastCheckpointGeneration) OR "
                "lastCheckpointGeneration < :generation)"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={
                ":checkpointing": {"S": "CHECKPOINTING"},
                ":digest": {"S": manifest_digest},
                ":generation": {"N": str(generation)},
                ":one": {"N": "1"},
                ":ready": {"S": "READY"},
                ":session": {"S": runtime_session_id},
                ":stopping": {"S": "STOPPING"},
                ":updated": {"S": _timestamp(datetime.now(UTC))},
            },
        )

    def _update_state(
        self,
        sandbox_id: str,
        runtime_session_id: str,
        state: str,
        *,
        allowed_states: tuple[str, ...],
    ) -> None:
        values: dict[str, Mapping[str, str]] = {
            ":one": {"N": "1"},
            ":session": {"S": runtime_session_id},
            ":state": {"S": state},
            ":updated": {"S": _timestamp(datetime.now(UTC))},
        }
        allowed_names: list[str] = []
        for index, allowed in enumerate(allowed_states):
            name = f":allowed{index}"
            allowed_names.append(name)
            values[name] = {"S": allowed}
        self._client.update_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            UpdateExpression=("SET #state = :state, updatedAt = :updated ADD stateVersion :one"),
            ConditionExpression=(
                f"runtimeSessionId = :session AND #state IN ({', '.join(allowed_names)})"
            ),
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues=values,
        )

    @staticmethod
    def _key(sandbox_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}}


def _organization_login(payload: Mapping[str, object]) -> OrganizationLogin | None:
    method = payload.get("method", "builder-id")
    if method in {"builder-id", None}:
        return None
    if method != "sso":
        raise AdapterRequestError(
            400,
            "INVALID_MESSAGE",
            "KIRO_AUTH",
            "The Kiro login method must be builder-id or sso.",
        )
    start_url = payload.get("startUrl")
    region = payload.get("region")
    if not isinstance(start_url, str) or not isinstance(region, str):
        raise AdapterRequestError(
            400,
            "INVALID_MESSAGE",
            "KIRO_AUTH",
            "SSO login requires startUrl and region.",
        )
    try:
        return OrganizationLogin(start_url, region)
    except ValueError as error:
        raise AdapterRequestError(
            400,
            "INVALID_MESSAGE",
            "KIRO_AUTH",
            str(error),
        ) from error


class ProductionRuntimeBackend:
    def __init__(
        self,
        loopback: LoopbackKiroCrewBackend,
        readiness: RuntimeReadiness,
        identity: KiroIdentityManager | None = None,
        *,
        busy_probe_ttl_seconds: float = 10.0,
        busy_max_seconds: float = 14400.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loopback = loopback
        self._readiness = readiness
        self._identity = identity
        self._checkpoint_engine: CheckpointEngine | None = None
        self._store: BrokeredCheckpointStore | None = None
        self._broker_client: LambdaBrokerClient | None = None
        self._checkpoint_lock = asyncio.Lock()
        self._durability_tasks: set[asyncio.Task[bool]] = set()
        # Background-activity keepalive: /ping reports HealthyBusy while
        # unattended work (task runner, subagents, workflows) is running,
        # which is what keeps AgentCore from idle-reclaiming a working
        # sandbox whose browser has disconnected.
        self._busy_probe_ttl = busy_probe_ttl_seconds
        self._busy_max = busy_max_seconds
        self._monotonic = monotonic
        self._busy_cache: tuple[float, bool] | None = None
        self._busy_since: float | None = None
        self._was_busy = False
        self._fuse_logged = False

    def configure(
        self,
        checkpoint_engine: CheckpointEngine,
        store: BrokeredCheckpointStore,
        broker_client: LambdaBrokerClient,
    ) -> None:
        if self._checkpoint_engine is not None:
            raise SessionInitializationError("Runtime checkpoint backend is already configured.")
        self._checkpoint_engine = checkpoint_engine
        self._store = store
        self._broker_client = broker_client

    async def execute(
        self,
        operation: str,
        request_id: str,
        payload: Mapping[str, object],
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        if operation in {"kiro.login.start", "kiro.status", "kiro.logout"}:
            if self._identity is None:
                raise AdapterRequestError(
                    501,
                    "FEATURE_UNAVAILABLE_IN_AGENTCORE",
                    "KIRO_AUTH",
                    "Interactive Kiro authentication is not configured.",
                )
            del request_id
            if operation == "kiro.status":
                status = await asyncio.to_thread(self._identity.status)
                yield "kiro.auth_status", status.payload()
                return
            if operation == "kiro.logout":
                status = await self._identity.logout()
                # The sign-out mutated the credential store; persist it so a
                # restored sandbox does not resurrect the abandoned sign-in.
                self._schedule_durability_checkpoint("kiro.logout")
                yield "kiro.auth_status", status.payload()
                return
            organization = _organization_login(payload)
            status = await asyncio.to_thread(self._identity.status)
            if status.state is KiroAuthState.AUTHENTICATED:
                yield "kiro.authenticated", status.payload()
                return
            # A new explicit login supersedes any stalled or abandoned flow.
            await self._identity.cancel_device_flow()
            authenticated = False
            async for event in self._identity.device_flow_events(organization):
                if event[0] == "kiro.authenticated":
                    authenticated = True
                yield event
            if authenticated:
                # Persist the fresh sign-in immediately: without this, a
                # sandbox reclaimed before the next Stop safely would restore
                # to a generation that predates the login.
                self._schedule_durability_checkpoint("kiro.login")
            return
        if operation != "sandbox.prepare_stop":
            async for event in self._loopback.execute(operation, request_id, payload):
                yield event
            return
        del request_id, payload
        try:
            receipt, checkpoint_receipt = await self._commit_checkpoint(final=True)
        except Exception as error:
            self._readiness.read_only = True
            raise AdapterRequestError(
                503,
                "CHECKPOINT_FAILED",
                "PERSISTENCE",
                "The final checkpoint could not be committed.",
                retryable=True,
            ) from error
        yield (
            "checkpoint.committed",
            {
                "checkpointReceipt": checkpoint_receipt,
                "generation": receipt.generation,
                "manifestDigest": receipt.manifest_digest,
            },
        )
        yield "request.completed", {"checkpointCommitted": True}

    async def _commit_checkpoint(self, *, final: bool) -> tuple[CheckpointReceipt, str]:
        async with self._checkpoint_lock:
            engine, store, broker_client = self._checkpoint_context()
            generation = (store.latest_committed() or 0) + 1
            receipt = await asyncio.to_thread(engine.checkpoint, generation, final=final)
            checkpoint_receipt = await asyncio.to_thread(
                broker_client.checkpoint_receipt,
                receipt.generation,
                receipt.manifest_digest,
                final=final,
            )
            return receipt, checkpoint_receipt

    def _schedule_durability_checkpoint(self, reason: str) -> None:
        if self._checkpoint_engine is None:
            # Not configured (initialization still in flight); the state
            # remains journaled for the next committed generation.
            return
        task = asyncio.get_running_loop().create_task(self._durability_checkpoint(reason))
        self._durability_tasks.add(task)
        task.add_done_callback(self._durability_tasks.discard)

    async def _durability_checkpoint(self, reason: str) -> bool:
        try:
            receipt, _ = await self._commit_checkpoint(final=False)
        except Exception:
            # Best effort: the session keeps working and the sign-in stays
            # usable; the next checkpoint attempt captures the same state.
            _LOGGER.warning("Durability checkpoint after %s failed.", reason, exc_info=True)
            return False
        _LOGGER.info(
            "Durability checkpoint after %s committed generation %d.",
            reason,
            receipt.generation,
        )
        return True

    async def background_busy(self) -> bool:
        """Report whether unattended work is still running in the sandbox.

        Drives the /ping status: HealthyBusy keeps AgentCore from
        idle-reclaiming a sandbox whose task runner, subagents, or
        workflows are still working after the browser disconnected.
        Probe failures count as idle: a broken gateway must be reclaimable,
        never immortal.
        """
        if self._busy_probe_ttl <= 0:
            return False
        now = self._monotonic()
        if self._busy_cache is not None and now - self._busy_cache[0] < self._busy_probe_ttl:
            busy = self._busy_cache[1]
        else:
            try:
                busy = await asyncio.wait_for(self._probe_background_activity(), 2.5)
            except Exception:
                busy = False
            self._busy_cache = (now, busy)
        if busy:
            if self._busy_since is None:
                self._busy_since = now
            elif self._busy_max > 0 and now - self._busy_since > self._busy_max:
                # Fuse: a task stuck busy forever must not pin the microVM
                # until MaxLifetime. Report idle so the platform reclaims it;
                # the idle-transition checkpoint below preserves the state.
                if not self._fuse_logged:
                    _LOGGER.warning(
                        "Background work busy for over %.0f seconds; reporting idle.",
                        self._busy_max,
                    )
                    self._fuse_logged = True
                busy = False
        else:
            self._busy_since = None
            self._fuse_logged = False
        if self._was_busy and not busy:
            # The sandbox just went quiet. Commit now instead of waiting for
            # the periodic interval: the platform may reclaim the session at
            # any point after this answer.
            self._schedule_durability_checkpoint("background-idle")
        self._was_busy = busy
        return busy

    async def _probe_background_activity(self) -> bool:
        with contextlib.suppress(Exception):
            data = await self._loopback.fetch_json("/api/taskrunner")
            if isinstance(data, Mapping):
                runs = data.get("runs")
                if isinstance(runs, list) and any(
                    isinstance(run, Mapping) and run.get("running") for run in runs
                ):
                    return True
        with contextlib.suppress(Exception):
            status = await self._loopback.fetch_json("/api/status")
            if isinstance(status, Mapping):
                subagents = status.get("subagents")
                if isinstance(subagents, int) and subagents > 0:
                    return True
        with contextlib.suppress(Exception):
            workflows = await self._loopback.fetch_json("/api/workflows/runs")
            if isinstance(workflows, Mapping):
                runs = workflows.get("runs")
                if isinstance(runs, list) and any(
                    isinstance(run, Mapping) and run.get("status") == "running" for run in runs
                ):
                    return True
        return False

    async def run_periodic_checkpoints(
        self,
        interval_seconds: float,
        *,
        fingerprint: Callable[[], str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Commit a durability checkpoint whenever the workspace changed.

        Losses from a reclaimed session are bounded by *interval_seconds*.
        The optional *fingerprint* callable lets idle intervals skip the
        checkpoint entirely, so a quiet sandbox never pauses its gateway.
        """
        if interval_seconds <= 0:
            raise ValueError("Checkpoint interval must be positive.")
        last_committed: str | None = None
        while True:
            await sleep(interval_seconds)
            if self._checkpoint_engine is None or self._readiness.read_only:
                continue
            current = None
            if fingerprint is not None:
                current = await asyncio.to_thread(fingerprint)
                if current == last_committed:
                    continue
            if await self._durability_checkpoint("periodic-interval"):
                last_committed = current

    async def checkpoint_on_shutdown(self, *, timeout_seconds: float = 15.0) -> None:
        """Best-effort final checkpoint when the runtime is being reclaimed.

        A graceful SIGTERM is the only notice an idle-reclaimed session gets;
        anything committed here survives the loss of managed session storage.
        """
        if self._checkpoint_engine is None:
            return
        try:
            receipt, _ = await asyncio.wait_for(
                self._commit_checkpoint(final=True), timeout_seconds
            )
        except Exception:
            _LOGGER.warning("Final checkpoint on shutdown did not commit.", exc_info=True)
            return
        _LOGGER.info("Shutdown checkpoint committed generation %d.", receipt.generation)

    async def cancel(self, request_id: str) -> bool:
        return await self._loopback.cancel(request_id)

    def _checkpoint_context(
        self,
    ) -> tuple[CheckpointEngine, BrokeredCheckpointStore, LambdaBrokerClient]:
        if self._checkpoint_engine is None or self._store is None or self._broker_client is None:
            raise SessionInitializationError("Runtime checkpoint backend is not initialized.")
        return self._checkpoint_engine, self._store, self._broker_client


class AwsSessionInitializer:
    def __init__(
        self,
        lambda_client: Any,
        broker_function_arn: str,
        state_store: DynamoSandboxStateStore,
        workspace: Path,
        metadata: RuntimeMetadata,
        supervisor: KiroCrewSupervisor,
        backend: ProductionRuntimeBackend,
        readiness: RuntimeReadiness,
        *,
        startup_timeout_seconds: float = 60.0,
    ) -> None:
        if not broker_function_arn or startup_timeout_seconds <= 0:
            raise ValueError("Persistence broker ARN and positive startup timeout are required.")
        self._lambda_client = lambda_client
        self._broker_function_arn = broker_function_arn
        self._state_store = state_store
        self._workspace = workspace
        self._metadata = metadata
        self._supervisor = supervisor
        self._backend = backend
        self._readiness = readiness
        self._startup_timeout = startup_timeout_seconds
        self._lock = asyncio.Lock()
        self._initialized_session: tuple[str, str, str] | None = None
        self._init_owner = str(uuid.uuid4())
        self._broker_client: LambdaBrokerClient | None = None

    async def initialize(
        self,
        cognito_subject: str,
        binding_token: str,
        claims: BindingClaims,
    ) -> None:
        identity = (cognito_subject, claims.sandbox_id, claims.runtime_session_id)
        async with self._lock:
            if self._initialized_session is not None:
                if self._initialized_session != identity or self._broker_client is None:
                    raise SessionInitializationError(
                        "Runtime process is already bound to another sandbox session."
                    )
                self._broker_client.binding_token = binding_token
                if self._readiness.healthy:
                    # A lease reclaim flipped the record to STARTING; this warm
                    # session is the only party that can publish READY again.
                    try:
                        await asyncio.to_thread(
                            self._state_store.heal_ready,
                            claims.sandbox_id,
                            claims.runtime_session_id,
                        )
                    except ClientError as error:
                        code = error.response.get("Error", {}).get("Code")
                        if code != "ConditionalCheckFailedException":
                            raise
                return
            self._readiness.restore_complete = False
            self._readiness.loopback_ready = False
            self._readiness.read_only = True
            self._readiness.initializing = True
            heartbeat = asyncio.create_task(self._init_heartbeat(claims.sandbox_id))
            try:
                broker_client, report, store, engine = await asyncio.to_thread(
                    self._initialize_sync,
                    binding_token,
                    claims,
                )
                self._backend.configure(
                    engine,
                    store,
                    broker_client,
                )
                self._broker_client = broker_client
                self._initialized_session = identity
                self._readiness.restore_complete = True
                self._readiness.loopback_ready = self._supervisor.ready
                self._readiness.read_only = False
                if not self._readiness.healthy or report.outcome == "failed":
                    raise SessionInitializationError("Sandbox did not become ready.")
            except InitOwnershipError as error:
                # A live owner is initializing elsewhere; walk away without
                # touching the record and let the client retry against it.
                self._readiness.read_only = True
                raise SessionInitializationError(
                    "Sandbox initialization is in progress elsewhere."
                ) from error
            except Exception as error:
                _LOGGER.error(
                    "Sandbox initialization failed (%s): %s",
                    type(error).__name__,
                    error,
                )
                self._readiness.read_only = True
                try:
                    await asyncio.to_thread(
                        self._state_store.mark_error,
                        claims.sandbox_id,
                        claims.runtime_session_id,
                        type(error).__name__,
                        (
                            error.diagnostic_code
                            if isinstance(error, GatewayExitedError)
                            else "UNKNOWN"
                        ),
                        (
                            error.exception_type
                            if isinstance(error, GatewayExitedError)
                            else "UNKNOWN"
                        ),
                        self._init_owner,
                    )
                except Exception as state_error:
                    error.add_note(
                        f"Sandbox ERROR lifecycle update failed: {type(state_error).__name__}"
                    )
                self._supervisor.terminate()
                if isinstance(error, SessionInitializationError):
                    raise
                raise SessionInitializationError(
                    "Authoritative sandbox restore or gateway startup failed."
                ) from error
            finally:
                self._readiness.initializing = False
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def _init_heartbeat(self, sandbox_id: str) -> None:
        """Extend the initialization lease while restore and startup run."""
        while True:
            await asyncio.sleep(20)
            try:
                await asyncio.to_thread(
                    self._state_store.heartbeat_init,
                    sandbox_id,
                    self._init_owner,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Losing one beat is survivable; the lease lasts 90 seconds.
                _LOGGER.warning("Initialization heartbeat failed.", exc_info=True)

    def _evict_legacy_model_cache(self) -> None:
        """Delete the in-workspace embedding-model copy older checkpoints carry.

        The model ships in the image now (KIROCREW_EMBED_MODEL_PATH), so a
        restored ~/.kiro/crew/models directory is ~640MB of dead weight
        inside the 1GB session-storage quota - enough to break kiro-cli
        sign-in with ENOSPC. New checkpoints already exclude the directory;
        this reclaims the space for sandboxes restored from older ones.
        """
        legacy = self._workspace / "home" / ".kiro" / "crew" / "models"
        if not legacy.is_dir() or legacy.is_symlink():
            return
        shutil.rmtree(legacy, ignore_errors=True)
        _LOGGER.info("Evicted the legacy in-workspace embedding model cache.")

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
        metadata = self._state_store.read(claims.sandbox_id)
        if metadata.runtime_session_id != claims.runtime_session_id:
            raise SessionInitializationError("Sandbox runtime session changed.")
        self._state_store.acquire_init(
            claims.sandbox_id, claims.runtime_session_id, self._init_owner
        )
        client = LambdaBrokerClient(
            self._lambda_client,
            self._broker_function_arn,
            claims.sandbox_id,
            claims.runtime_session_id,
            binding_token,
        )
        broker = LambdaPersistenceBroker(client, claims.sandbox_id)
        store = BrokeredCheckpointStore(broker, claims.sandbox_id)
        cipher = broker.cipher(claims.sandbox_id)
        committed = store.committed_generations()
        expected = metadata.last_checkpoint_generation
        # The durable store may be AHEAD of the recorded pointer: a background
        # checkpoint can commit its generation to S3 and die before the
        # receipt lands in DynamoDB. Restoring the newer committed generation
        # is safe; only a store BEHIND the pointer indicates data loss.
        if expected is not None and (not committed or committed[-1].generation < expected):
            raise RestoreError("PERSISTENCE_RESTORE_FAILED")
        report = RestoreEngine(
            self._workspace,
            claims.sandbox_id,
            self._metadata.kirocrew_version,
            store,
            cipher,
        ).restore(existing_sandbox=expected is not None)
        _LOGGER.info(
            "Restore outcome=%s generation=%s fallback=%s attempts=%s reasons=%s",
            report.outcome,
            report.generation,
            report.fallback_used,
            list(report.attempted_generations),
            list(report.reasons),
        )
        self._evict_legacy_model_cache()
        self._supervisor.start(timeout_seconds=self._startup_timeout)
        if not self._supervisor.ready:
            raise SessionInitializationError("Loopback gateway did not become ready.")
        checkpoint_engine = CheckpointEngine(
            self._workspace,
            claims.sandbox_id,
            DirtyJournal(self._workspace),
            ManifestBuilder(self._workspace),
            cipher,
            store,
            self._supervisor,
            StagedStateFlusher(self._supervisor),
            runtime_version=self._metadata.kirocrew_version,
        )
        self._state_store.mark_ready(
            claims.sandbox_id,
            claims.runtime_session_id,
            report,
            self._init_owner,
        )
        return client, report, store, checkpoint_engine


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name, "")
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def build_runtime_application(
    environment: Mapping[str, str] = os.environ,
) -> web.Application:
    image = ImageMetadata.from_environment(environment)
    metadata = RuntimeMetadata(
        image.kirocrew_version,
        image.kirocrew_artifact_sha256,
        image.protocol_version,
    )
    workspace = Path(environment.get("WORKSPACE_ROOT", "/mnt/workspace"))
    supervisor = KiroCrewSupervisor(WorkspaceLayout(workspace), metadata)

    region = _required(environment, "AWS_REGION")
    session = boto3.Session(region_name=region)
    kms = session.client("kms")
    dynamodb = session.client("dynamodb")
    lambda_client = session.client("lambda")
    binding_verifier = KmsBindingVerifier(
        AwsKmsSignatureVerifier(kms, _required(environment, "BINDING_KEY_ARN")),
        _required(environment, "BINDING_AUDIENCE"),
        _required(environment, "COGNITO_ISSUER"),
    )
    table_name = _required(environment, "SANDBOX_TABLE")
    lease_authorizer = DynamoLeaseAuthorizer(dynamodb, table_name)
    state_store = DynamoSandboxStateStore(dynamodb, table_name)
    loopback_session = ClientSession()
    loopback = LoopbackKiroCrewBackend(loopback_session, supervisor)
    readiness = RuntimeReadiness()
    layout = WorkspaceLayout(workspace)
    identity_manager = KiroIdentityManager(
        KiroIdentityConfig(
            mode="device_flow",
            home=layout.home,
            api_key_home=layout.home / ".kiro-api-key-home",
        ),
        LocalKiroCommandRunner(),
    )
    backend = ProductionRuntimeBackend(
        loopback,
        readiness,
        identity=identity_manager,
        # 0 disables the probe; the fuse bounds a stuck-busy task so it cannot
        # pin the microVM until MaxLifetime.
        busy_probe_ttl_seconds=float(environment.get("KIROCREW_BUSY_PROBE_TTL_SECONDS", "10")),
        busy_max_seconds=float(environment.get("KIROCREW_BUSY_MAX_SECONDS", "14400")),
    )
    initializer = AwsSessionInitializer(
        lambda_client,
        _required(environment, "PERSISTENCE_BROKER_ARN"),
        state_store,
        workspace,
        metadata,
        supervisor,
        backend,
        readiness,
        startup_timeout_seconds=float(environment.get("KIROCREW_START_TIMEOUT", "60")),
    )
    adapter = AgentCoreAdapter(
        AdapterConfig(
            _required(environment, "BINDING_AUDIENCE"),
            _required(environment, "COGNITO_ISSUER"),
        ),
        binding_verifier,
        lease_authorizer,
        backend,
        readiness,
        session_initializer=initializer,
    )

    async def cleanup(_application: web.Application) -> None:
        await loopback_session.close()
        supervisor.terminate()

    # Interval-bounded durability (0 disables): a session reclaimed without a
    # graceful stop loses at most this many seconds of workspace mutations.
    checkpoint_interval = float(environment.get("KIROCREW_CHECKPOINT_INTERVAL_SECONDS", "300"))
    periodic_checkpoints: list[asyncio.Task[None]] = []

    async def start_periodic_checkpoints(_application: web.Application) -> None:
        if checkpoint_interval > 0:
            periodic_checkpoints.append(
                asyncio.get_running_loop().create_task(
                    backend.run_periodic_checkpoints(
                        checkpoint_interval,
                        fingerprint=lambda: workspace_fingerprint(workspace),
                    )
                )
            )

    async def shutdown(_application: web.Application) -> None:
        for task in periodic_checkpoints:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await backend.checkpoint_on_shutdown()

    adapter.application.on_startup.append(start_periodic_checkpoints)
    adapter.application.on_shutdown.append(shutdown)
    adapter.application.on_cleanup.append(cleanup)
    return adapter.application


def serve_runtime(environment: Mapping[str, str] = os.environ) -> None:
    web.run_app(
        build_runtime_application(environment),
        host="0.0.0.0",  # noqa: S104  # nosec B104 - AgentCore ingress port.
        port=8080,
    )
