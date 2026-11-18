from __future__ import annotations

import base64
import http.client
import json
import ssl
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from urllib.parse import urlsplit

from kirocrew_agentcore_persistence.crypto import SandboxCipher
from kirocrew_agentcore_persistence.durability import (
    BrokerAuthorizationError,
    BrokerRefusalError,
    PresignedOperation,
    StorageOperation,
)

# Grants per batch call; the broker caps a batch at the same size.
_BATCH_GRANTS = 128
# A cached grant this close to expiry is not handed out: a large PUT or a slow
# GET could still be in flight when the URL stops being valid.
_GRANT_MARGIN = timedelta(seconds=60)
# Object requests time out after this many seconds.
_OBJECT_TIMEOUT_SECONDS = 30
_TLS_CONTEXT = ssl.create_default_context()
# One keep-alive HTTPS connection per thread and host. A restore fetches tens of
# thousands of small chunks; a fresh TLS handshake for each one dominated the
# download time.
_CONNECTIONS = threading.local()


def _connection(host: str, port: int | None) -> http.client.HTTPSConnection:
    pool = cast(
        "dict[tuple[str, int | None], http.client.HTTPSConnection] | None",
        getattr(_CONNECTIONS, "pool", None),
    )
    if pool is None:
        pool = {}
        _CONNECTIONS.pool = pool
    connection = pool.get((host, port))
    if connection is None:
        connection = http.client.HTTPSConnection(
            host, port, timeout=_OBJECT_TIMEOUT_SECONDS, context=_TLS_CONTEXT
        )
        pool[(host, port)] = connection
    return connection


def _discard_connection(host: str, port: int | None) -> None:
    pool = cast(
        "dict[tuple[str, int | None], http.client.HTTPSConnection]",
        _CONNECTIONS.pool,
    )
    pool.pop((host, port)).close()


def _object_request(
    host: str,
    port: int | None,
    target: str,
    method: str,
    body: bytes | None,
    headers: Mapping[str, str],
) -> tuple[int, bytes]:
    """Send one object request over this thread's kept-alive connection.

    A connection the server closed while idle fails on reuse; the request is
    then sent once more on a fresh connection. Object requests are idempotent
    (content-addressed chunks and fixed-name manifests), so a retry is safe.
    """
    for attempt in range(2):
        connection = _connection(host, port)
        try:
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            payload = response.read()
        except (http.client.HTTPException, OSError):
            _discard_connection(host, port)
            if attempt == 1:
                raise
            continue
        if response.will_close:
            _discard_connection(host, port)
        return response.status, payload
    raise AssertionError("unreachable")  # pragma: no cover


def invoke_broker(
    client: Any,
    function_arn: str,
    operation: str,
    *,
    token: str,
    sandbox_id: str,
    runtime_session_id: str,
    **values: object,
) -> dict[str, object]:
    """Invoke one persistence broker operation and return its JSON result.

    ``token`` is either the browser-issued binding token or the broker-issued
    runtime-session token; the broker accepts both under ``bindingToken`` and
    checks the sandbox record against the claims on every call. A refused
    token or binding surfaces as :class:`BrokerRefusalError`; any other broker
    failure as its base :class:`BrokerAuthorizationError`.
    """
    request = {
        "bindingToken": token,
        "operation": operation,
        "runtimeSessionId": runtime_session_id,
        "sandboxId": sandbox_id,
        **values,
    }
    response = client.invoke(
        FunctionName=function_arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(request, separators=(",", ":")).encode(),
    )
    payload = response["Payload"].read()
    if response.get("FunctionError"):
        if _error_type(payload) == "PermissionError":
            raise BrokerRefusalError("Persistence broker refused the binding.")
        raise BrokerAuthorizationError("Persistence broker rejected the operation.")
    value = cast(object, json.loads(payload))
    if not isinstance(value, dict):
        raise BrokerAuthorizationError("Persistence broker response is invalid.")
    return cast(dict[str, object], value)


def _error_type(payload: bytes) -> str | None:
    """Return the Lambda error payload's ``errorType`` when it is readable."""
    try:
        value = cast(object, json.loads(payload))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    error_type = cast(dict[str, object], value).get("errorType")
    return error_type if isinstance(error_type, str) else None


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
        # The generation this container's working copy descends from. Set it
        # after restore (0 for a brand-new sandbox); every committed receipt
        # advances it. While None, receipts are sent without a base.
        self.base_generation: int | None = None
        # Published outward on every receipt so a scheduler outside the sandbox
        # knows when to wake it: the epoch second the next job or loop is due
        # (0 = nothing scheduled), which cron job that is (empty for a loop),
        # and how long this sandbox took to restore -- the wake lead is derived
        # from it, so a big workspace is woken earlier than an empty one.
        self.next_due_at: int | None = None
        self.next_due_job: str = ""
        self.restore_seconds: int | None = None
        # This container's own id (its initialization owner). The platform can
        # run two microVMs under one runtime session id, so the session alone
        # cannot tell the current holder from a superseded one; receipts carry
        # the instance and the broker accepts them only from the holder.
        self.holder_instance: str | None = None

    def call(self, operation: str, **values: object) -> dict[str, object]:
        return invoke_broker(
            self._client,
            self._function_arn,
            operation,
            token=self.binding_token,
            sandbox_id=self._sandbox_id,
            runtime_session_id=self._runtime_session_id,
            **values,
        )

    def checkpoint_receipt(
        self, generation: int, manifest_digest: str, *, final: bool = True
    ) -> str:
        values: dict[str, object] = {
            "final": final,
            "generation": generation,
            "manifestDigest": manifest_digest,
        }
        if self.base_generation is not None:
            values["baseGeneration"] = self.base_generation
        if self.next_due_at is not None:
            values["nextDueAt"] = self.next_due_at
            values["nextDueJob"] = self.next_due_job
        if self.restore_seconds is not None:
            values["restoreSeconds"] = self.restore_seconds
        if self.holder_instance is not None:
            values["holderInstance"] = self.holder_instance
        value = self.call("checkpointReceipt", **values)
        receipt = value.get("checkpointReceipt")
        if not isinstance(receipt, str):
            raise BrokerAuthorizationError("Checkpoint receipt is unavailable.")
        self.base_generation = generation
        return receipt


class LambdaPersistenceBroker:
    def __init__(
        self,
        client: LambdaBrokerClient,
        sandbox_id: str,
        *,
        clock: Any = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._clock = clock
        # Chunk grants fetched ahead in batches. A caller that knows which
        # chunks it is about to move announces them with ``prefetch_chunks``;
        # the first grant it then asks for fetches that chunk and the next
        # announced ones in one broker call, so N chunks cost N / 128 broker
        # round trips instead of N. Fetching lazily, one batch at a time, keeps
        # every grant well inside its five-minute life.
        self._grant_lock = threading.Lock()
        self._grants: dict[tuple[str, StorageOperation], PresignedOperation] = {}
        self._wanted: dict[StorageOperation, deque[str]] = {}
        self._batch_supported = True

    def prefetch_chunks(
        self, sandbox_id: str, digests: Iterable[str], operation: StorageOperation
    ) -> None:
        self._validate_sandbox(sandbox_id)
        if operation not in {StorageOperation.GET, StorageOperation.PUT}:
            return
        with self._grant_lock:
            if self._batch_supported:
                self._wanted.setdefault(operation, deque()).extend(digests)

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
        self._validate_sandbox(sandbox_id)
        names: list[str] | None = None
        with self._grant_lock:
            grant = self._take_grant(digest, operation)
            if grant is None and self._batch_supported and self._wanted.get(operation):
                names = self._claim_batch(digest, operation)
        if grant is not None:
            return grant
        if names is not None:
            # The broker call runs outside the lock so workers that miss at the
            # same time fetch their own batches in parallel; one lock-held call
            # at a time serialized a whole restore behind the broker.
            fetched = self._fetch_batch(names, operation)
            with self._grant_lock:
                if fetched is None:
                    self._batch_supported = False
                    self._wanted.clear()
                else:
                    self._grants.update(fetched)
                grant = self._take_grant(digest, operation)
            if grant is not None:
                return grant
        return self._presign(sandbox_id, "chunks", f"{digest}.bin", operation)

    def _take_grant(self, digest: str, operation: StorageOperation) -> PresignedOperation | None:
        grant = self._grants.pop((digest, operation), None)
        if grant is None or grant.expires_at - self._clock() < _GRANT_MARGIN:
            return None
        return grant

    def _claim_batch(self, digest: str, operation: StorageOperation) -> list[str]:
        """Take ``digest`` plus the next announced chunks off the wanted queue.

        Called with the grant lock held, so concurrent claims never overlap.
        """
        wanted = self._wanted[operation]
        names = [digest]
        while wanted and len(names) < _BATCH_GRANTS:
            candidate = wanted.popleft()
            if candidate != digest and (candidate, operation) not in self._grants:
                names.append(candidate)
        return names

    def _fetch_batch(
        self, names: list[str], operation: StorageOperation
    ) -> dict[tuple[str, StorageOperation], PresignedOperation] | None:
        """Fetch grants for ``names`` in one broker call.

        A broker without the batch operation (an older deployment) answers with
        an ordinary operation error; ``None`` tells the caller to fall back to
        one grant per chunk. A refused binding is not a missing feature and
        propagates.
        """
        try:
            value = self._client.call(
                "presignBatch",
                method=operation.value,
                names=[f"{name}.bin" for name in names],
            )
        except BrokerRefusalError:
            raise
        except BrokerAuthorizationError:
            return None
        grants = value.get("grants")
        if not isinstance(grants, list):
            raise BrokerAuthorizationError("Persistence batch grant is invalid.")
        fetched: dict[tuple[str, StorageOperation], PresignedOperation] = {}
        for item in cast(list[object], grants):
            if not isinstance(item, dict):
                raise BrokerAuthorizationError("Persistence batch grant is invalid.")
            entry = cast(dict[str, object], item)
            name = entry.get("name")
            if not isinstance(name, str) or not name.endswith(".bin"):
                raise BrokerAuthorizationError("Persistence batch grant is invalid.")
            fetched[(name.removesuffix(".bin"), operation)] = self._grant_from(entry, operation)
        return fetched

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
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        try:
            status, payload = _object_request(
                hostname, parsed.port, target, operation.value, body, grant.headers
            )
        except (http.client.HTTPException, OSError) as error:
            raise BrokerAuthorizationError("Persistence object operation failed.") from error
        if operation is StorageOperation.HEAD:
            if status == 404:
                return False
            if status == 200:
                return True
        elif 200 <= status < 300:
            return payload
        raise BrokerAuthorizationError("Persistence object operation failed.")

    def chunk_exists(self, sandbox_id: str, digest: str) -> bool:
        self._validate_sandbox(sandbox_id)
        name = f"{digest}.bin"
        value = self._client.call("list", category="chunks", name=name)
        names = value.get("names")
        if not isinstance(names, list) or not all(isinstance(item, str) for item in names):
            raise BrokerAuthorizationError("Persistence object listing is invalid.")
        return name in cast(list[str], names)

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
        return self._grant_from(value, operation)

    @staticmethod
    def _grant_from(value: Mapping[str, object], operation: StorageOperation) -> PresignedOperation:
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
