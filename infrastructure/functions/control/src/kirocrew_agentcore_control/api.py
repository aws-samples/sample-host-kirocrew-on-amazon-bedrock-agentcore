from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal, Protocol, cast

from kirocrew_agentcore_control.sandbox import (
    IdempotencyConflictError,
    InMemorySandboxRegistry,
    LeaseConflictError,
    SandboxRecord,
    SandboxRegistryError,
    SandboxState,
    SandboxUnavailableError,
    StateConflictError,
)

_LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION: Final = "kirocrew-agentcore.v1"
# Long enough for a cold-start restore of a multi-hundred-megabyte checkpoint
# (every chunk download re-presents this token to the persistence broker);
# still bound to one sandbox, session, and subject, and KMS-signed.
BINDING_TTL: Final = timedelta(minutes=30)
RECEIPT_TTL: Final = timedelta(minutes=10)
_CONTROL_SCOPE: Final = "kirocrew.control"
_CORRELATION_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ROUTES: Final = {
    ("GET", "/control/v1/config"): "getPublicConfig",
    ("GET", "/control/v1/sandbox"): "getSandbox",
    ("DELETE", "/control/v1/sandbox"): "deleteSandbox",
    ("POST", "/control/v1/sandbox/start"): "startSandbox",
    ("POST", "/control/v1/sandbox/stop"): "stopSandbox",
    ("GET", "/control/v1/sandbox/checkpoints"): "listCheckpoints",
    ("GET", "/control/v1/sandbox/history"): "getSandboxHistory",
}


class CognitoTokenUse(StrEnum):
    ACCESS = "access"


class ControlApiError(RuntimeError):
    def __init__(
        self,
        status: int,
        code: str,
        category: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.category = category
        self.message = message
        self.retryable = retryable


class SigningService(Protocol):
    def sign(self, message: bytes) -> bytes: ...

    def verify(self, message: bytes, signature: bytes) -> None: ...


class LocalAsymmetricKmsSigner:
    """Credential-free Ed25519 adapter matching asymmetric KMS sign/verify semantics."""

    def __init__(self, private_key: Any) -> None:
        self._private_key = private_key
        self._public_key = private_key.public_key()

    @classmethod
    def generate(cls) -> LocalAsymmetricKmsSigner:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        return cls(Ed25519PrivateKey.generate())

    def sign(self, message: bytes) -> bytes:
        return cast(bytes, self._private_key.sign(message))

    def verify(self, message: bytes, signature: bytes) -> None:
        self._public_key.verify(signature, message)


@dataclass(frozen=True, slots=True)
class Identity:
    subject: str
    issuer: str
    administrator: bool

    @property
    def subject_hash(self) -> str:
        return hashlib.sha256(f"{self.issuer}\x00{self.subject}".encode()).hexdigest()


class ClaimsValidator:
    def __init__(
        self,
        issuer: str,
        app_client_id: str,
        *,
        required_scope: str = _CONTROL_SCOPE,
        administrator_group: str = "sandbox-admins",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not issuer or not app_client_id or not required_scope:
            raise ValueError("Issuer, app client, and scope are required.")
        self._issuer = issuer
        self._app_client_id = app_client_id
        self._required_scope = required_scope
        self._administrator_group = administrator_group
        self._clock = clock

    @property
    def issuer(self) -> str:
        """The issuer an Identity must be built against.

        Exposed because a scheduled wake constructs an Identity from a configured
        subject rather than from a verified token, and ``subject_hash`` -- which is
        what the sandbox record and every token are keyed on -- folds the issuer
        in. Reading it from the same validator the browser path uses is what keeps
        the two from drifting apart into two different hashes for one person.
        """
        return self._issuer

    def validate(self, claims: Mapping[str, object] | None) -> Identity:
        if claims is None:
            raise ControlApiError(
                401,
                "AUTHENTICATION_FAILED",
                "AUTHENTICATION",
                "Authentication is required.",
            )
        subject = claims.get("sub")
        issuer = claims.get("iss")
        client = claims.get("client_id", claims.get("aud"))
        raw_expires = claims.get("exp")
        expires = (
            int(raw_expires)
            if isinstance(raw_expires, str) and raw_expires.isdigit()
            else raw_expires
        )
        scopes = claims.get("scope")
        token_use = claims.get("token_use")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(issuer, str)
            or issuer != self._issuer
            or not isinstance(client, str)
            or client != self._app_client_id
            or type(expires) not in {int, float}
            or not isinstance(scopes, str)
            or token_use != CognitoTokenUse.ACCESS
        ):
            raise ControlApiError(
                401,
                "AUTHENTICATION_FAILED",
                "AUTHENTICATION",
                "Authentication claims are invalid.",
            )
        if float(cast(int | float, expires)) <= self._clock().timestamp():
            raise ControlApiError(
                401,
                "AUTHENTICATION_FAILED",
                "AUTHENTICATION",
                "Authentication has expired.",
            )
        if self._required_scope not in scopes.split():
            raise ControlApiError(
                403,
                "AUTHORIZATION_FAILED",
                "AUTHORIZATION",
                "The required scope is missing.",
            )
        groups = claims.get("cognito:groups", [])
        administrator = isinstance(groups, list) and self._administrator_group in groups
        return Identity(subject, issuer, administrator)


class SignedControlTokens:
    def __init__(
        self,
        signer: SigningService,
        audience: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        nonce_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        if not audience:
            raise ValueError("Token audience is required.")
        self._signer = signer
        self._audience = audience
        self._clock = clock
        self._nonce_factory = nonce_factory

    def issue_binding(self, identity: Identity, record: SandboxRecord) -> tuple[str, datetime]:
        expires = self._clock() + BINDING_TTL
        token = self._issue(
            {
                "aud": self._audience,
                "exp": int(expires.timestamp()),
                "iat": int(self._clock().timestamp()),
                "nonce": self._nonce_factory(),
                "sandboxId": record.sandbox_id,
                "subjectHash": identity.subject_hash,
                "runtimeSessionId": record.runtime_session_id,
                "type": "binding",
            }
        )
        return token, expires

    def issue_scheduler(self, identity: Identity, record: SandboxRecord) -> tuple[str, datetime]:
        """Mint the token an unattended wake presents instead of a binding token.

        Claim-for-claim identical to a binding token apart from ``type``, because
        everything downstream -- lease authorization, the sandbox claim, restore --
        should behave exactly as it does for a browser. The distinct type exists so
        the adapter can tell that NO Cognito ``Authorization`` header will
        accompany it: a scheduled wake has no browser and therefore no user token
        to cross-check ``subjectHash`` against.

        The signature is what carries the authority, exactly as for a binding
        token. What makes skipping the cross-check safe is not the type name but
        who can obtain one: this issuer is only reached through a direct Lambda
        invocation, so the trust boundary is ``lambda:InvokeFunction`` on the
        control plane. The adapter must ALSO refuse this type on the
        browser-facing runtime -- see docs/design-scheduled-jobs.md.
        """
        expires = self._clock() + BINDING_TTL
        token = self._issue(
            {
                "aud": self._audience,
                "exp": int(expires.timestamp()),
                "iat": int(self._clock().timestamp()),
                "nonce": self._nonce_factory(),
                "sandboxId": record.sandbox_id,
                "subjectHash": identity.subject_hash,
                "runtimeSessionId": record.runtime_session_id,
                "type": "scheduler",
            }
        )
        return token, expires

    def issue_checkpoint_receipt(
        self,
        identity: Identity,
        record: SandboxRecord,
        generation: int,
        manifest_digest: str,
    ) -> str:
        if generation <= 0 or len(manifest_digest) != 64:
            raise ValueError("Committed generation and manifest digest are required.")
        return self._issue(
            {
                "aud": self._audience,
                "exp": int((self._clock() + RECEIPT_TTL).timestamp()),
                "generation": generation,
                "iat": int(self._clock().timestamp()),
                "manifestDigest": manifest_digest,
                "nonce": self._nonce_factory(),
                "runtimeSessionId": record.runtime_session_id,
                "sandboxId": record.sandbox_id,
                "subjectHash": identity.subject_hash,
                "type": "checkpoint-receipt",
            }
        )

    def verify_checkpoint_receipt(
        self, token: str, identity: Identity, record: SandboxRecord
    ) -> tuple[int, str]:
        value = self._verify(token)
        generation = value.get("generation")
        digest = value.get("manifestDigest")
        if (
            value.get("type") != "checkpoint-receipt"
            or value.get("aud") != self._audience
            or value.get("sandboxId") != record.sandbox_id
            or value.get("runtimeSessionId") != record.runtime_session_id
            or value.get("subjectHash") != identity.subject_hash
            or type(generation) is not int
            or generation <= 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ControlApiError(
                409,
                "CHECKPOINT_FAILED",
                "PERSISTENCE",
                "Checkpoint receipt does not match the sandbox.",
            )
        expires = value.get("exp")
        if type(expires) is not int or expires <= int(self._clock().timestamp()):
            raise ControlApiError(
                409,
                "CHECKPOINT_FAILED",
                "PERSISTENCE",
                "Checkpoint receipt is stale.",
            )
        return generation, digest

    def _issue(self, value: Mapping[str, object]) -> str:
        payload = _base64url(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())
        signature = _base64url(self._signer.sign(payload.encode()))
        return f"{payload}.{signature}"

    def _verify(self, token: str) -> dict[str, object]:
        try:
            payload_encoded, signature_encoded = token.split(".", 1)
            payload = _unbase64url(payload_encoded)
            signature = _unbase64url(signature_encoded)
            self._signer.verify(payload_encoded.encode(), signature)
            value = cast(object, json.loads(payload))
            if not isinstance(value, dict):
                raise ValueError
            return cast(dict[str, object], value)
        except Exception as error:
            raise ControlApiError(
                409,
                "CHECKPOINT_FAILED",
                "PERSISTENCE",
                "Checkpoint receipt is invalid.",
            ) from error


@dataclass(frozen=True, slots=True)
class CheckpointMetadata:
    generation: int
    manifest_digest: str
    created_at: datetime
    status: Literal["COMMITTED", "RESTORE_FAILED"] = "COMMITTED"
    schema_version: int = 1


class InMemoryCheckpointCatalog:
    def __init__(self) -> None:
        self._items: dict[str, dict[int, CheckpointMetadata]] = {}

    def record(self, sandbox_id: str, metadata: CheckpointMetadata) -> None:
        if metadata.generation <= 0 or len(metadata.manifest_digest) != 64:
            raise ValueError("Valid checkpoint metadata is required.")
        self._items.setdefault(sandbox_id, {})[metadata.generation] = metadata

    def get(self, sandbox_id: str, generation: int) -> CheckpointMetadata | None:
        return self._items.get(sandbox_id, {}).get(generation)

    def list(self, sandbox_id: str) -> tuple[CheckpointMetadata, ...]:
        return tuple(
            sorted(self._items.get(sandbox_id, {}).values(), key=lambda item: item.generation)
        )


class AgentCoreStopError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__("AgentCore stop failed.")
        self.status_code = status_code


class AgentCoreStopper(Protocol):
    def stop(self, runtime_session_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ControlConfig:
    issuer: str
    app_client_id: str
    allowed_origin: str
    region: str
    runtime_arn: str
    qualifier: str
    http_url: str
    websocket_url: str
    deployment_mode: Literal["microvm", "instances"]
    frontend_compatibility_version: str
    token_audience: str
    persisted_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.allowed_origin.startswith("https://")
            or not self.runtime_arn.startswith("arn:")
            or not self.http_url.startswith("https://")
            or not self.websocket_url.startswith("wss://")
            or not self.qualifier
            or not self.region
            or not self.frontend_compatibility_version
        ):
            raise ValueError("Control API configuration is invalid.")


class SandboxControlService:
    def __init__(
        self,
        config: ControlConfig,
        registry: InMemorySandboxRegistry,
        claims: ClaimsValidator,
        tokens: SignedControlTokens,
        checkpoints: InMemoryCheckpointCatalog,
        stopper: AgentCoreStopper,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = lambda cap: secrets.randbelow(1_000_001)
        / 1_000_000
        * cap,
        max_stop_attempts: int = 4,
        lease_ttl: timedelta = timedelta(seconds=90),
    ) -> None:
        if max_stop_attempts <= 0 or lease_ttl <= timedelta(0):
            raise ValueError("Retry attempts and lease TTL must be positive.")
        self._config = config
        self._registry = registry
        self._claims = claims
        self._tokens = tokens
        self._checkpoints = checkpoints
        self._stopper = stopper
        self._sleep = sleep
        self._jitter = jitter
        self._max_stop_attempts = max_stop_attempts
        self._lease_ttl = lease_ttl
        self._idempotency: dict[tuple[str, str, str], tuple[str, dict[str, object]]] = {}

    def handle(self, event: Mapping[str, object]) -> dict[str, object]:
        correlation_id = self._correlation_id(event)
        try:
            method = event.get("requestContext")
            request_context = method if isinstance(method, dict) else {}
            # A scheduled wake arrives as a DIRECT Lambda invocation, which has no
            # requestContext at all. Requiring its absence is what keeps this path
            # off the HTTP surface: every API Gateway event carries one, so a
            # browser cannot reach this branch even with a valid user token, and
            # the trust boundary is lambda:InvokeFunction rather than a route.
            if not request_context and event.get("operation") == "schedulerStart":
                return self._scheduler_start(event, correlation_id)
            if not request_context and event.get("operation") == "schedulerStop":
                return self._scheduler_stop(event, correlation_id)
            http = request_context.get("http")
            http_value = http if isinstance(http, dict) else {}
            verb = http_value.get("method")
            path = event.get("rawPath")
            if not isinstance(verb, str) or not isinstance(path, str):
                raise ControlApiError(400, "INVALID_MESSAGE", "LIFECYCLE", "Request is invalid.")
            self._validate_origin(event)
            if verb == "OPTIONS":
                return self._response(204, {}, correlation_id)
            operation = ROUTES.get((verb, path))
            if operation is None:
                raise ControlApiError(404, "INVALID_MESSAGE", "LIFECYCLE", "Route is unavailable.")
            if operation == "getPublicConfig":
                return self._response(200, self._public_config(), correlation_id)
            identity = self._claims.validate(self._claims_from_event(request_context))
            if operation == "getSandbox":
                record = self._registry.get_or_create(identity.subject)
                # The status poll doubles as the history observer: every
                # state transition the browser can see lands in the record.
                self._registry.record_observation(record)
                return self._response(200, self._sandbox(record), correlation_id)
            if operation == "getSandboxHistory":
                record = self._registry.get_or_create(identity.subject)
                self._registry.record_observation(record)
                history_body: dict[str, object] = {
                    "events": [
                        {
                            "at": _timestamp(event.at),
                            "state": event.state.value,
                            "stateVersion": event.state_version,
                        }
                        for event in self._registry.history(record.sandbox_id)
                    ],
                    "persistedPaths": list(self._config.persisted_paths),
                }
                return self._response(200, history_body, correlation_id)
            if operation == "startSandbox":
                body = self._start(identity, self._idempotency_key(event))
                return self._response(200, body, correlation_id)
            if operation == "stopSandbox":
                body = self._stop(identity, self._idempotency_key(event), self._body(event))
                return self._response(202, body, correlation_id)
            if operation == "listCheckpoints":
                record = self._registry.get(identity.subject)
                checkpoint_body = [
                    self._checkpoint(item) for item in self._checkpoints.list(record.sandbox_id)
                ]
                return self._response(200, checkpoint_body, correlation_id)
            if not identity.administrator:
                raise ControlApiError(
                    403,
                    "AUTHORIZATION_FAILED",
                    "AUTHORIZATION",
                    "Administrator authorization is required.",
                )
            body = self._sandbox(self._registry.request_deletion(identity.subject))
            return self._response(202, body, correlation_id)
        except ControlApiError as error:
            return self._error(error, correlation_id)
        except SandboxUnavailableError:
            return self._error(
                ControlApiError(
                    404,
                    "AUTHORIZATION_FAILED",
                    "AUTHORIZATION",
                    "Sandbox is unavailable.",
                ),
                correlation_id,
            )
        except (LeaseConflictError, StateConflictError, IdempotencyConflictError) as error:
            return self._error(
                ControlApiError(409, "SANDBOX_CONFLICT", "LIFECYCLE", str(error)),
                correlation_id,
            )
        except SandboxRegistryError:
            return self._error(
                ControlApiError(500, "INTERNAL_ERROR", "INTERNAL", "Request failed."),
                correlation_id,
            )

    def _scheduler_start(
        self, event: Mapping[str, object], correlation_id: str
    ) -> dict[str, object]:
        """Wake a sandbox on behalf of an owner who is not present.

        The caller must name BOTH the Cognito subject and the sandbox id it
        expects, and they must correspond. The subject alone would be enough to
        mint a token, but then a typo or a stale config would silently CREATE a
        sandbox for a subject that has none, and a scheduled job would start
        writing into a fresh empty workspace instead of failing loudly. The
        sandbox id is the assertion that turns that into an error.

        The subject has to be supplied rather than discovered: the record keeps
        only a one-way ``owner_hash``, so nothing here can recover whose sandbox
        it is. See docs/design-scheduled-jobs.md for why that is deliberate and
        what multi-owner support would need.
        """
        subject = event.get("cognitoSubject")
        sandbox_id = event.get("sandboxId")
        key = event.get("idempotencyKey")
        if not isinstance(subject, str) or not subject:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler wake requires a subject."
            )
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler wake requires a sandbox id."
            )
        if not isinstance(key, str) or not key:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler wake requires an idempotency key."
            )
        identity = Identity(subject=subject, issuer=self._claims.issuer, administrator=False)
        replay = self._idempotent(identity.subject, "schedulerStart", key, "schedulerStart")
        if replay is not None:
            return self._response(200, replay, correlation_id)
        # `get`, never `get_or_create`. Creating on a wake is exactly the failure
        # this path must not have: a stale or mistyped subject would mint a fresh
        # empty sandbox, the job would run against it, and everything would look
        # healthy while writing nowhere the owner can see.
        try:
            existing = self._registry.get(identity.subject)
        except SandboxUnavailableError as error:
            raise ControlApiError(
                404,
                "SANDBOX_NOT_FOUND",
                "LIFECYCLE",
                "The named subject has no sandbox to wake.",
            ) from error
        if existing.sandbox_id != sandbox_id:
            # Deliberately not "not found": the subject DOES resolve, just not to
            # the sandbox the caller expected, which means the caller's
            # configuration and this deployment disagree about who owns what.
            raise ControlApiError(
                409,
                "SANDBOX_MISMATCH",
                "LIFECYCLE",
                "The named sandbox does not belong to the named subject.",
            )
        lease = self._registry.acquire_start(
            identity.subject,
            existing.runtime_session_id,
            ttl=self._lease_ttl,
        )
        token, expires = self._tokens.issue_scheduler(identity, lease.record)
        body: dict[str, object] = {
            "authoritative": lease.authoritative,
            "expiresAt": _timestamp(expires),
            "qualifier": self._config.qualifier,
            "runtimeArn": self._config.runtime_arn,
            "runtimeSessionId": lease.record.runtime_session_id,
            "sandboxId": lease.record.sandbox_id,
            "schedulerToken": token,
            "state": lease.record.state.value,
        }
        self._remember(identity.subject, "schedulerStart", key, "schedulerStart", body)
        return self._response(200, body, correlation_id)

    def _start(self, identity: Identity, key: str) -> dict[str, object]:
        replay = self._idempotent(identity.subject, "start", key, "start")
        if replay is not None:
            return replay
        existing = self._registry.get_or_create(identity.subject)
        lease = self._registry.acquire_start(
            identity.subject,
            existing.runtime_session_id,
            ttl=self._lease_ttl,
        )
        binding, expires = self._tokens.issue_binding(identity, lease.record)
        response: dict[str, object] = {
            "bindingToken": binding,
            "expiresAt": _timestamp(expires),
            "frontendCompatibilityVersion": self._config.frontend_compatibility_version,
            "httpUrl": self._config.http_url,
            "protocolVersion": PROTOCOL_VERSION,
            "qualifier": self._config.qualifier,
            "runtimeArn": self._config.runtime_arn,
            "runtimeSessionId": lease.record.runtime_session_id,
            "sandboxId": lease.record.sandbox_id,
            "state": lease.record.state.value,
            "webSocketUrl": self._config.websocket_url,
        }
        self._remember(identity.subject, "start", key, "start", response)
        return response

    def _scheduler_stop(
        self, event: Mapping[str, object], correlation_id: str
    ) -> dict[str, object]:
        """Finish the teardown a scheduled wake started, on the owner's behalf.

        Stopping is TWO steps and neither half works alone. The runtime commits a
        final checkpoint and hands back a receipt, moving the record to STOPPING;
        this call verifies that receipt against the committed generation, tears the
        runtime session down, and finalizes STOPPED.

        A wake that performed only the first half left the record stranded at
        STOPPING -- the state `_stop_runtime` explicitly warns about -- with the
        session never rotated, so the NEXT wake's `sandbox.prepare_stop` answered
        "already prepared" with an empty event list and committed nothing. The
        durability of every later cycle quietly depended on a periodic checkpoint
        landing before idle reclaim.

        Reuses `_stop` rather than reimplementing the lifecycle: the receipt
        verification, the generation and digest cross-checks, and the STOPPED
        transition are the same rules whether a browser or a schedule asks, and
        having two copies of them is how they drift apart.
        """
        subject = event.get("cognitoSubject")
        sandbox_id = event.get("sandboxId")
        key = event.get("idempotencyKey")
        receipt = event.get("checkpointReceipt")
        if not isinstance(subject, str) or not subject:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler stop requires a subject."
            )
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler stop requires a sandbox id."
            )
        if not isinstance(key, str) or not key:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "Scheduler stop requires an idempotency key."
            )
        if not isinstance(receipt, str) or not receipt:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "PERSISTENCE", "Scheduler stop requires a receipt."
            )
        identity = Identity(subject=subject, issuer=self._claims.issuer, administrator=False)
        # Same assertion as the wake: the subject alone would be enough to act, but
        # then a stale configuration could tear down a sandbox the caller did not
        # mean to name. The sandbox id is what turns that into an error.
        try:
            existing = self._registry.get(identity.subject)
        except SandboxUnavailableError as error:
            raise ControlApiError(
                404, "SANDBOX_NOT_FOUND", "LIFECYCLE", "The named subject has no sandbox."
            ) from error
        if existing.sandbox_id != sandbox_id:
            raise ControlApiError(
                409,
                "SANDBOX_MISMATCH",
                "LIFECYCLE",
                "The named sandbox does not belong to the named subject.",
            )
        body = self._stop(identity, key, {"checkpointReceipt": receipt})
        return self._response(200, body, correlation_id)

    def _stop(self, identity: Identity, key: str, body: Mapping[str, object]) -> dict[str, object]:
        receipt = body.get("checkpointReceipt")
        if not isinstance(receipt, str) or set(body) != {"checkpointReceipt"}:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "PERSISTENCE", "Checkpoint receipt is required."
            )
        payload_hash = hashlib.sha256(receipt.encode()).hexdigest()
        replay = self._idempotent(identity.subject, "stop", key, payload_hash)
        if replay is not None:
            return replay
        record = self._registry.get(identity.subject)
        generation, digest = self._tokens.verify_checkpoint_receipt(receipt, identity, record)
        metadata = self._checkpoints.get(record.sandbox_id, generation)
        if (
            record.state is not SandboxState.STOPPING
            or record.last_checkpoint_generation != generation
            or metadata is None
            or metadata.status != "COMMITTED"
            or metadata.manifest_digest != digest
        ):
            raise ControlApiError(
                409,
                "CHECKPOINT_FAILED",
                "PERSISTENCE",
                "A current committed checkpoint is required before stop.",
            )
        self._stop_runtime(record.runtime_session_id)
        lease_owner = cast(str, record.lease_owner)
        stopped = self._registry.transition(
            identity.subject,
            SandboxState.STOPPED,
            expected_version=record.state_version,
            lease_owner=lease_owner,
        )
        response = self._sandbox(stopped)
        self._remember(identity.subject, "stop", key, payload_hash, response)
        return response

    def _stop_runtime(self, runtime_session_id: str) -> None:
        attempt = 0
        while True:
            try:
                self._stopper.stop(runtime_session_id)
                return
            except AgentCoreStopError as error:
                retryable = error.status_code in {409, 429} or error.status_code >= 500
                if not retryable:
                    # The data plane refused the teardown for good (the JWT-authed
                    # runtime rejects this SigV4 call as an auth-method mismatch, or
                    # the session is already gone). Durability is already committed
                    # and idle reclaim frees the microVM, so finalize STOPPED rather
                    # than stranding the record at STOPPING until a manual reset.
                    _LOGGER.warning(
                        "Runtime stop rejected (status %d); finalizing STOPPED on the "
                        "committed checkpoint and leaving the session to idle reclaim.",
                        error.status_code,
                    )
                    return
                attempt += 1
                if attempt == self._max_stop_attempts:
                    raise ControlApiError(
                        503,
                        "THROTTLED",
                        "THROTTLING",
                        "Runtime stop is temporarily unavailable.",
                        retryable=True,
                    ) from error
                self._sleep(self._jitter(min(0.25 * (2 ** (attempt - 1)), 2.0)))

    def _idempotent(
        self, subject: str, operation: str, key: str, payload_hash: str
    ) -> dict[str, object] | None:
        existing = self._idempotency.get((subject, operation, key))
        if existing is None:
            return None
        if existing[0] != payload_hash:
            raise ControlApiError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "LIFECYCLE",
                "Idempotency key was reused with another request.",
            )
        return dict(existing[1])

    def _remember(
        self,
        subject: str,
        operation: str,
        key: str,
        payload_hash: str,
        response: dict[str, object],
    ) -> None:
        self._idempotency[(subject, operation, key)] = (payload_hash, dict(response))

    def _validate_origin(self, event: Mapping[str, object]) -> None:
        headers = _headers(event)
        origin = headers.get("origin")
        if origin is not None and origin != self._config.allowed_origin:
            raise ControlApiError(
                403, "AUTHORIZATION_FAILED", "AUTHORIZATION", "Origin is not allowed."
            )

    @staticmethod
    def _claims_from_event(context: Mapping[str, object]) -> Mapping[str, object] | None:
        authorizer = context.get("authorizer")
        if not isinstance(authorizer, dict):
            return None
        jwt = authorizer.get("jwt")
        if not isinstance(jwt, dict):
            return None
        claims = jwt.get("claims")
        return cast(Mapping[str, object], claims) if isinstance(claims, dict) else None

    @staticmethod
    def _body(event: Mapping[str, object]) -> Mapping[str, object]:
        raw = event.get("body")
        if not isinstance(raw, str):
            raise ControlApiError(400, "INVALID_MESSAGE", "LIFECYCLE", "JSON body is required.")
        try:
            value = cast(object, json.loads(raw))
        except json.JSONDecodeError as error:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "JSON body is invalid."
            ) from error
        if not isinstance(value, dict):
            raise ControlApiError(400, "INVALID_MESSAGE", "LIFECYCLE", "JSON body is invalid.")
        return cast(dict[str, object], value)

    @staticmethod
    def _idempotency_key(event: Mapping[str, object]) -> str:
        key = _headers(event).get("idempotency-key")
        if key is None or not 16 <= len(key) <= 128:
            raise ControlApiError(
                400, "INVALID_MESSAGE", "LIFECYCLE", "A valid idempotency key is required."
            )
        return key

    def _public_config(self) -> dict[str, object]:
        return {
            "deploymentMode": self._config.deployment_mode,
            "frontendCompatibilityVersion": self._config.frontend_compatibility_version,
            "protocolVersion": PROTOCOL_VERSION,
            "qualifier": self._config.qualifier,
            "region": self._config.region,
            "runtimeArn": self._config.runtime_arn,
        }

    @staticmethod
    def _sandbox(record: SandboxRecord) -> dict[str, object]:
        return {
            "lastCheckpointAt": None,
            "lastRestore": record.last_restore,
            "sandboxId": record.sandbox_id,
            "state": record.state.value,
            "stateVersion": record.state_version,
            "updatedAt": _timestamp(record.updated_at),
        }

    @staticmethod
    def _checkpoint(item: CheckpointMetadata) -> dict[str, object]:
        return {
            "createdAt": _timestamp(item.created_at),
            "generation": item.generation,
            "schemaVersion": item.schema_version,
            "status": item.status,
        }

    def _correlation_id(self, event: Mapping[str, object]) -> str:
        supplied = _headers(event).get("x-correlation-id")
        if (
            supplied is not None
            and len(supplied) == 26
            and all(character in _CORRELATION_ALPHABET for character in supplied)
        ):
            return supplied
        value = int.from_bytes(secrets.token_bytes(16))
        return "".join(
            _CORRELATION_ALPHABET[(value >> (5 * shift)) & 31] for shift in range(25, -1, -1)
        )

    def _response(self, status: int, body: object, correlation_id: str) -> dict[str, object]:
        return {
            "body": ""
            if status == 204
            else json.dumps(body, sort_keys=True, separators=(",", ":")),
            "headers": {
                "Access-Control-Allow-Headers": (
                    "Authorization,Content-Type,Idempotency-Key,X-Correlation-Id"
                ),
                "Access-Control-Allow-Methods": "GET,POST,DELETE,OPTIONS",
                "Access-Control-Allow-Origin": self._config.allowed_origin,
                "Content-Type": "application/json",
                "X-Correlation-Id": correlation_id,
            },
            "isBase64Encoded": False,
            "statusCode": status,
        }

    def _error(self, error: ControlApiError, correlation_id: str) -> dict[str, object]:
        return self._response(
            error.status,
            {
                "category": error.category,
                "code": error.code,
                "correlationId": correlation_id,
                "message": error.message,
                "retryable": error.retryable,
            },
            correlation_id,
        )


def _headers(event: Mapping[str, object]) -> dict[str, str]:
    value = event.get("headers")
    if not isinstance(value, dict):
        return {}
    return {
        str(key).lower(): str(item)
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _unbase64url(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
