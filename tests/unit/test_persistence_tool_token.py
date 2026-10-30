from __future__ import annotations

import base64
import email.message
import hashlib
import io
import json
import urllib.error
import urllib.parse
from collections.abc import Iterator
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from kirocrew_agentcore_persistence import lambda_handler as broker
from kirocrew_agentcore_persistence import tool_token

SANDBOX = "sbx_01J00000000000000000000000"
SUBJECT = "c95ac5ec-80f1-7057-f83b-2d4ed4f81658"
OWNER_HASH = hashlib.sha256(f"kirocrew-owner-v1\x00{SUBJECT}".encode()).hexdigest()
SEALED = base64.b64encode(b"sealed-blob").decode()
CONFIG = {
    "clientId": "machine-client",
    "clientSecret": "machine-secret",
    "tokenEndpoint": "https://pool.auth.example.amazoncognito.com/oauth2/token",
    "scope": "kirocrew.tools/invoke",
}


@pytest.fixture(autouse=True)
def _environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TOOL_TOKEN_SECRET_ARN", "arn:secret")
    monkeypatch.setenv("WAKE_SUBJECT_KEY_ARN", "arn:wake-key")
    monkeypatch.setenv("SANDBOX_TABLE", "table")
    monkeypatch.setattr(tool_token, "_config_cache", None)
    yield


class _Kms:
    def __init__(self, plaintext: bytes | None = SUBJECT.encode(), fail: bool = False) -> None:
        self.plaintext = plaintext
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise ClientError({"Error": {"Code": "InvalidCiphertextException"}}, "Decrypt")
        return {"Plaintext": self.plaintext}


class _Dynamo:
    def __init__(self, item: dict[str, Any]) -> None:
        self.item = item
        self.calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Item": self.item}


class _Secrets:
    def __init__(self, value: object = CONFIG) -> None:
        self.value = value
        self.calls = 0

    def get_secret_value(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        return {"SecretString": json.dumps(self.value)}


class _Response(io.BytesIO):
    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


ENROLLED = {"wakeSubject": {"S": SEALED}, "ownerHash": {"S": OWNER_HASH}}


def _opener(payload: object, seen: list[Any]) -> Any:
    def open_(request: Any, timeout: int) -> _Response:
        seen.append((request, timeout))
        return _Response(json.dumps(payload).encode())

    return open_


def test_issue_names_the_unsealed_owner_and_returns_only_the_token() -> None:
    seen: list[Any] = []
    kms = _Kms()
    result = tool_token.issue(
        kms,
        _Dynamo(ENROLLED),
        _Secrets(),
        SANDBOX,
        opener=_opener({"access_token": "tok", "expires_in": 900, "token_type": "Bearer"}, seen),
    )
    assert result == {"accessToken": "tok", "expiresIn": 900, "tokenType": "Bearer"}
    assert kms.calls[0]["EncryptionContext"] == {
        "purpose": "kirocrew-wake-subject",
        "sandboxId": SANDBOX,
    }
    request, timeout = seen[0]
    assert timeout == 10
    assert request.full_url == CONFIG["tokenEndpoint"]
    form = urllib.parse.parse_qs(request.data.decode())
    assert form["grant_type"] == ["client_credentials"]
    assert form["scope"] == ["kirocrew.tools/invoke"]
    assert json.loads(form["aws_client_metadata"][0]) == {"owner_sub": SUBJECT}
    expected = base64.b64encode(b"machine-client:machine-secret").decode()
    assert request.get_header("Authorization") == f"Basic {expected}"


def test_configuration_is_read_once_per_container() -> None:
    secrets = _Secrets()
    for _ in range(2):
        tool_token.issue(
            _Kms(),
            _Dynamo(ENROLLED),
            secrets,
            SANDBOX,
            opener=_opener({"access_token": "tok", "expires_in": 900}, []),
        )
    assert secrets.calls == 1


@pytest.mark.parametrize(
    "item",
    [
        {},
        {"ownerHash": {"S": OWNER_HASH}},
        {"wakeSubject": {"S": SEALED}},
        {"wakeSubject": {"S": "not base64!"}, "ownerHash": {"S": OWNER_HASH}},
        {"wakeSubject": {"S": SEALED}, "ownerHash": {"S": "0" * 64}},
    ],
)
def test_unenrolled_or_mismatched_owner_gets_no_token(item: dict[str, Any]) -> None:
    with pytest.raises(tool_token.ToolTokenUnavailableError):
        tool_token.issue(_Kms(), _Dynamo(item), _Secrets(), SANDBOX, opener=_opener({}, []))


@pytest.mark.parametrize("kms", [_Kms(fail=True), _Kms(plaintext=b""), _Kms(plaintext=None)])
def test_owner_record_that_does_not_open_gets_no_token(kms: _Kms) -> None:
    with pytest.raises(tool_token.ToolTokenUnavailableError):
        tool_token.issue(kms, _Dynamo(ENROLLED), _Secrets(), SANDBOX, opener=_opener({}, []))


@pytest.mark.parametrize(
    "payload",
    [[], {"expires_in": 900}, {"access_token": "", "expires_in": 900}, {"access_token": "t"}],
)
def test_malformed_token_responses_are_refused(payload: object) -> None:
    with pytest.raises(tool_token.ToolTokenUnavailableError):
        tool_token.issue(
            _Kms(), _Dynamo(ENROLLED), _Secrets(), SANDBOX, opener=_opener(payload, [])
        )


def test_a_refused_owner_does_not_leak_cognitos_reason() -> None:
    def refuse(request: Any, timeout: int) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            email.message.Message(),
            io.BytesIO(b"unknown owner sub"),
        )

    with pytest.raises(tool_token.ToolTokenUnavailableError) as raised:
        tool_token.issue(_Kms(), _Dynamo(ENROLLED), _Secrets(), SANDBOX, opener=refuse)
    assert "unknown owner sub" not in str(raised.value)


@pytest.mark.parametrize(
    "config",
    [
        {**CONFIG, "clientSecret": ""},
        {key: value for key, value in CONFIG.items() if key != "scope"},
        {**CONFIG, "tokenEndpoint": "http://pool.example/oauth2/token"},
        ["not", "an", "object"],
    ],
)
def test_incomplete_or_plain_http_configuration_is_rejected(config: object) -> None:
    with pytest.raises(RuntimeError):
        tool_token.issue(
            _Kms(), _Dynamo(ENROLLED), _Secrets(config), SANDBOX, opener=_opener({}, [])
        )


def test_disabled_without_a_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOOL_TOKEN_SECRET_ARN")
    assert not tool_token.enabled()
    with pytest.raises(tool_token.ToolTokenUnavailableError):
        tool_token.issue(_Kms(), _Dynamo(ENROLLED), _Secrets(), SANDBOX, opener=_opener({}, []))


def test_handler_routes_tool_token_after_authorizing_the_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(broker, "_clients", lambda: ("s3", "kms", "ddb"))
    monkeypatch.setattr(broker, "_binding", lambda _kms, _token: {"ok": True})

    def authorize(_ddb: object, _claims: object, _event: object) -> str:
        order.append("authorize")
        return SANDBOX

    def issue(kms: object, dynamodb: object, secrets: object, sandbox_id: str) -> dict[str, object]:
        order.append("issue")
        assert (kms, dynamodb, sandbox_id) == ("kms", "ddb", SANDBOX)
        return {"accessToken": "tok"}

    monkeypatch.setattr(broker, "_authorize", authorize)
    monkeypatch.setattr(tool_token, "issue", issue)
    monkeypatch.setattr(vars(broker)["boto3"], "client", lambda name: name)
    result = broker.handler({"operation": "toolToken", "bindingToken": "t"}, None)
    assert result == {"accessToken": "tok"}
    assert order == ["authorize", "issue"]
