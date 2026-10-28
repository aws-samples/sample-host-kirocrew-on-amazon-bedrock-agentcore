"""Multi-owner scheduled wake (#39, step 2).

The invariants worth pinning are the ones a reviewer cannot see from a diff:
the scheduled tick decrypts nothing, a leg carries only a sandbox id, and every
way a sealed subject can be wrong ends in a logged refusal rather than a wake
of somebody else's workspace -- or an async retry storm.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

WAKER_SRC = Path(__file__).resolve().parents[2] / "infrastructure" / "functions" / "waker" / "src"
if str(WAKER_SRC) not in sys.path:
    sys.path.insert(0, str(WAKER_SRC))

SUBJECT = "191ac5ec-0000-4000-8000-000000000001"
OWNER_HASH = hashlib.sha256(f"kirocrew-owner-v1\x00{SUBJECT}".encode()).hexdigest()
KEY_ARN = "arn:aws:kms:ap-southeast-1:1:key/wake"
SEALED = base64.b64encode(b"ciphertext").decode()


class _Dynamo:
    def __init__(self, records: dict[str, dict[str, Any]], pages: int = 1) -> None:
        self.records = records
        self.pages = pages
        self.scans: list[dict[str, Any]] = []

    def scan(self, **kwargs: Any) -> dict[str, Any]:
        self.scans.append(kwargs)
        ids = sorted(sid for sid, r in self.records.items() if "wakeSubject" in r)
        page = len(self.scans) - 1
        size = max(1, -(-len(ids) // self.pages))
        chunk = ids[page * size : (page + 1) * size]
        answer: dict[str, Any] = {"Items": [{"pk": {"S": f"SANDBOX#{sid}"}} for sid in chunk]}
        if page + 1 < self.pages:
            answer["LastEvaluatedKey"] = {"pk": {"S": f"page{page}"}}
        return answer

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        sandbox_id = kwargs["Key"]["pk"]["S"].removeprefix("SANDBOX#")
        record = self.records.get(sandbox_id)
        return {"Item": record} if record is not None else {}


class _Kms:
    def __init__(self, plaintext: bytes = SUBJECT.encode(), fail: bool = False) -> None:
        self.plaintext = plaintext
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    def decrypt(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise ClientError(
                {"Error": {"Code": "InvalidCiphertextException", "Message": "context"}},
                "Decrypt",
            )
        return {"Plaintext": self.plaintext}


class _Cognito:
    def __init__(self, users: list[dict[str, Any]]) -> None:
        self.users = users
        self.calls: list[dict[str, Any]] = []

    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"Users": self.users}


class _Lambda:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"StatusCode": 202}


class _Context:
    invoked_function_arn = "arn:aws:lambda:ap-southeast-1:1:function:waker"


def _session(clients: dict[str, Any]) -> Any:
    class _Session:
        def client(self, name: str, **kwargs: Any) -> Any:
            del kwargs
            return clients[name]

    return _Session()


def _stopped(due: int, **extra: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "state": {"S": "STOPPED"},
        "updatedAt": {"S": "2026-10-01T00:00:00Z"},
        "nextDueAt": {"N": str(due)},
    }
    record.update(extra)
    return record


@pytest.fixture
def waker(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name, value in {
        "WAKE_SUBJECT_KEY_ARN": KEY_ARN,
        "SANDBOX_TABLE_NAME": "kirocrew-sandboxes",
        "CONTROL_FUNCTION_NAME": "kirocrew-control",
        "MACHINE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:ap-southeast-1:1:runtime/machine",
        "MACHINE_ENDPOINT_QUALIFIER": "machine_live",
        "COGNITO_USER_POOL_ID": "ap-southeast-1_pool",
        "WAKE_GATE": "on",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("COGNITO_SUBJECT", "SANDBOX_ID", "WAKE_MAX_DISPATCH"):
        monkeypatch.delenv(name, raising=False)
    module = importlib.import_module("kirocrew_agentcore_waker.lambda_handler")
    return importlib.reload(module)


def test_multi_owner_mode_follows_the_key(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert waker._multi_owner() is True
    monkeypatch.delenv("WAKE_SUBJECT_KEY_ARN")
    assert waker._multi_owner() is False


def test_the_tick_starts_one_leg_per_due_sandbox_and_decrypts_nothing(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    monkeypatch.setattr(waker.time, "time", lambda: float(now))
    dynamo = _Dynamo(
        {
            "sbx_DUE": _stopped(now + 60, wakeSubject={"S": SEALED}),
            "sbx_LATER": _stopped(now + 86_400, wakeSubject={"S": SEALED}),
            "sbx_UNENROLLED": _stopped(now + 60),
        },
        pages=2,
    )
    kms, lam = _Kms(), _Lambda()
    monkeypatch.setattr(
        waker.boto3,
        "Session",
        lambda *a, **k: _session({"dynamodb": dynamo, "kms": kms, "lambda": lam}),
    )

    result = waker.handler({}, _Context())

    assert result == {"outcome": "dispatched", "enrolled": 2, "started": ["sbx_DUE"]}
    assert len(dynamo.scans) == 2  # pagination followed
    assert dynamo.scans[1]["ExclusiveStartKey"] == {"pk": {"S": "page0"}}
    assert kms.calls == []
    (leg,) = lam.calls
    assert leg["InvocationType"] == "Event"
    assert leg["FunctionName"] == _Context.invoked_function_arn
    assert json.loads(leg["Payload"]) == {"sandboxId": "sbx_DUE"}


def test_a_flood_of_due_sandboxes_is_capped(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 2_000_000_000
    monkeypatch.setattr(waker.time, "time", lambda: float(now))
    monkeypatch.setenv("WAKE_MAX_DISPATCH", "2")
    records = {f"sbx_{i}": _stopped(now + 30, wakeSubject={"S": SEALED}) for i in range(5)}
    lam = _Lambda()
    monkeypatch.setattr(
        waker.boto3,
        "Session",
        lambda *a, **k: _session({"dynamodb": _Dynamo(records), "lambda": lam}),
    )

    result = waker.handler({}, _Context())

    assert len(result["started"]) == 2
    assert len(lam.calls) == 2


def test_a_quiet_tick_creates_no_lambda_client(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(waker.boto3, "Session", lambda *a, **k: _session({"dynamodb": _Dynamo({})}))
    assert waker.handler({}, _Context()) == {"outcome": "dispatched", "enrolled": 0, "started": []}


def test_a_due_tick_without_a_name_cannot_dispatch(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 2_000_000_000
    monkeypatch.setattr(waker.time, "time", lambda: float(now))
    monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
    records = {"sbx_DUE": _stopped(now + 30, wakeSubject={"S": SEALED})}
    monkeypatch.setattr(
        waker.boto3,
        "Session",
        lambda *a, **k: _session({"dynamodb": _Dynamo(records), "lambda": _Lambda()}),
    )
    with pytest.raises(RuntimeError, match="name itself"):
        waker.handler({}, object())


def _opener(
    waker: Any,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, Any],
    kms: _Kms | None = None,
    users: list[dict[str, Any]] | None = None,
) -> tuple[_Kms, _Cognito]:
    kms = kms or _Kms()
    cognito = _Cognito([{"Enabled": True}] if users is None else users)
    clients = {"dynamodb": _Dynamo({"sbx_A": record}), "kms": kms, "cognito-idp": cognito}
    monkeypatch.setattr(waker.boto3, "Session", lambda *a, **k: _session(clients))
    return kms, cognito


def _enrolled() -> dict[str, Any]:
    return {"wakeSubject": {"S": SEALED}, "ownerHash": {"S": OWNER_HASH}}


def test_a_sealed_subject_opens_only_under_its_own_context(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    kms, cognito = _opener(waker, monkeypatch, _enrolled())
    assert waker._open_subject(waker.boto3.Session(), "sbx_A") == SUBJECT
    (call,) = kms.calls
    assert call["KeyId"] == KEY_ARN
    assert call["CiphertextBlob"] == b"ciphertext"
    assert call["EncryptionContext"] == {"purpose": "kirocrew-wake-subject", "sandboxId": "sbx_A"}
    assert cognito.calls[0]["Filter"] == f'sub = "{SUBJECT}"'


@pytest.mark.parametrize(
    ("record", "kms", "users", "message"),
    [
        ({"ownerHash": {"S": OWNER_HASH}}, None, None, "not enrolled"),
        ({**_enrolled(), "wakeSubject": {"S": "@@not-base64@@"}}, None, None, "does not open"),
        (_enrolled(), _Kms(fail=True), None, "does not open"),
        (_enrolled(), _Kms(plaintext=b""), None, "is empty"),
        (_enrolled(), _Kms(plaintext=b"someone-else-0000"), None, "does not match"),
        (_enrolled(), None, [], "disabled or no longer exists"),
        (_enrolled(), None, [{"Enabled": False}], "disabled or no longer exists"),
    ],
)
def test_every_bad_enrolment_is_refused(
    waker: Any,
    monkeypatch: pytest.MonkeyPatch,
    record: dict[str, Any],
    kms: _Kms | None,
    users: list[dict[str, Any]] | None,
    message: str,
) -> None:
    _opener(waker, monkeypatch, record, kms, users)
    with pytest.raises(waker.WakeRefusedError, match=message):
        waker._open_subject(waker.boto3.Session(), "sbx_A")


def test_a_non_uuid_subject_never_reaches_the_cognito_filter(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    odd = 'x" or sub ^= "'
    record = {
        "wakeSubject": {"S": SEALED},
        "ownerHash": {"S": hashlib.sha256(f"kirocrew-owner-v1\x00{odd}".encode()).hexdigest()},
    }
    _, cognito = _opener(waker, monkeypatch, record, _Kms(plaintext=odd.encode()))
    with pytest.raises(waker.WakeRefusedError, match="disabled"):
        waker._open_subject(waker.boto3.Session(), "sbx_A")
    assert cognito.calls == []


def test_without_a_pool_the_enabled_check_is_skipped(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("COGNITO_USER_POOL_ID")
    assert waker._subject_enabled(object(), SUBJECT) is True


def test_a_refused_leg_returns_instead_of_raising(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Raising would make the async invoke retry a decision twice more.
    _opener(waker, monkeypatch, _enrolled(), users=[])
    result = waker.handler({"sandboxId": "sbx_A"}, _Context())
    assert result["outcome"] == "refused"
    assert result["sandboxId"] == "sbx_A"


def test_a_relay_in_multi_owner_mode_carries_only_the_sandbox_id(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    lam = _Lambda()
    waker._relay(lam, _Context(), 2, 41, "sbx_A")
    payload = json.loads(lam.calls[0]["Payload"])
    assert payload == {"relay": 2, "generationBefore": 41, "sandboxId": "sbx_A"}
    assert SUBJECT not in lam.calls[0]["Payload"].decode()


def test_a_single_owner_relay_is_unchanged(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAKE_SUBJECT_KEY_ARN")
    lam = _Lambda()
    waker._relay(lam, _Context(), 1, 7, "sbx_A")
    assert json.loads(lam.calls[0]["Payload"]) == {"relay": 1, "generationBefore": 7}
