from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast
from urllib.parse import urlsplit

from kirocrew_agentcore_persistence.crypto import SandboxCipher
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    PresignedOperation,
    StorageOperation,
)


class LambdaBrokerClient:
    def __init__(
        self,
        client: Any,
        function_arn: str,
        sandbox_id: str,
        runtime_session_id: str,
        binding_token: str,
    ) -> None:
        if not all((function_arn, sandbox_id, runtime_session_id, binding_token)):
            raise ValueError("Complete persistence broker binding is required.")
        self._client = client
        self._function_arn = function_arn
        self._sandbox_id = sandbox_id
        self._runtime_session_id = runtime_session_id
        self.binding_token = binding_token

    def call(self, operation: str, **values: object) -> dict[str, object]:
        request = {
            "bindingToken": self.binding_token,
            "operation": operation,
            "runtimeSessionId": self._runtime_session_id,
            "sandboxId": self._sandbox_id,
            **values,
        }
        response = self._client.invoke(
            FunctionName=self._function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(request, separators=(",", ":")).encode(),
        )
        payload = response["Payload"].read()
        if response.get("FunctionError"):
            raise BrokerAuthorizationError("Persistence broker rejected the operation.")
        value = cast(object, json.loads(payload))
        if not isinstance(value, dict):
            raise BrokerAuthorizationError("Persistence broker response is invalid.")
        return cast(dict[str, object], value)

    def checkpoint_receipt(
        self, generation: int, manifest_digest: str, *, final: bool = True
    ) -> str:
        value = self.call(
            "checkpointReceipt",
            final=final,
            generation=generation,
            manifestDigest=manifest_digest,
        )
        receipt = value.get("checkpointReceipt")
        if not isinstance(receipt, str):
            raise BrokerAuthorizationError("Checkpoint receipt is unavailable.")
        return receipt


class LambdaPersistenceBroker:
    def __init__(self, client: LambdaBrokerClient, sandbox_id: str) -> None:
        self._client = client
        self._sandbox_id = sandbox_id

    def cipher(self, sandbox_id: str) -> SandboxCipher:
        self._validate_sandbox(sandbox_id)
        value = self._client.call("dataKey")
        encoded = value.get("plaintextKey")
        if not isinstance(encoded, str):
            raise BrokerAuthorizationError("Persistence data key is unavailable.")
        try:
            key = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise BrokerAuthorizationError("Persistence data key is invalid.") from error
        return SandboxCipher(key, sandbox_id)

    def encryption_context(self, sandbox_id: str) -> dict[str, str]:
        self._validate_sandbox(sandbox_id)
        return {
            "application": "kirocrew-agentcore",
            "purpose": "sandbox-checkpoint",
            "sandboxId": sandbox_id,
        }

    def presign_chunk(
        self, sandbox_id: str, digest: str, operation: StorageOperation
    ) -> PresignedOperation:
        return self._presign(sandbox_id, "chunks", f"{digest}.bin", operation)

    def presign_manifest(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation:
        return self._presign(sandbox_id, "manifests", f"{generation}.json.enc", operation)

    def presign_commit(
        self, sandbox_id: str, generation: int, operation: StorageOperation
    ) -> PresignedOperation:
        return self._presign(sandbox_id, "commits", f"{generation}.json", operation)

    def request(
        self,
        grant: PresignedOperation,
        operation: StorageOperation,
        *,
        body: bytes | None = None,
    ) -> bytes | bool:
        if grant.method is not operation:
            raise BrokerAuthorizationError("Persistence grant operation does not match.")
        parsed = urlsplit(grant.url)
        hostname = parsed.hostname or ""
        if parsed.scheme != "https" or not (
            hostname.endswith(".amazonaws.com") or hostname.endswith(".amazonaws.com.cn")
        ):
            raise BrokerAuthorizationError("Persistence grant URL is not trusted.")
        request = urllib.request.Request(  # noqa: S310
            grant.url,
            data=body,
            headers=dict(grant.headers),
            method=operation.value,
        )
        try:
            with urllib.request.urlopen(  # noqa: S310  # nosec B310 - validated HTTPS AWS host.
                request, timeout=30
            ) as response:
                if operation is StorageOperation.HEAD:
                    return cast(int, response.status) == 200
                return cast(bytes, response.read())
        except urllib.error.HTTPError as error:
            if operation is StorageOperation.HEAD and error.code == 404:
                return False
            raise BrokerAuthorizationError("Persistence object operation failed.") from error
        except urllib.error.URLError as error:
            raise BrokerAuthorizationError("Persistence object operation failed.") from error

    def internal_keys(self, sandbox_id: str, category: str) -> tuple[str, ...]:
        self._validate_sandbox(sandbox_id)
        value = self._client.call("list", category=category)
        names = value.get("names")
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            raise BrokerAuthorizationError("Persistence object listing is invalid.")
        return tuple(f"sandboxes/{sandbox_id}/{category}/{item}" for item in cast(list[str], names))

    def internal_delete(self, sandbox_id: str, category: str, name: str) -> None:
        raise BrokerAuthorizationError("Runtime checkpoint deletion is not permitted.")

    def _presign(
        self,
        sandbox_id: str,
        category: str,
        name: str,
        operation: StorageOperation,
    ) -> PresignedOperation:
        self._validate_sandbox(sandbox_id)
        value = self._client.call(
            "presign",
            category=category,
            method=operation.value,
            name=name,
        )
        url = value.get("url")
        expires = value.get("expiresAt")
        headers = value.get("headers")
        if (
            not isinstance(url, str)
            or not isinstance(expires, str)
            or not isinstance(headers, dict)
        ):
            raise BrokerAuthorizationError("Persistence grant is invalid.")
        try:
            expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError as error:
            raise BrokerAuthorizationError("Persistence grant expiry is invalid.") from error
        safe_headers = {
            key: item
            for key, item in cast(Mapping[object, object], headers).items()
            if isinstance(key, str) and isinstance(item, str)
        }
        if len(safe_headers) != len(headers):
            raise BrokerAuthorizationError("Persistence grant headers are invalid.")
        return PresignedOperation(url, operation, expires_at, safe_headers)

    def _validate_sandbox(self, sandbox_id: str) -> None:
        if sandbox_id != self._sandbox_id:
            raise BrokerAuthorizationError("Cross-sandbox persistence access is denied.")
