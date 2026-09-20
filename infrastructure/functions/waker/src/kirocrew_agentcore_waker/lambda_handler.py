"""Wake a sandbox on a schedule, so a cron inside it can fire while nobody watches.

Two hops, and the reason there are two is the whole design:

The browser runtime is configured with a ``customJWTAuthorizer``, and AWS states
that such a runtime cannot be reached with ``InvokeAgentRuntime`` from an AWS SDK
at all -- the caller must present an OAuth token, which a scheduler has no way to
obtain without impersonating a human. So a machine cannot knock on the door the
browser uses. The deployment therefore runs a SECOND runtime on the same image
with SigV4 inbound auth, which is also what the AWS security guidance recommends:
one runtime supports one authentication type, and a different type belongs on a
separate one.

That second door still needs to know WHICH sandbox to open, and the record keeps
only a one-way ``owner_hash`` -- nothing here can recover whose sandbox it is.
Hence hop one: the control plane is the only component holding the signing key, so
it mints a short-lived scheduler binding on being told the subject and the sandbox
id, and it refuses if the two do not correspond. Hop two presents that binding to
the machine endpoint.

The wake deliberately carries a real request rather than a no-op. Starting the
container is not the same as starting KiroCrew: the gateway that owns the cron
scheduler only comes up once the sandbox has been claimed and restored, and a
container that answered health checks for eight hours without ever being claimed
was observed running no KiroCrew at all. Reading ``/api/status`` is what proves
the gateway is up, and its ``cron`` block is the evidence the scheduler is live.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import time
from typing import Any

import boto3
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ULID_RANDOM_LENGTH = 16
_ULID_TIME_LENGTH = 10


def _ulid() -> str:
    """A ULID, because the invocation contract requires that exact shape.

    Written out rather than taken from a dependency: this function ships as a
    zip with no build step, and one small generator is cheaper than vendoring a
    package for a 26-character string.
    """
    remaining = int(time.time() * 1000)
    head = ""
    for _ in range(_ULID_TIME_LENGTH):
        head = _CROCKFORD[remaining % 32] + head
        remaining //= 32
    tail = "".join(secrets.choice(_CROCKFORD) for _ in range(_ULID_RANDOM_LENGTH))
    return head + tail


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} must be configured.")
    return value


def handler(event: Any, _context: Any) -> dict[str, object]:
    """Claim the sandbox, then drive one request through the restored gateway."""
    del event  # The schedule carries no input; every wake is identical.
    subject = _required("COGNITO_SUBJECT")
    sandbox_id = _required("SANDBOX_ID")
    control_function = _required("CONTROL_FUNCTION_NAME")
    runtime_arn = _required("MACHINE_RUNTIME_ARN")
    qualifier = _required("MACHINE_ENDPOINT_QUALIFIER")
    wake_path = os.environ.get("WAKE_PATH", "/api/status")
    dwell_seconds = int(os.environ.get("WAKE_DWELL_SECONDS", "120"))
    table_name = os.environ.get("SANDBOX_TABLE_NAME", "")

    session = boto3.Session()
    # Read BEFORE claiming anything, so the comparison afterwards answers "did
    # this wake persist something" rather than "is there any state at all".
    before = _record_generation(session, table_name, sandbox_id)

    lambda_client = session.client("lambda")
    # Invoked directly rather than through the API. That is not a shortcut: the
    # control plane decides a caller is the scheduler precisely BECAUSE there is
    # no `requestContext`, which only a direct invoke can produce. A request
    # arriving through API Gateway always carries one and is held to the browser
    # rules, so this path cannot be reached from the internet.
    response = lambda_client.invoke(
        FunctionName=control_function,
        Payload=json.dumps(
            {
                "operation": "schedulerStart",
                "cognitoSubject": subject,
                "sandboxId": sandbox_id,
                # A fresh key per wake. Replaying one would return the previous
                # answer, whose binding may already have expired -- which would
                # look like a wake that succeeded and did nothing.
                "idempotencyKey": _ulid(),
            }
        ).encode(),
    )
    outer = json.loads(response["Payload"].read())
    status = outer.get("statusCode")
    body = json.loads(outer["body"]) if isinstance(outer.get("body"), str) else outer
    if status != 200:
        LOGGER.error("Scheduler start refused: status=%s body=%s", status, body)
        raise RuntimeError(f"Scheduler start refused with status {status}.")

    binding = body["schedulerToken"]
    runtime_session_id = body["runtimeSessionId"]
    LOGGER.info(
        "Claimed sandbox %s as session %s (authoritative=%s state=%s)",
        sandbox_id,
        runtime_session_id,
        body.get("authoritative"),
        body.get("state"),
    )

    # A real workspace's final checkpoint was measured at 92 seconds, and botocore's
    # default read timeout is 60 with retries ON. So the SDK gave up on the real
    # response, retried, and the second attempt hit a runtime that was already
    # preparing -- which answers with an empty event list. That empty answer is what
    # made eight wakes report "uncommitted" while the commit landed anyway, and it
    # was read as an upstream defect when it was this client timing itself out.
    #
    # Retries are off rather than merely longer: a retried checkpoint is a SECOND
    # commit racing the first for the same generation number, which is exactly how a
    # manifest digest ended up not matching its own object.
    runtime = session.client(
        "bedrock-agentcore",
        config=Config(
            read_timeout=300,
            connect_timeout=10,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )
    invocation = {
        "version": "kirocrew-agentcore.v1",
        "requestId": _ulid(),
        "bindingToken": binding,
        # `kirocrew.http` and not `ping`: the protocol schema lists `ping` among
        # the valid operations, but the production backend implements only
        # kirocrew.ws.send, kirocrew.ws.close and kirocrew.http, and refuses
        # anything else with "The loopback operation is unsupported."
        "operation": "kirocrew.http",
        "payload": {
            "method": "GET",
            "path": wake_path,
            "transport": "http",
            "headers": {},
            "body": "",
        },
    }
    try:
        result = runtime.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(invocation).encode(),
        )
    except ClientError as error:
        # A 503 here is a live container refusing to hand over the sandbox, or a
        # sandbox still coming up. Neither is a failure of THIS wake's purpose:
        # something already holds the sandbox, which means it is awake, which is
        # the entire point. Raising would be actively harmful -- an unhandled
        # error makes the schedule retry, and a FAILED wake is the expensive
        # outcome, since a container keeps billing for as long as it holds a
        # session. Roughly twenty-five times the cost of a clean one was measured.
        #
        # The status has to be scraped from the message because the platform
        # returns the container's body to nobody: the SDK surface carries only
        # "Received error (N) from runtime. Please check your CloudWatch logs."
        status = _runtime_status(error)
        if status == 503:
            LOGGER.warning(
                "Sandbox %s is already held or still starting (503); "
                "leaving it alone rather than retrying.",
                sandbox_id,
            )
            return {
                "sandboxId": sandbox_id,
                "runtimeSessionId": runtime_session_id,
                "outcome": "already-awake",
            }
        raise
    raw = result["response"].read()
    LOGGER.info(
        "Woke sandbox %s: status=%s bytes=%d cron=%s",
        sandbox_id,
        result.get("statusCode"),
        len(raw),
        _cron_state(raw),
    )

    # Waking the sandbox is not the same as the work surviving. Nothing is durable
    # until a checkpoint commits, and an unattended wake cannot rely on the
    # PERIODIC one: that interval defaults to 300s while the machine endpoint's
    # idle timeout is 120s, so the container is always reclaimed first. Six
    # consecutive overnight wakes were observed restoring generation 114,
    # answering 200, and dying without ever advancing past 114 -- every job's
    # output discarded, while every log line said success.
    #
    # So the wake dwells long enough for a due job to actually fire, then commits
    # explicitly. `sandbox.prepare_stop` is handled by the runtime itself rather
    # than proxied to the gateway, so it does not depend on KiroCrew still being
    # healthy, and it hands back the generation it wrote.
    if dwell_seconds > 0:
        LOGGER.info(
            "Holding sandbox %s awake for %ss so due jobs can fire.", sandbox_id, dwell_seconds
        )
        time.sleep(dwell_seconds)

    generation, receipt = _commit_checkpoint(
        runtime, runtime_arn, qualifier, runtime_session_id, binding
    )
    # Stopping is TWO steps. The runtime commits and hands back a receipt, moving
    # the record to STOPPING; the control plane verifies that receipt and finalizes
    # STOPPED. Doing only the first half stranded the record at STOPPING with the
    # session never rotated, and the NEXT wake's prepare_stop then answered
    # "already prepared" with an empty event list and committed nothing -- so every
    # later cycle's durability quietly depended on a periodic checkpoint landing
    # before idle reclaim. Finishing the teardown is also what stops the bill: the
    # alternative is paying for the session until the platform reclaims it.
    stopped = _finish_stop(lambda_client, control_function, subject, sandbox_id, receipt)

    after = _await_generation(session, table_name, sandbox_id, before)
    persisted = before is not None and after is not None and after > before
    LOGGER.info(
        "Sandbox %s generation %s -> %s (persisted=%s, receipt=%s, stopped=%s)",
        sandbox_id,
        before,
        after,
        persisted,
        generation,
        stopped,
    )
    return {
        "sandboxId": sandbox_id,
        "runtimeSessionId": runtime_session_id,
        "status": result.get("statusCode"),
        "generationBefore": before,
        "generationAfter": after,
        "persisted": persisted,
        "stopped": stopped,
    }


def _finish_stop(
    lambda_client: Any,
    control_function: str,
    subject: str,
    sandbox_id: str,
    receipt: str | None,
) -> str:
    """Complete the teardown the checkpoint started, via the control plane.

    Reported rather than raised, for the same reason the checkpoint is: the wake's
    work already ran, and making the schedule retry costs a whole cycle plus the
    risk of two wakes overlapping -- which is how a corrupt generation was produced
    in the first place.
    """
    if not receipt:
        return "no-receipt"
    try:
        answer = lambda_client.invoke(
            FunctionName=control_function,
            Payload=json.dumps(
                {
                    "operation": "schedulerStop",
                    "cognitoSubject": subject,
                    "sandboxId": sandbox_id,
                    "checkpointReceipt": receipt,
                    "idempotencyKey": _ulid(),
                }
            ).encode(),
        )
        outer = json.loads(answer["Payload"].read())
    except (ClientError, ValueError, KeyError) as error:
        LOGGER.warning("Scheduler stop failed: %s", error)
        return "error"
    if outer.get("statusCode") != 200:
        LOGGER.warning("Scheduler stop refused: %s", str(outer.get("body"))[:300])
        return f"refused-{outer.get('statusCode')}"
    return "stopped"


def _await_generation(
    session: Any,
    table_name: str,
    sandbox_id: str,
    before: int | None,
    timeout_seconds: float = 30.0,
    interval_seconds: float = 2.0,
) -> int | None:
    """Wait briefly for the committed generation to move past `before`.

    Reading once, immediately after the checkpoint request returns, reports a false
    negative: `sandbox.prepare_stop` answers with an EMPTY event list and yet the
    commit lands -- one observed wake read 142 while the record settled on 143
    moments later, so a genuinely persisted cycle was reported as lost. The
    side effect is real and the reply is not, so the record is polled rather than
    the reply believed.

    Bounded, and returns whatever the last read saw. A wake that persisted nothing
    should be reported as such after a short wait, not waited on forever.
    """
    deadline = time.monotonic() + timeout_seconds
    latest = _record_generation(session, table_name, sandbox_id)
    while time.monotonic() < deadline:
        if before is None or (latest is not None and latest > before):
            return latest
        time.sleep(interval_seconds)
        latest = _record_generation(session, table_name, sandbox_id)
    return latest


def _record_generation(session: Any, table_name: str, sandbox_id: str) -> int | None:
    """Read the committed generation straight off the sandbox record.

    Returns None rather than raising: a wake that already delivered its request
    must not be reported as failed because a read for a log line did not work, and
    None makes `persisted` false, which is the honest answer when the check itself
    could not run.
    """
    if not table_name:
        return None
    try:
        item = (
            session.client("dynamodb")
            .get_item(
                TableName=table_name,
                Key={"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}},
                ProjectionExpression="lastCheckpointGeneration",
            )
            .get("Item")
        )
    except (ClientError, KeyError) as error:
        LOGGER.warning("Could not read the sandbox generation: %s", error)
        return None
    if not item:
        return None
    value = item.get("lastCheckpointGeneration", {}).get("N")
    return int(value) if value is not None else None


def _commit_checkpoint(
    runtime: Any,
    runtime_arn: str,
    qualifier: str,
    runtime_session_id: str,
    binding: str,
) -> tuple[object, str | None]:
    """Ask the runtime for a terminal checkpoint; return the generation and receipt.

    The receipt is the half that matters operationally: the control plane will not
    finalize a stop without it, so losing it strands the record at STOPPING.

    A failure here is reported, not raised. The wake itself already happened, and an
    exception would make the schedule retry a wake whose work is already done --
    paying for a whole second cycle, and risking the overlapping-wake race that
    produced a corrupt generation once already.
    """
    envelope = {
        "version": "kirocrew-agentcore.v1",
        "requestId": _ulid(),
        "bindingToken": binding,
        "operation": "sandbox.prepare_stop",
        "payload": {},
    }
    try:
        answer = runtime.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(envelope).encode(),
        )
        body = json.loads(answer["response"].read())
    except (ClientError, ValueError, KeyError) as error:
        LOGGER.error("Checkpoint request failed: %s", error)
        return "uncommitted", None
    for event in body.get("events", []):
        if event.get("operation") == "checkpoint.committed":
            payload = event.get("payload", {})
            return payload.get("generation", "unknown"), payload.get("checkpointReceipt")
    # An empty list is what "already prepared" looks like: the record is at STOPPING
    # from an earlier half-finished teardown, so there is nothing left to commit and
    # no receipt to finish with. Distinguished in the log rather than guessed at.
    LOGGER.warning("Checkpoint returned no committed event: %s", str(body)[:300])
    return "uncommitted", None


def _runtime_status(error: ClientError) -> int | None:
    """Recover the container's HTTP status from the only place it survives.

    AgentCore does not pass the container's response body to the caller. What
    reaches an SDK client is a fixed sentence -- "Received error (503) from
    runtime. Please check your CloudWatch logs for more information." -- so the
    number inside the parentheses is the entire signal available for deciding
    whether a wake should be retried or let go.
    """
    message = str(error.response.get("Error", {}).get("Message", ""))
    found = re.search(r"Received error \((\d{3})\)", message)
    return int(found.group(1)) if found else None


def _cron_state(raw: bytes) -> object:
    """Pull the gateway's own cron block out of the answer, for the log line.

    This is what distinguishes a wake that worked from a container that merely
    started: the scheduler only runs once KiroCrew itself is up. Any parsing
    failure is reported as unknown rather than raised -- a wake that delivered
    the request has already done its job, and losing it to a log-formatting
    error would be worse than losing the detail.

    The body arrives base64-encoded when the gateway's answer is not plain text,
    so both encodings are tried; reading only one reported `unknown` against a
    perfectly good response.
    """
    try:
        events = json.loads(raw).get("events", [])
    except (ValueError, AttributeError, TypeError):
        return "unknown"
    for event in events:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        for value in payload.values():
            if not isinstance(value, str) or len(value) < 32:
                continue
            for decode in (lambda text: base64.b64decode(text, validate=True), str.encode):
                try:
                    body = json.loads(decode(value))
                except (ValueError, TypeError, binascii.Error):
                    continue
                if isinstance(body, dict) and "cron" in body:
                    return body["cron"]
    return "unknown"
