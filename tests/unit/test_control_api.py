from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from cryptography.exceptions import InvalidSignature
from kirocrew_agentcore_control.api import (
    PROTOCOL_VERSION,
    ROUTES,
    AgentCoreStopError,
    CheckpointMetadata,
    ClaimsValidator,
    ControlApiError,
    ControlConfig,
    Identity,
    InMemoryCheckpointCatalog,
    LocalAsymmetricKmsSigner,
    SandboxControlService,
    SignedControlTokens,
)
from kirocrew_agentcore_control.sandbox import (
    InMemorySandboxRegistry,
    SandboxRecord,
    SandboxRegistryError,
    SandboxState,
)

SUBJECT = "subject-a"
OTHER_SUBJECT = "subject-b"
ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example"
CLIENT = "client-id"
ORIGIN = "https://app.example.com"
CORRELATION_ID = "01J00000000000000000000000"


@dataclass
class MutableClock:
    now: datetime = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class FakeStopper:
    def __init__(self, outcomes: Sequence[int | None] | None = None) -> None:
        self.outcomes = list(outcomes or [None])
        self.calls: list[str] = []

    def stop(self, runtime_session_id: str) -> None:
        self.calls.append(runtime_session_id)
        outcome = self.outcomes.pop(0)
        if outcome is not None:
            raise AgentCoreStopError(outcome)


class BrokenRegistry(InMemorySandboxRegistry):
    def get_or_create(self, cognito_subject: str) -> SandboxRecord:
        raise SandboxRegistryError("secret internal detail")


def config(**changes: object) -> ControlConfig:
    values: dict[str, object] = {
        "allowed_origin": ORIGIN,
        "app_client_id": CLIENT,
        "deployment_mode": "microvm",
        "frontend_compatibility_version": "1.2.3",
        "http_url": "https://agentcore.example.com/invocations",
        "issuer": ISSUER,
        "qualifier": "DEFAULT",
        "region": "us-east-1",
        "runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:111122223333:runtime/example",
        "token_audience": "kirocrew-runtime",
        "websocket_url": "wss://agentcore.example.com/ws",
    }
    values.update(changes)
    return ControlConfig(**values)  # type: ignore[arg-type]


def claims(
    subject: str = SUBJECT,
    *,
    clock: MutableClock | None = None,
    admin: bool = False,
    **changes: object,
) -> dict[str, object]:
    selected = clock or MutableClock()
    value: dict[str, object] = {
        "client_id": CLIENT,
        "cognito:groups": ["sandbox-admins"] if admin else [],
        "exp": int((selected.now + timedelta(hours=1)).timestamp()),
        "iss": ISSUER,
        "scope": "openid kirocrew.control",
        "sub": subject,
        "token_use": "access",
    }
    value.update(changes)
    return value


def event(
    method: str,
    path: str,
    *,
    claim_values: dict[str, object] | None = None,
    body: object | None = None,
    headers: dict[str, object] | None = None,
) -> dict[str, object]:
    request_context: dict[str, object] = {"http": {"method": method}}
    if claim_values is not None:
        request_context["authorizer"] = {"jwt": {"claims": claim_values}}
    selected_headers: dict[str, object] = {
        "origin": ORIGIN,
        "x-correlation-id": CORRELATION_ID,
    }
    if headers:
        selected_headers.update(headers)
    result: dict[str, object] = {
        "headers": selected_headers,
        "rawPath": path,
        "requestContext": request_context,
    }
    if body is not None:
        result["body"] = body if isinstance(body, str) else json.dumps(body)
    return result


def response_body(response: dict[str, object]) -> dict[str, object] | list[object]:
    body = response["body"]
    assert isinstance(body, str)
    value = json.loads(body)
    assert isinstance(value, dict | list)
    return cast(dict[str, object] | list[object], value)


def service(
    *,
    clock: MutableClock | None = None,
    registry: InMemorySandboxRegistry | None = None,
    stopper: FakeStopper | None = None,
    signer: LocalAsymmetricKmsSigner | None = None,
    max_attempts: int = 4,
    config_value: ControlConfig | None = None,
) -> tuple[
    SandboxControlService,
    InMemorySandboxRegistry,
    InMemoryCheckpointCatalog,
    SignedControlTokens,
    FakeStopper,
    list[float],
]:
    selected_clock = clock or MutableClock()
    selected_registry = registry or InMemorySandboxRegistry(
        sandbox_id_factory=lambda: "sbx_01J00000000000000000000000",
        runtime_session_id_factory=lambda: "7c0a2b3e-7d94-4ce7-a41b-5888a53159f4",
        clock=selected_clock,
    )
    selected_signer = signer or LocalAsymmetricKmsSigner.generate()
    tokens = SignedControlTokens(
        selected_signer,
        "kirocrew-runtime",
        clock=selected_clock,
        nonce_factory=lambda: "fixed-nonce",
    )
    catalog = InMemoryCheckpointCatalog()
    selected_stopper = stopper or FakeStopper()
    sleeps: list[float] = []
    control = SandboxControlService(
        config_value or config(),
        selected_registry,
        ClaimsValidator(ISSUER, CLIENT, clock=selected_clock),
        tokens,
        catalog,
        selected_stopper,
        sleep=sleeps.append,
        jitter=lambda cap: cap,
        max_stop_attempts=max_attempts,
    )
    return control, selected_registry, catalog, tokens, selected_stopper, sleeps


def start_event(subject: str = SUBJECT, key: str = "start-key-0000001") -> dict[str, object]:
    return event(
        "POST",
        "/control/v1/sandbox/start",
        claim_values=claims(subject),
        headers={"Idempotency-Key": key},
    )


def prepare_stopping(
    registry: InMemorySandboxRegistry,
    catalog: InMemoryCheckpointCatalog,
    tokens: SignedControlTokens,
    *,
    subject: str = SUBJECT,
    generation: int = 1,
    digest: str = "a" * 64,
) -> str:
    record = registry.get_or_create(subject)
    lease = registry.acquire_start(subject, record.runtime_session_id, ttl=timedelta(seconds=90))
    record = registry.transition(
        subject,
        SandboxState.RESTORING,
        expected_version=lease.record.state_version,
        lease_owner=record.runtime_session_id,
    )
    record = registry.transition(
        subject,
        SandboxState.READY,
        expected_version=record.state_version,
        lease_owner=record.runtime_session_id,
    )
    record = registry.transition(
        subject,
        SandboxState.CHECKPOINTING,
        expected_version=record.state_version,
        lease_owner=record.runtime_session_id,
    )
    record = registry.record_checkpoint(subject, record.runtime_session_id, generation)
    record = registry.transition(
        subject,
        SandboxState.STOPPING,
        expected_version=record.state_version,
        lease_owner=record.runtime_session_id,
    )
    catalog.record(record.sandbox_id, CheckpointMetadata(generation, digest, record.updated_at))
    return tokens.issue_checkpoint_receipt(
        Identity(subject, ISSUER, False), record, generation, digest
    )


def test_public_config_preflight_cors_route_and_request_validation() -> None:
    control, _, _, _, _, _ = service()
    public = control.handle(event("GET", "/control/v1/config"))
    assert public["statusCode"] == 200
    body = response_body(public)
    assert isinstance(body, dict)
    assert body["runtimeArn"] == config().runtime_arn
    assert body["protocolVersion"] == PROTOCOL_VERSION
    assert "app_client_id" not in str(public)
    preflight = control.handle(event("OPTIONS", "/control/v1/sandbox"))
    assert preflight["statusCode"] == 204
    assert preflight["body"] == ""
    bad_origin = control.handle(
        event("GET", "/control/v1/config", headers={"origin": "https://evil.example"})
    )
    assert bad_origin["statusCode"] == 403
    assert control.handle(event("GET", "/missing"))["statusCode"] == 404
    assert control.handle({})["statusCode"] == 400


def test_claim_validation_matrix_and_no_bearer_token_reflection() -> None:
    control, _, _, _, _, _ = service()
    path = "/control/v1/sandbox"
    non_list_groups = claims()
    non_list_groups["cognito:groups"] = "sandbox-admins"
    api_gateway_claims = claims()
    api_gateway_claims["exp"] = str(api_gateway_claims["exp"])
    cases = [
        (None, 401),
        (claims(iss="wrong"), 401),
        (claims(client_id="wrong"), 401),
        (claims(exp=1), 401),
        (claims(exp="not-a-timestamp"), 401),
        (claims(scope="openid"), 403),
        (claims(token_use="id"), 401),  # noqa: S106 - Cognito enum fixture
        (claims(sub=""), 401),
        (non_list_groups, 200),
        (api_gateway_claims, 200),
    ]
    for claim_values, status in cases:
        response = control.handle(event("GET", path, claim_values=claim_values))
        assert response["statusCode"] == status
        assert "bearer-secret-value" not in str(response)
    malformed_authorizer = event("GET", path)
    context = cast(dict[str, object], malformed_authorizer["requestContext"])
    context["authorizer"] = {"jwt": []}
    assert control.handle(malformed_authorizer)["statusCode"] == 401
    with pytest.raises(ValueError, match="Issuer"):
        ClaimsValidator("", CLIENT)


def test_get_sandbox_uses_claim_subject_and_non_enumerating_isolation() -> None:
    control, registry, _, _, _, _ = service()
    first = control.handle(event("GET", "/control/v1/sandbox", claim_values=claims()))
    second = control.handle(event("GET", "/control/v1/sandbox", claim_values=claims(OTHER_SUBJECT)))
    assert first["statusCode"] == second["statusCode"] == 200
    first_body = response_body(first)
    second_body = response_body(second)
    assert isinstance(first_body, dict) and isinstance(second_body, dict)
    assert (
        first_body["sandboxId"] != second_body["sandboxId"]
        or registry.get(SUBJECT).owner_hash != registry.get(OTHER_SUBJECT).owner_hash
    )
    assert first["headers"]["X-Correlation-Id"] == CORRELATION_ID  # type: ignore[index]


def test_start_is_idempotent_and_returns_only_configured_runtime_descriptor() -> None:
    control, registry, _, _, _, _ = service()
    first = control.handle(start_event())
    duplicate = control.handle(start_event())
    assert first == duplicate
    assert first["statusCode"] == 200
    body = response_body(first)
    assert isinstance(body, dict)
    assert body["runtimeArn"] == config().runtime_arn
    assert body["state"] == "STARTING"
    assert set(body) == {
        "bindingToken",
        "expiresAt",
        "frontendCompatibilityVersion",
        "httpUrl",
        "protocolVersion",
        "qualifier",
        "runtimeArn",
        "runtimeSessionId",
        "sandboxId",
        "state",
        "webSocketUrl",
    }
    assert registry.get(SUBJECT).state_version == 1
    another_key = control.handle(start_event(key="start-key-0000002"))
    assert another_key["statusCode"] == 200
    assert registry.get(SUBJECT).state_version == 1
    missing = control.handle(event("POST", "/control/v1/sandbox/start", claim_values=claims()))
    assert missing["statusCode"] == 400


def test_signed_tokens_reject_tampering_expiry_identity_and_bad_shapes() -> None:
    clock = MutableClock()
    signer = LocalAsymmetricKmsSigner.generate()
    control, registry, _, tokens, _, _ = service(clock=clock, signer=signer)
    start = control.handle(start_event())
    assert start["statusCode"] == 200
    record = registry.get(SUBJECT)
    identity = Identity(SUBJECT, ISSUER, False)
    binding, expires = tokens.issue_binding(identity, record)
    assert expires == clock.now + timedelta(minutes=30)
    assert SUBJECT not in binding
    with pytest.raises(ValueError, match="audience"):
        SignedControlTokens(LocalAsymmetricKmsSigner.generate(), "")
    with pytest.raises(ValueError, match="generation"):
        tokens.issue_checkpoint_receipt(identity, record, 0, "bad")
    receipt = tokens.issue_checkpoint_receipt(identity, record, 1, "a" * 64)
    assert tokens.verify_checkpoint_receipt(receipt, identity, record) == (1, "a" * 64)
    with pytest.raises(ControlApiError, match="invalid"):
        tokens.verify_checkpoint_receipt(receipt + "x", identity, record)
    with pytest.raises(ControlApiError, match="does not match"):
        tokens.verify_checkpoint_receipt(receipt, Identity(OTHER_SUBJECT, ISSUER, False), record)
    clock.advance(timedelta(minutes=10))
    with pytest.raises(ControlApiError, match="stale"):
        tokens.verify_checkpoint_receipt(receipt, identity, record)
    with pytest.raises(ControlApiError, match="invalid"):
        tokens.verify_checkpoint_receipt("not-a-token", identity, record)
    encoded = base64.urlsafe_b64encode(b"[]").rstrip(b"=").decode()
    signature = base64.urlsafe_b64encode(signer.sign(encoded.encode())).rstrip(b"=").decode()
    with pytest.raises(ControlApiError, match="invalid"):
        tokens.verify_checkpoint_receipt(f"{encoded}.{signature}", identity, record)


def test_checkpoint_catalog_and_listing_expose_no_storage_keys_or_digests() -> None:
    control, registry, catalog, _, _, _ = service()
    record = registry.get_or_create(SUBJECT)
    catalog.record(
        record.sandbox_id,
        CheckpointMetadata(2, "b" * 64, MutableClock().now, status="RESTORE_FAILED"),
    )
    response = control.handle(
        event("GET", "/control/v1/sandbox/checkpoints", claim_values=claims())
    )
    assert response["statusCode"] == 200
    body = response_body(response)
    assert body == [
        {
            "createdAt": "2026-08-17T16:00:00Z",
            "generation": 2,
            "schemaVersion": 1,
            "status": "RESTORE_FAILED",
        }
    ]
    assert "snapshots/" not in str(response)
    assert "bbbbbbbb" not in str(response)
    with pytest.raises(ValueError, match="metadata"):
        catalog.record(record.sandbox_id, CheckpointMetadata(0, "bad", MutableClock().now))
    assert catalog.get("missing", 1) is None


def test_stop_verifies_current_commit_retries_and_is_idempotent() -> None:
    stopper = FakeStopper([409, 429, 500, None])
    control, registry, catalog, tokens, _, sleeps = service(stopper=stopper)
    receipt = prepare_stopping(registry, catalog, tokens)
    stop_event = event(
        "POST",
        "/control/v1/sandbox/stop",
        claim_values=claims(),
        body={"checkpointReceipt": receipt},
        headers={"Idempotency-Key": "stop-key-00000001"},
    )
    response = control.handle(stop_event)
    assert response["statusCode"] == 202
    stopped_body = response_body(response)
    assert isinstance(stopped_body, dict)
    assert stopped_body["state"] == "STOPPED"
    assert len(stopper.calls) == 4
    assert sleeps == [0.25, 0.5, 1.0]
    duplicate = control.handle(stop_event)
    assert duplicate == response
    assert len(stopper.calls) == 4


def test_stop_rejects_cross_user_stale_uncommitted_and_changed_idempotency() -> None:
    control, registry, catalog, tokens, _, _ = service()
    receipt = prepare_stopping(registry, catalog, tokens)

    def stop_request(subject: str, token: str, key: str = "stop-key-00000001") -> dict[str, object]:
        return event(
            "POST",
            "/control/v1/sandbox/stop",
            claim_values=claims(subject),
            body={"checkpointReceipt": token},
            headers={"Idempotency-Key": key},
        )

    cross_user = control.handle(stop_request(OTHER_SUBJECT, receipt))
    assert cross_user["statusCode"] in {404, 409}
    invalid = control.handle(stop_request(SUBJECT, "invalid-token-value"))
    assert invalid["statusCode"] == 409
    catalog._items[registry.get(SUBJECT).sandbox_id][1] = replace(
        cast(CheckpointMetadata, catalog.get(registry.get(SUBJECT).sandbox_id, 1)),
        status="RESTORE_FAILED",
    )
    uncommitted = control.handle(stop_request(SUBJECT, receipt))
    assert uncommitted["statusCode"] == 409
    catalog._items[registry.get(SUBJECT).sandbox_id][1] = replace(
        cast(CheckpointMetadata, catalog.get(registry.get(SUBJECT).sandbox_id, 1)),
        status="COMMITTED",
    )
    success = control.handle(stop_request(SUBJECT, receipt))
    assert success["statusCode"] == 202
    conflict = control.handle(stop_request(SUBJECT, receipt + "changed"))
    assert conflict["statusCode"] == 409
    conflict_body = response_body(conflict)
    assert isinstance(conflict_body, dict)
    assert conflict_body["code"] == "IDEMPOTENCY_CONFLICT"


def test_stop_body_validation_nonretryable_and_retry_exhaustion_preserve_state() -> None:
    body_cases: tuple[object, ...] = (
        None,
        "not-json",
        [],
        {},
        {"checkpointReceipt": "x", "extra": True},
    )
    for body in body_cases:
        control, _, _, _, _, _ = service()
        response = control.handle(
            event(
                "POST",
                "/control/v1/sandbox/stop",
                claim_values=claims(),
                body=body,
                headers={"Idempotency-Key": "stop-key-00000001"},
            )
        )
        assert response["statusCode"] in {400, 404}
    retry_cases: tuple[tuple[Sequence[int | None], int, bool], ...] = (
        ([400], 502, False),
        ([503, 503], 503, True),
    )
    for outcomes, status, retryable in retry_cases:
        stopper = FakeStopper(outcomes)
        control, registry, catalog, tokens, _, _ = service(
            stopper=stopper, max_attempts=len(outcomes)
        )
        receipt = prepare_stopping(registry, catalog, tokens)
        response = control.handle(
            event(
                "POST",
                "/control/v1/sandbox/stop",
                claim_values=claims(),
                body={"checkpointReceipt": receipt},
                headers={"Idempotency-Key": "stop-key-00000001"},
            )
        )
        assert response["statusCode"] == status
        body_value = response_body(response)
        assert isinstance(body_value, dict) and body_value["retryable"] is retryable
        assert registry.get(SUBJECT).state is SandboxState.STOPPING


def test_administrator_only_deletion_requires_stopped_sandbox() -> None:
    control, registry, _, _, _, _ = service()
    registry.get_or_create(SUBJECT)
    denied = control.handle(event("DELETE", "/control/v1/sandbox", claim_values=claims()))
    assert denied["statusCode"] == 403
    accepted = control.handle(
        event("DELETE", "/control/v1/sandbox", claim_values=claims(admin=True))
    )
    assert accepted["statusCode"] == 202
    assert registry.get(SUBJECT).deletion_state == "REQUESTED"

    active_control, active_registry, _, _, _, _ = service()
    active_control.handle(start_event())
    conflict = active_control.handle(
        event("DELETE", "/control/v1/sandbox", claim_values=claims(admin=True))
    )
    assert conflict["statusCode"] == 409
    assert active_registry.get(SUBJECT).deletion_state is None


def test_service_configuration_internal_error_and_defensive_helpers() -> None:
    with pytest.raises(ValueError, match="configuration"):
        config(allowed_origin="http://insecure.example")
    with pytest.raises(ValueError, match="positive"):
        SandboxControlService(
            config(),
            InMemorySandboxRegistry(),
            ClaimsValidator(ISSUER, CLIENT),
            SignedControlTokens(LocalAsymmetricKmsSigner.generate(), "aud"),
            InMemoryCheckpointCatalog(),
            FakeStopper(),
            max_stop_attempts=0,
        )
    broken = BrokenRegistry()
    control, _, _, _, _, _ = service(registry=broken)
    response = control.handle(event("GET", "/control/v1/sandbox", claim_values=claims()))
    assert response["statusCode"] == 500
    assert "secret internal detail" not in str(response)
    generated = control.handle(
        {
            "headers": {1: "ignored", "origin": ORIGIN, "x-correlation-id": "invalid"},
            "rawPath": "/control/v1/config",
            "requestContext": {"http": {"method": "GET"}},
        }
    )
    headers = cast(dict[str, str], generated["headers"])
    assert len(headers["X-Correlation-Id"]) == 26
    no_headers = control.handle(
        {
            "headers": [],
            "rawPath": "/control/v1/config",
            "requestContext": {"http": {"method": "GET"}},
        }
    )
    assert no_headers["statusCode"] == 200


def test_local_signer_reports_invalid_signature() -> None:
    signer = LocalAsymmetricKmsSigner.generate()
    signature = signer.sign(b"message")
    signer.verify(b"message", signature)
    with pytest.raises(InvalidSignature):
        signer.verify(b"other", signature)
    assert AgentCoreStopError(409).status_code == 409


def test_route_table_matches_canonical_control_operations() -> None:
    assert set(ROUTES.values()) == {
        "deleteSandbox",
        "getPublicConfig",
        "getSandbox",
        "getSandboxHistory",
        "listCheckpoints",
        "startSandbox",
        "stopSandbox",
    }
    assert Path("contracts/openapi.yaml").is_file()


def test_history_route_reports_observed_transitions_and_persisted_paths() -> None:
    control, registry, _, _, _, _ = service(
        config_value=config(persisted_paths=("/mnt/workspace/projects", "/mnt/workspace/user"))
    )
    # The status poll observes the initial state...
    first = control.handle(event("GET", "/control/v1/sandbox", claim_values=claims()))
    assert first["statusCode"] == 200
    # ...and the history route returns it alongside the persisted paths.
    response = control.handle(event("GET", "/control/v1/sandbox/history", claim_values=claims()))
    assert response["statusCode"] == 200
    body = cast(dict[str, Any], response_body(response))
    assert body["persistedPaths"] == ["/mnt/workspace/projects", "/mnt/workspace/user"]
    events = cast(list[dict[str, Any]], body["events"])
    assert len(events) == 1
    assert events[0]["state"] == "STOPPED"
    assert events[0]["stateVersion"] == 0
    assert cast(str, events[0]["at"]).endswith("Z")


def test_in_memory_history_rejects_a_nonpositive_limit() -> None:
    _, registry, _, _, _, _ = service()
    record = registry.get_or_create(SUBJECT)
    registry.record_observation(record)
    registry.record_observation(record)  # same version: deduplicated
    assert len(registry.history(record.sandbox_id)) == 1
    with pytest.raises(ValueError, match="positive"):
        registry.history(record.sandbox_id, limit=0)


def test_authoritative_start_rotates_the_runtime_session_and_resume_does_not() -> None:
    clock = MutableClock()
    registry = InMemorySandboxRegistry(clock=clock)
    record = registry.get_or_create(SUBJECT)
    original = record.runtime_session_id
    lease = registry.acquire_start(SUBJECT, "owner-a", ttl=timedelta(seconds=90))
    # A dead session's id must never be reused: the corpse poisons every
    # restart until the platform reclaims it.
    assert lease.record.runtime_session_id != original
    # While the lease is live, reconnects keep the same session.
    resumed = registry.acquire_start(SUBJECT, "owner-a", ttl=timedelta(seconds=90))
    assert resumed.record.runtime_session_id == lease.record.runtime_session_id
