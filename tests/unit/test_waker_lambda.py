"""Tests for the scheduled waker.

The waker's whole job is two hops that must happen in one order, so the tests are
about the SHAPE of what it sends rather than about arithmetic: a wake that sends a
well-formed request to the wrong door, or a well-formed request the backend does
not implement, fails in production while looking correct in review. Both of those
actually happened while this was being built.
"""

from __future__ import annotations

import base64
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

WAKER_SRC = Path(__file__).resolve().parents[2] / "infrastructure" / "functions" / "waker" / "src"
if str(WAKER_SRC) not in sys.path:
    sys.path.insert(0, str(WAKER_SRC))

ULID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")

STATUS_BODY = json.dumps({"cron": {"running": True, "jobs": 1, "enabled": 1}})


class _StubPayload:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw


class _StubLambda:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        operation = json.loads(kwargs["Payload"])["operation"]
        if operation == "schedulerStop":
            body: dict[str, Any] = {"sandboxId": "sbx_TEST", "state": "STOPPED"}
            return {
                "Payload": _StubPayload(
                    json.dumps({"statusCode": 200, "body": json.dumps(body)}).encode()
                )
            }
        body = {
            "authoritative": True,
            "expiresAt": "2026-09-17T00:00:00Z",
            "qualifier": "live",
            "runtimeArn": "arn:aws:bedrock-agentcore:ap-southeast-1:1:runtime/r",
            "runtimeSessionId": "00000000-0000-7000-8000-00000000000b",
            "sandboxId": "sbx_TEST",
            "schedulerToken": "s" * 64,
            "state": "READY",
        }
        return {
            "Payload": _StubPayload(
                json.dumps({"statusCode": self._status, "body": json.dumps(body)}).encode()
            )
        }


class _StubRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if json.loads(kwargs["payload"])["operation"] == "sandbox.prepare_stop":
            events = {
                "events": [
                    {
                        "operation": "checkpoint.committed",
                        "payload": {
                            "generation": 115,
                            "manifestDigest": "d" * 64,
                            "checkpointReceipt": "receipt-" + "r" * 32,
                        },
                    },
                    {"operation": "request.completed", "payload": {"checkpointCommitted": True}},
                ]
            }
            return {"statusCode": 200, "response": _StubPayload(json.dumps(events).encode())}
        events = {
            "events": [
                {"operation": "request.accepted", "payload": {"status": 200}},
                {"operation": "output.delta", "payload": {"body": STATUS_BODY}},
            ]
        }
        return {"statusCode": 200, "response": _StubPayload(json.dumps(events).encode())}


class _StubDynamo:
    """Returns the queued generations in order, one per get_item call."""

    def __init__(self, generations: list[int]) -> None:
        self._generations = list(generations)
        self._last: int | None = None
        self.calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if not self._generations:
            # The record does not vanish once the queue runs dry: the poll reads it
            # repeatedly, and returning nothing would model a table that forgot.
            return (
                {}
                if self._last is None
                else {"Item": {"lastCheckpointGeneration": {"N": str(self._last)}}}
            )
        self._last = self._generations.pop(0)
        return {"Item": {"lastCheckpointGeneration": {"N": str(self._last)}}}


@pytest.fixture
def waker(monkeypatch: pytest.MonkeyPatch) -> Any:
    for name, value in {
        "COGNITO_SUBJECT": "00000000-0000-7000-8000-000000000000",
        "SANDBOX_ID": "sbx_TEST",
        "CONTROL_FUNCTION_NAME": "kirocrew-control",
        "MACHINE_RUNTIME_ARN": "arn:aws:bedrock-agentcore:ap-southeast-1:1:runtime/machine",
        "MACHINE_ENDPOINT_QUALIFIER": "machine_live",
        "SANDBOX_TABLE_NAME": "kirocrew-sandboxes",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("WAKE_PATH", raising=False)
    monkeypatch.delenv("WAKE_DWELL_SECONDS", raising=False)
    module = importlib.import_module("kirocrew_agentcore_waker.lambda_handler")
    reloaded = importlib.reload(module)
    # The dwell and the generation poll both sleep for real, and the poll is bounded
    # by a monotonic deadline. Left alone, the suite spent fourteen minutes asleep,
    # and the poll would spin for its full timeout in wall-clock time. So sleeping
    # ADVANCES a fake clock instead: the code's timing logic runs exactly as written
    # while the tests stay instant, and the dwell test asserts on what was requested.
    clock = {"now": 1000.0}
    monkeypatch.setattr(reloaded.time, "monotonic", lambda: clock["now"])

    def _sleep(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(reloaded.time, "sleep", _sleep)
    return reloaded


def _wire(
    module: Any,
    monkeypatch: pytest.MonkeyPatch,
    lambda_client: Any,
    runtime: Any,
    dynamo: Any | None = None,
) -> None:
    clients = {
        "lambda": lambda_client,
        "bedrock-agentcore": runtime,
        "dynamodb": dynamo if dynamo is not None else _StubDynamo([1, 2]),
    }

    class _Session:
        def client(self, name: str, **kwargs: Any) -> Any:
            # Accepts kwargs because the runtime client is constructed with an
            # explicit botocore Config -- the default read timeout is shorter than a
            # real checkpoint, and its retries corrupt one.
            del kwargs
            return clients[name]

    monkeypatch.setattr(module.boto3, "Session", lambda *a, **k: _Session())


def test_wake_mints_a_binding_then_uses_it_on_the_machine_endpoint(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The order is the design: the control plane holds the only signing key."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)

    result = waker.handler({}, None)

    assert result["status"] == 200
    mint = json.loads(lambda_client.calls[0]["Payload"])
    assert mint["operation"] == "schedulerStart"
    # Both must be named. The subject alone would let a stale config CREATE a
    # fresh empty sandbox and write a scheduled job's output into it, which looks
    # healthy from every angle except the owner's.
    assert mint["cognitoSubject"] and mint["sandboxId"] == "sbx_TEST"

    invocation = json.loads(runtime.calls[0]["payload"])
    assert invocation["bindingToken"] == "s" * 64
    # The session id comes from the mint, not from a fresh one: a different id
    # would be a different session and would not hold the lease just acquired.
    assert runtime.calls[0]["runtimeSessionId"] == "00000000-0000-7000-8000-00000000000b"
    assert runtime.calls[0]["qualifier"] == "machine_live"
    assert runtime.calls[0]["agentRuntimeArn"].endswith("/machine")


def test_wake_carries_a_real_http_request_not_a_ping(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ping` is in the protocol schema's enum but no backend implements it.

    Sending it produced a 400 "The loopback operation is unsupported." AFTER the
    sandbox had already been claimed and restored -- the most misleading possible
    failure, because every hard part had succeeded. Pinned here so the operation
    cannot drift back to something the schema accepts and the backend refuses.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)

    waker.handler({}, None)

    invocation = json.loads(runtime.calls[0]["payload"])
    assert invocation["operation"] == "kirocrew.http"
    assert invocation["payload"]["method"] == "GET"
    # Must live under /api/, or the loopback route policy denies it with a 403.
    assert invocation["payload"]["path"].startswith("/api/")
    assert invocation["payload"]["transport"] == "http"


def test_invocation_matches_the_published_contract(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contract validation runs BEFORE authorization, so a bad shape masks auth."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)

    waker.handler({}, None)

    invocation = json.loads(runtime.calls[0]["payload"])
    assert set(invocation) == {
        "version",
        "requestId",
        "bindingToken",
        "operation",
        "payload",
    }
    assert invocation["version"] == "kirocrew-agentcore.v1"
    assert ULID_PATTERN.match(invocation["requestId"])
    assert len(invocation["bindingToken"]) >= 32


def test_every_wake_uses_a_fresh_idempotency_key(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying a key returns the FIRST answer, whose binding may have expired.

    That would present as a wake that reported success and did nothing, which is
    the failure mode hardest to notice from outside.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)

    waker.handler({}, None)
    waker.handler({}, None)

    keys = [
        json.loads(call["Payload"])["idempotencyKey"]
        for call in lambda_client.calls
        if json.loads(call["Payload"])["operation"] == "schedulerStart"
    ]
    assert len(set(keys)) == 2
    assert all(ULID_PATTERN.match(key) for key in keys)


def test_a_refused_mint_fails_loudly_and_never_reaches_the_runtime(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 409 means the configured subject and sandbox disagree with reality.

    Continuing would wake SOMETHING while the operator believes their job runs in
    a workspace it never touches, so the wake has to die here.
    """
    lambda_client, runtime = _StubLambda(status=409), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)

    with pytest.raises(RuntimeError, match="Scheduler start refused"):
        waker.handler({}, None)
    assert runtime.calls == []


@pytest.mark.parametrize(
    "missing",
    ["COGNITO_SUBJECT", "SANDBOX_ID", "CONTROL_FUNCTION_NAME", "MACHINE_RUNTIME_ARN"],
)
def test_missing_configuration_is_refused_before_any_call(
    waker: Any, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """Empty config must not be treated as a default worth trying."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        waker.handler({}, None)
    assert lambda_client.calls == [] or runtime.calls == []


def test_cron_state_is_reported_and_a_broken_answer_never_loses_the_wake(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cron block is the evidence KiroCrew itself came up, not just the box.

    A container that answered health checks for nearly eight hours while never
    being claimed ran no KiroCrew at all, so "the container started" is not the
    thing worth logging.

    Decoding it is still only for the log line: a wake that delivered its request
    has done its job, and losing that to a formatting error would be worse than
    losing the detail.
    """
    assert waker._cron_state(
        json.dumps({"events": [{"payload": {"body": STATUS_BODY}}]}).encode()
    ) == {"running": True, "jobs": 1, "enabled": 1}

    # The gateway's body arrives base64-encoded whenever it is not plain text.
    # Reading only the plain form reported `unknown` against a perfectly good
    # 200 response, which is exactly the confusion this log line exists to avoid.
    encoded = base64.b64encode(STATUS_BODY.encode()).decode()
    assert waker._cron_state(
        json.dumps({"events": [{"payload": {"chunkData": encoded}}]}).encode()
    ) == {"running": True, "jobs": 1, "enabled": 1}

    # A long string that decodes but carries no cron block must not be mistaken
    # for an answer -- keep looking rather than reporting the first thing parsed.
    assert waker._cron_state(
        json.dumps(
            {
                "events": [
                    {"payload": {"headers": json.dumps({"content-type": "x" * 40})}},
                    {"payload": {"body": STATUS_BODY}},
                ]
            }
        ).encode()
    ) == {"running": True, "jobs": 1, "enabled": 1}

    assert waker._cron_state(b"not json") == "unknown"
    assert waker._cron_state(json.dumps({"events": []}).encode()) == "unknown"

    lambda_client = _StubLambda()

    class _Garbled(_StubRuntime):
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"statusCode": 200, "response": _StubPayload(b"\x00\x01 not json")}

    garbled = _Garbled()
    _wire(waker, monkeypatch, lambda_client, garbled)
    assert waker.handler({}, None)["status"] == 200


def _runtime_error(status: int) -> ClientError:
    return ClientError(
        {
            "Error": {
                "Code": "RuntimeClientError",
                "Message": (
                    f"Received error ({status}) from runtime. "
                    "Please check your CloudWatch logs for more information."
                ),
            }
        },
        "InvokeAgentRuntime",
    )


def test_a_sandbox_already_held_is_not_treated_as_a_failed_wake(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 means something already holds the sandbox -- so it is already awake.

    Raising would make the schedule retry, and a FAILED wake is the expensive
    outcome rather than a missed one: a container keeps billing for as long as it
    holds a session, and a failed one was measured at roughly twenty-five times
    the cost of a clean wake. So this reports and stops.
    """
    lambda_client = _StubLambda()

    class _Contended(_StubRuntime):
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            raise _runtime_error(503)

    _wire(waker, monkeypatch, lambda_client, _Contended())

    result = waker.handler({}, None)

    assert result["outcome"] == "already-awake"
    assert "status" not in result


def test_other_runtime_failures_still_surface(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only lease contention is swallowed. A 400 or 403 is a real defect."""
    lambda_client = _StubLambda()

    class _Rejected(_StubRuntime):
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            raise _runtime_error(403)

    _wire(waker, monkeypatch, lambda_client, _Rejected())

    with pytest.raises(ClientError):
        waker.handler({}, None)


def test_status_is_recovered_from_the_only_message_that_carries_it(waker: Any) -> None:
    """The platform withholds the container's body; the number is all there is."""
    assert waker._runtime_status(_runtime_error(503)) == 503
    assert waker._runtime_status(_runtime_error(400)) == 400
    # An unrecognised message must NOT be read as contention, or a genuine defect
    # would be silently swallowed as "already awake".
    opaque = ClientError({"Error": {"Code": "X", "Message": "something else"}}, "Op")
    assert waker._runtime_status(opaque) is None


def test_a_wake_verifies_persistence_against_the_record_not_the_envelope(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runtime's answer cannot be trusted to say whether anything persisted.

    Eight unattended wakes reported "uncommitted" while committed generations
    advanced anyway: `sandbox.prepare_stop` returned an empty event list, and
    nothing in that answer separates "already committed" from "refused" from
    "worked". A wake that cannot tell a persisted cycle from a lost one is the
    exact failure this design exists to remove, so the question is put to the
    authoritative record instead.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    ddb = _StubDynamo([137, 138])
    _wire(waker, monkeypatch, lambda_client, runtime, ddb)
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["generationBefore"] == 137
    assert result["generationAfter"] == 138
    assert result["persisted"] is True
    operations = [json.loads(call["payload"])["operation"] for call in runtime.calls]
    # Order matters: drive the work, THEN persist it.
    assert operations == ["kirocrew.http", "sandbox.prepare_stop"]


def test_an_unchanged_generation_is_reported_as_not_persisted(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This is the case eight overnight wakes could not distinguish."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime, _StubDynamo([137, 137]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["persisted"] is False
    assert result["generationBefore"] == result["generationAfter"] == 137


def test_a_generation_read_that_fails_reports_not_persisted(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """None makes `persisted` false, which is honest: the check did not run.

    It must not raise -- the wake already delivered its request, and failing it
    over a read taken for a log line would make the schedule retry, which is the
    expensive outcome.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()

    class _Broken(_StubDynamo):
        def get_item(self, **kwargs: Any) -> dict[str, Any]:
            raise _runtime_error(500)

    _wire(waker, monkeypatch, lambda_client, runtime, _Broken([]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["persisted"] is False
    assert result["generationBefore"] is None


def test_a_wake_finishes_the_teardown_it_started(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping is two steps and the first one alone leaves the record stranded.

    The runtime commits and hands back a receipt, moving the record to STOPPING; the
    control plane verifies that receipt and finalizes STOPPED. Doing only the first
    half left the session unrotated, so the NEXT wake's prepare_stop answered
    "already prepared" with an empty event list and committed nothing -- every later
    cycle's durability then depended on a periodic checkpoint landing before idle
    reclaim. It is also what stops the bill, instead of paying until reclaim.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime, _StubDynamo([140, 141]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["persisted"] is True
    assert result["stopped"] == "stopped"
    operations = [json.loads(call["Payload"])["operation"] for call in lambda_client.calls]
    assert operations == ["schedulerStart", "schedulerStop"]
    stop = json.loads(lambda_client.calls[1]["Payload"])
    # The receipt comes from the checkpoint; the control plane refuses without it.
    assert stop["checkpointReceipt"] == "receipt-" + "r" * 32
    assert stop["sandboxId"] == "sbx_TEST"


def test_no_receipt_means_no_stop_attempt(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty event list yields no receipt, and the stop cannot be faked without one.

    Reported as `no-receipt` rather than retried: this is the "already prepared"
    shape, so a retry would find the same empty answer.
    """
    lambda_client = _StubLambda()

    class _AlreadyPrepared(_StubRuntime):
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            if json.loads(kwargs["payload"])["operation"] == "sandbox.prepare_stop":
                self.calls.append(kwargs)
                return {"statusCode": 200, "response": _StubPayload(b'{"events": []}')}
            return super().invoke_agent_runtime(**kwargs)

    _wire(waker, monkeypatch, lambda_client, _AlreadyPrepared(), _StubDynamo([140, 140]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["stopped"] == "no-receipt"
    assert result["persisted"] is False
    assert [json.loads(c["Payload"])["operation"] for c in lambda_client.calls] == [
        "schedulerStart"
    ]


def test_a_refused_stop_is_reported_not_raised(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Raising would retry a wake whose work already ran -- the expensive outcome."""
    runtime = _StubRuntime()

    class _RefusingStop(_StubLambda):
        def invoke(self, **kwargs: Any) -> dict[str, Any]:
            if json.loads(kwargs["Payload"])["operation"] == "schedulerStop":
                self.calls.append(kwargs)
                return {
                    "Payload": _StubPayload(json.dumps({"statusCode": 409, "body": "{}"}).encode())
                }
            return super().invoke(**kwargs)

    _wire(waker, monkeypatch, _RefusingStop(), runtime, _StubDynamo([140, 141]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["stopped"] == "refused-409"
    # Durability is independent of the teardown succeeding.
    assert result["persisted"] is True


def test_a_commit_that_lands_late_is_still_counted_as_persisted(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reply comes back before the record settles, so one read is a false negative.

    Observed live: `sandbox.prepare_stop` returned an empty event list, the waker
    read generation 142, and the record settled on 143 moments later -- a genuinely
    persisted cycle reported as lost. The side effect is real and the reply is not,
    so the record is polled.
    """
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    # 142 before, then two stale reads, then the commit becomes visible.
    _wire(waker, monkeypatch, lambda_client, runtime, _StubDynamo([142, 142, 142, 143]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["generationBefore"] == 142
    assert result["generationAfter"] == 143
    assert result["persisted"] is True


def test_a_wake_that_persisted_nothing_is_not_waited_on_forever(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The poll is bounded: a lost cycle must be REPORTED, not hung on."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    ddb = _StubDynamo([142] * 40)
    _wire(waker, monkeypatch, lambda_client, runtime, ddb)
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    assert result["persisted"] is False
    # Bounded by the deadline, not by the queue of stale reads.
    assert len(ddb.calls) < 40


def test_the_runtime_client_outwaits_a_real_checkpoint_and_never_retries_it(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SDK's own defaults were the bug, and they are invisible at the call site.

    A real workspace's final checkpoint took 92 seconds against botocore's 60-second
    default read timeout with retries ON. The SDK abandoned the real response, retried,
    and the second attempt hit a runtime already preparing -- which answers with an
    empty event list. Eight wakes reported "uncommitted" because of that, and it was
    read as an upstream defect rather than this client timing itself out.

    Retries are OFF rather than merely slower: a retried checkpoint is a second commit
    racing the first for one generation number, which is how a manifest digest came to
    disagree with its own object.
    """
    captured: dict[str, Any] = {}

    class _Session:
        def client(self, name: str, **kwargs: Any) -> Any:
            if name == "bedrock-agentcore":
                captured["config"] = kwargs.get("config")
                return _StubRuntime()
            if name == "lambda":
                return _StubLambda()
            return _StubDynamo([1, 2])

    monkeypatch.setattr(waker.boto3, "Session", lambda *a, **k: _Session())
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    waker.handler({}, None)

    config = captured["config"]
    assert config is not None, "the runtime client must not use botocore's defaults"
    assert config.read_timeout >= 200, "must outwait a ~92s checkpoint with headroom"
    # max_attempts of 1 means the single original attempt and no retry.
    assert config.retries["max_attempts"] == 1


def test_the_dwell_holds_the_sandbox_awake_before_the_checkpoint(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job due at 09:00 has not run yet when the gateway answers at 08:59:10."""
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "45")
    slept: list[float] = []
    monkeypatch.setattr(waker.time, "sleep", slept.append)

    waker.handler({}, None)

    assert slept == [45]
    # The sleep must land BETWEEN the two calls, not after both.
    assert len(runtime.calls) == 2


def test_a_failed_checkpoint_is_reported_rather_than_retried(
    waker: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wake already happened; retrying it would pay for a whole second cycle.

    And the record still decides whether anything persisted -- a refused checkpoint
    request does not by itself mean the cycle was lost, since the generation may
    have advanced by another path. Reporting that honestly is the whole point.
    """
    lambda_client = _StubLambda()

    class _NoCheckpoint(_StubRuntime):
        def invoke_agent_runtime(self, **kwargs: Any) -> dict[str, Any]:
            operation = json.loads(kwargs["payload"])["operation"]
            if operation == "sandbox.prepare_stop":
                self.calls.append(kwargs)
                raise _runtime_error(503)
            return super().invoke_agent_runtime(**kwargs)

    _wire(waker, monkeypatch, lambda_client, _NoCheckpoint(), _StubDynamo([140, 140]))
    monkeypatch.setenv("WAKE_DWELL_SECONDS", "0")

    result = waker.handler({}, None)

    # No exception: the schedule must not retry a wake whose work already ran.
    assert result["persisted"] is False
    assert result["generationAfter"] == 140


def test_wake_path_is_configurable(waker: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    lambda_client, runtime = _StubLambda(), _StubRuntime()
    _wire(waker, monkeypatch, lambda_client, runtime)
    monkeypatch.setenv("WAKE_PATH", "/api/taskrunner")

    waker.handler({}, None)

    assert json.loads(runtime.calls[0]["payload"])["payload"]["path"] == "/api/taskrunner"
