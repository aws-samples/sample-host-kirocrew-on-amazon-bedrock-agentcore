from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from kirocrew_agentcore_control.api import (
    AgentCoreStopError,
    CheckpointMetadata,
    ClaimsValidator,
    ControlConfig,
    InMemoryCheckpointCatalog,
    SandboxControlService,
    SignedControlTokens,
)
from kirocrew_agentcore_control.sandbox import (
    HistoryEvent,
    InMemorySandboxRegistry,
    LeaseConflictError,
    SandboxRecord,
    SandboxState,
    SandboxUnavailableError,
    StartLease,
    StateConflictError,
    init_lease_active,
    new_runtime_session_id,
    new_sandbox_id,
)


class KmsRsaPssSigner:
    def __init__(self, client: Any, key_arn: str) -> None:
        if not key_arn:
            raise ValueError("BINDING_KEY_ARN is required.")
        self._client = client
        self._key_arn = key_arn

    def sign(self, message: bytes) -> bytes:
        response = self._client.sign(
            KeyId=self._key_arn,
            Message=message,
            MessageType="RAW",
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        signature = response.get("Signature")
        if not isinstance(signature, bytes):
            raise RuntimeError("KMS signature is unavailable.")
        return signature

    def verify(self, message: bytes, signature: bytes) -> None:
        response = self._client.verify(
            KeyId=self._key_arn,
            Message=message,
            MessageType="RAW",
            Signature=signature,
            SigningAlgorithm="RSASSA_PSS_SHA_256",
        )
        if response.get("SignatureValid") is not True:
            raise ValueError("KMS signature is invalid.")


class DynamoSandboxRegistry(InMemorySandboxRegistry):
    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        clock: Any = lambda: datetime.now(UTC),
        kirocrew_version: str = "0.2.0",
    ) -> None:
        if not table_name:
            raise ValueError("SANDBOX_TABLE is required.")
        super().__init__()
        self._client = client
        self._table_name = table_name
        self._production_clock = clock
        self._production_version = kirocrew_version

    def get_or_create(self, cognito_subject: str) -> SandboxRecord:
        try:
            return self.get(cognito_subject)
        except SandboxUnavailableError:
            pass
        now = self._production_clock()
        owner_hash = self._owner_hash(cognito_subject)
        record = SandboxRecord(
            owner_hash,
            new_sandbox_id(),
            new_runtime_session_id(),
            SandboxState.STOPPED,
            0,
            None,
            None,
            None,
            None,
            None,
            None,
            "device-flow",
            self._production_version,
            now,
            now,
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._mapping_item(record),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": self._record_item(record),
                            "ConditionExpression": "attribute_not_exists(pk)",
                        }
                    },
                ]
            )
            return record
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "TransactionCanceledException":
                raise
            return self.get(cognito_subject)

    def get(self, cognito_subject: str) -> SandboxRecord:
        owner_hash = self._owner_hash(cognito_subject)
        mapping = self._client.get_item(
            TableName=self._table_name,
            Key={"pk": {"S": f"OWNER#{owner_hash}"}, "sk": {"S": "SANDBOX"}},
            ConsistentRead=True,
        ).get("Item", {})
        sandbox_id = cast(Mapping[str, str], mapping.get("sandboxId", {})).get("S")
        if not sandbox_id:
            raise SandboxUnavailableError("Sandbox is unavailable.")
        item = self._client.get_item(
            TableName=self._table_name,
            Key=self._key(sandbox_id),
            ConsistentRead=True,
        ).get("Item", {})
        if not item:
            raise SandboxUnavailableError("Sandbox is unavailable.")
        return self._record(cast(Mapping[str, Mapping[str, str]], item))

    _HISTORY_TTL_SECONDS = 7 * 24 * 3600

    def record_observation(self, record: SandboxRecord) -> None:
        # One event per state version: the version key makes repeated polls
        # of the same state a cheap conditional no-op. The stored timestamp
        # is the record's updatedAt - the actual transition time - so late
        # observation does not distort the history. Events expire via TTL.
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item={
                    "pk": {"S": f"SANDBOX#{record.sandbox_id}"},
                    "sk": {"S": f"EVENT#{record.state_version:012d}"},
                    "state": {"S": record.state.value},
                    "at": {"S": record.updated_at.isoformat()},
                    "expiresAt": {
                        "N": str(int(record.updated_at.timestamp()) + self._HISTORY_TTL_SECONDS)
                    },
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

    def history(self, sandbox_id: str, limit: int = 20) -> list[HistoryEvent]:
        if limit <= 0:
            raise ValueError("History limit must be positive.")
        response = self._client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"SANDBOX#{sandbox_id}"},
                ":prefix": {"S": "EVENT#"},
            },
            ScanIndexForward=False,
            Limit=limit,
        )
        events: list[HistoryEvent] = []
        for item in cast(list[Mapping[str, Mapping[str, str]]], response.get("Items", [])):
            key = item.get("sk", {}).get("S", "")
            state = item.get("state", {}).get("S", "")
            at = item.get("at", {}).get("S", "")
            if not key.startswith("EVENT#") or not state or not at:
                continue
            events.append(
                HistoryEvent(
                    int(key.removeprefix("EVENT#")),
                    SandboxState(state),
                    datetime.fromisoformat(at),
                )
            )
        return events

    def acquire_start(
        self,
        cognito_subject: str,
        lease_owner: str,
        *,
        ttl: timedelta,
        confirmed_inactive: bool = False,
    ) -> StartLease:
        record = self.get_or_create(cognito_subject)
        now = self._production_clock()
        if record.lease_expires_at is not None and record.lease_expires_at > now:
            return StartLease(record, authoritative=record.lease_owner == lease_owner)
        if init_lease_active(record, now):
            # A container is mid-initialization; leave the record alone.
            return StartLease(record, authoritative=record.lease_owner == lease_owner)
        if (
            record.lease_owner is not None
            and record.lease_owner != lease_owner
            and record.state is not SandboxState.STOPPED
            and not confirmed_inactive
        ):
            raise LeaseConflictError("Expired lease requires authoritative-owner confirmation.")
        # A fresh AgentCore session for every authoritative start: a
        # previous session that died without a clean stop (lost browser,
        # failed checkpoint, stuck microVM) leaves its session id
        # poisoned, and reusing it turns every restart into 424s until
        # the platform reclaims the corpse. The lease owner follows the
        # rotation - the new session owns the sandbox now - otherwise the
        # next start would read a mismatched owner and 409 forever.
        rotated = new_runtime_session_id()
        updated = replace(
            record,
            state=SandboxState.STARTING,
            state_version=record.state_version + 1,
            runtime_session_id=rotated,
            lease_owner=rotated,
            lease_expires_at=now + ttl,
            active_request_id=None,
            updated_at=now,
        )
        self._put_updated(record, updated)
        return StartLease(updated, authoritative=True)

    def transition(
        self,
        cognito_subject: str,
        target: SandboxState,
        *,
        expected_version: int,
        lease_owner: str,
    ) -> SandboxRecord:
        record = self.get(cognito_subject)
        if record.state_version != expected_version or record.lease_owner != lease_owner:
            raise StateConflictError("Sandbox state version or lease changed.")
        if target is not SandboxState.STOPPED or record.state is not SandboxState.STOPPING:
            raise StateConflictError("Lifecycle transition is not allowed.")
        updated = replace(
            record,
            state=target,
            state_version=record.state_version + 1,
            lease_owner=None,
            lease_expires_at=None,
            active_request_id=None,
            updated_at=self._production_clock(),
        )
        self._put_updated(record, updated)
        return updated

    def request_deletion(self, cognito_subject: str) -> SandboxRecord:
        record = self.get(cognito_subject)
        if record.state is not SandboxState.STOPPED:
            raise StateConflictError("Sandbox must be stopped before deletion.")
        updated = replace(
            record,
            state_version=record.state_version + 1,
            deletion_state="REQUESTED",
            updated_at=self._production_clock(),
        )
        self._put_updated(record, updated)
        return updated

    def _put_updated(self, previous: SandboxRecord, updated: SandboxRecord) -> None:
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=self._record_item(updated),
                ConditionExpression="stateVersion = :version",
                ExpressionAttributeValues={":version": {"N": str(previous.state_version)}},
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise StateConflictError("Sandbox state version changed.") from error
            raise

    @staticmethod
    def _owner_hash(subject: str) -> str:
        if not subject:
            raise SandboxUnavailableError("Sandbox is unavailable.")
        return hashlib.sha256(f"kirocrew-owner-v1\x00{subject}".encode()).hexdigest()

    @staticmethod
    def _key(sandbox_id: str) -> dict[str, dict[str, str]]:
        return {"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}}

    @staticmethod
    def _mapping_item(record: SandboxRecord) -> dict[str, dict[str, str]]:
        return {
            "pk": {"S": f"OWNER#{record.owner_hash}"},
            "sk": {"S": "SANDBOX"},
            "sandboxId": {"S": record.sandbox_id},
            "runtimeSessionId": {"S": record.runtime_session_id},
            "createdAt": {"S": _timestamp(record.created_at)},
            "updatedAt": {"S": _timestamp(record.updated_at)},
        }

    @staticmethod
    def _record_item(record: SandboxRecord) -> dict[str, dict[str, str]]:
        item = {
            "pk": {"S": f"SANDBOX#{record.sandbox_id}"},
            "sk": {"S": "METADATA"},
            "ownerHash": {"S": record.owner_hash},
            "runtimeSessionId": {"S": record.runtime_session_id},
            "state": {"S": record.state.value},
            "stateVersion": {"N": str(record.state_version)},
            "kiroAuthMode": {"S": record.kiro_auth_mode},
            "kiroCrewVersion": {"S": record.kirocrew_version},
            "createdAt": {"S": _timestamp(record.created_at)},
            "updatedAt": {"S": _timestamp(record.updated_at)},
        }
        optional = {
            "leaseOwner": ("S", record.lease_owner),
            "leaseExpiresAt": (
                "S",
                _timestamp(record.lease_expires_at)
                if record.lease_expires_at is not None
                else None,
            ),
            "activeRequestId": ("S", record.active_request_id),
            "lastCheckpointGeneration": (
                "N",
                str(record.last_checkpoint_generation)
                if record.last_checkpoint_generation is not None
                else None,
            ),
            "initExpiresAt": (
                "N",
                str(int(record.init_expires_at.timestamp()))
                if record.init_expires_at is not None
                else None,
            ),
            "lastCheckpointManifestDigest": ("S", None),
            "lastRestore": ("S", record.last_restore),
            "deletionState": ("S", record.deletion_state),
        }
        for name, (kind, value) in optional.items():
            if value is not None:
                item[name] = {kind: value}
        return item

    @staticmethod
    def _record(item: Mapping[str, Mapping[str, str]]) -> SandboxRecord:
        def required(name: str, kind: str = "S") -> str:
            value = item.get(name, {}).get(kind)
            if value is None:
                raise SandboxUnavailableError("Sandbox metadata is invalid.")
            return value

        def optional_time(name: str) -> datetime | None:
            value = item.get(name, {}).get("S")
            return _datetime(value) if value is not None else None

        generation = item.get("lastCheckpointGeneration", {}).get("N")
        init_expires_raw = item.get("initExpiresAt", {}).get("N")
        init_expires = (
            datetime.fromtimestamp(int(init_expires_raw), tz=UTC)
            if init_expires_raw is not None
            else None
        )
        try:
            return SandboxRecord(
                required("ownerHash"),
                required("pk").removeprefix("SANDBOX#"),
                required("runtimeSessionId"),
                SandboxState(required("state")),
                int(required("stateVersion", "N")),
                item.get("leaseOwner", {}).get("S"),
                optional_time("leaseExpiresAt"),
                item.get("activeRequestId", {}).get("S"),
                int(generation) if generation is not None else None,
                item.get("lastRestore", {}).get("S"),
                item.get("deletionState", {}).get("S"),
                required("kiroAuthMode"),
                required("kiroCrewVersion"),
                _datetime(required("createdAt")),
                _datetime(required("updatedAt")),
                init_expires,
            )
        except (ValueError, TypeError) as error:
            raise SandboxUnavailableError("Sandbox metadata is invalid.") from error


class DynamoCheckpointCatalog(InMemoryCheckpointCatalog):
    def __init__(self, client: Any, table_name: str) -> None:
        super().__init__()
        self._client = client
        self._table_name = table_name

    def get(self, sandbox_id: str, generation: int) -> CheckpointMetadata | None:
        item = self._client.get_item(
            TableName=self._table_name,
            Key={
                "pk": {"S": f"SANDBOX#{sandbox_id}"},
                "sk": {"S": f"CHECKPOINT#{generation:020d}"},
            },
            ConsistentRead=True,
        ).get("Item", {})
        return self._metadata(item) if item else None

    def list(self, sandbox_id: str) -> tuple[CheckpointMetadata, ...]:
        response = self._client.query(
            TableName=self._table_name,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :checkpoint)",
            ExpressionAttributeValues={
                ":pk": {"S": f"SANDBOX#{sandbox_id}"},
                ":checkpoint": {"S": "CHECKPOINT#"},
            },
            ConsistentRead=True,
        )
        items = cast(list[Mapping[str, Mapping[str, str]]], response.get("Items", []))
        return tuple(
            sorted((self._metadata(item) for item in items), key=lambda item: item.generation)
        )

    @staticmethod
    def _metadata(item: Mapping[str, Mapping[str, str]]) -> CheckpointMetadata:
        try:
            status = item["status"]["S"]
            if status not in {"COMMITTED", "RESTORE_FAILED"}:
                raise ValueError
            return CheckpointMetadata(
                int(item["generation"]["N"]),
                item["manifestDigest"]["S"],
                _datetime(item["createdAt"]["S"]),
                cast(Any, status),
                int(item.get("schemaVersion", {"N": "1"})["N"]),
            )
        except (KeyError, ValueError) as error:
            raise ValueError("Checkpoint metadata is invalid.") from error


class BotoAgentCoreStopper:
    def __init__(self, client: Any, runtime_arn: str, qualifier: str) -> None:
        self._client = client
        self._runtime_arn = runtime_arn
        self._qualifier = qualifier

    def stop(self, runtime_session_id: str) -> None:
        try:
            self._client.stop_runtime_session(
                agentRuntimeArn=self._runtime_arn,
                runtimeSessionId=runtime_session_id,
                qualifier=self._qualifier,
            )
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)
            raise AgentCoreStopError(int(status)) from error


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp timezone is required.")
    return parsed


def _build_service() -> SandboxControlService:
    region = _required("REGION")
    runtime_arn = _required("RUNTIME_ARN")
    qualifier = _required("RUNTIME_QUALIFIER")
    encoded_arn = quote(runtime_arn, safe="")
    invocation_path = f"/runtimes/{encoded_arn}/invocations?qualifier={quote(qualifier)}"
    http_url = f"https://bedrock-agentcore.{region}.amazonaws.com{invocation_path}"
    # The data plane serves WebSocket upgrades at /ws, not /invocations
    # (which only accepts POST and answers an upgrade with 405).
    websocket_path = f"/runtimes/{encoded_arn}/ws?qualifier={quote(qualifier)}"
    websocket_url = f"wss://bedrock-agentcore.{region}.amazonaws.com{websocket_path}"
    session = boto3.Session(region_name=region)
    dynamodb = session.client("dynamodb")
    kms = session.client("kms")
    agentcore = session.client("bedrock-agentcore")
    table_name = _required("SANDBOX_TABLE")
    config = ControlConfig(
        _required("ISSUER"),
        _required("APP_CLIENT_ID"),
        _required("ALLOWED_ORIGIN"),
        region,
        runtime_arn,
        qualifier,
        http_url,
        websocket_url,
        cast(Any, _required("DEPLOYMENT_MODE")),
        _required("FRONTEND_COMPATIBILITY_VERSION"),
        _required("BINDING_AUDIENCE"),
        persisted_paths=tuple(cast(list[str], json.loads(os.environ.get("PERSISTED_PATHS", "[]")))),
    )
    return SandboxControlService(
        config,
        DynamoSandboxRegistry(dynamodb, table_name),
        ClaimsValidator(
            config.issuer,
            config.app_client_id,
            required_scope=os.environ.get("REQUIRED_SCOPE", "aws.cognito.signin.user.admin"),
        ),
        SignedControlTokens(
            KmsRsaPssSigner(kms, _required("BINDING_KEY_ARN")),
            config.token_audience,
        ),
        DynamoCheckpointCatalog(dynamodb, table_name),
        BotoAgentCoreStopper(agentcore, runtime_arn, qualifier),
    )


_SERVICE: SandboxControlService | None = None


def handler(event: Mapping[str, object], _context: object) -> dict[str, object]:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service()
    return _SERVICE.handle(event)
