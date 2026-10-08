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
from datetime import datetime
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
    # A scheduled wake carries no input. A RELAY carries two fields: which leg of
    # the chain it is, and the generation the first leg saw before claiming, so the
    # final "did this persist" answer is measured against the start of the work.
    relay = event.get("relay", 0) if isinstance(event, dict) else 0
    relay = relay if isinstance(relay, int) and relay >= 0 else 0
    subject = _required("COGNITO_SUBJECT")
    sandbox_id = _required("SANDBOX_ID")
    control_function = _required("CONTROL_FUNCTION_NAME")
    runtime_arn = _required("MACHINE_RUNTIME_ARN")
    qualifier = _required("MACHINE_ENDPOINT_QUALIFIER")
    wake_path = os.environ.get("WAKE_PATH", "/api/status")
    dwell_seconds = int(os.environ.get("WAKE_DWELL_SECONDS", "120"))
    poll_seconds = max(5, int(os.environ.get("WAKE_POLL_SECONDS", "30")))
    stop_reserve = int(os.environ.get("WAKE_STOP_RESERVE_SECONDS", "180"))
    max_relays = int(os.environ.get("WAKE_MAX_RELAYS", "8"))
    table_name = os.environ.get("SANDBOX_TABLE_NAME", "")

    session = boto3.Session()
    # The schedule fires every minute; most of those ticks must cost nothing.
    # Decide from the record whether THIS tick is the one to wake on.
    # A relay is already mid-work, and a manual invoke with {"force": true}
    # (a smoke test) asks for a wake regardless.
    gated = os.environ.get("WAKE_GATE", "") == "on"
    forced = isinstance(event, dict) and event.get("force") is True
    plan = (
        _wake_plan(session, table_name, sandbox_id, time.time())
        if gated
        else {"wake": True, "reason": "ungated", "due_at": None, "job": ""}
    )
    if gated and not relay and not forced and not plan["wake"]:
        LOGGER.info("No wake for %s: %s", sandbox_id, plan["reason"])
        return {"sandboxId": sandbox_id, "outcome": "not-due", "reason": plan["reason"]}
    LOGGER.info("Waking %s: %s", sandbox_id, plan["reason"] if not relay else "relay")
    # Read BEFORE claiming anything, so the comparison afterwards answers "did
    # this wake persist something" rather than "is there any state at all".
    inherited = event.get("generationBefore") if isinstance(event, dict) and relay else None
    before = (
        inherited
        if isinstance(inherited, int)
        else _record_generation(session, table_name, sandbox_id)
    )

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
    if dwell_seconds > 0 and relay == 0:
        LOGGER.info(
            "Holding sandbox %s awake for %ss so due jobs can fire.", sandbox_id, dwell_seconds
        )
        time.sleep(dwell_seconds)

    # The dwell only gives a due job time to START. A job that is still working
    # when it ends -- an app crew fixing an issue, an agent turn running tests --
    # must not have its sandbox checkpointed and stopped under it. So keep the
    # sandbox until it reports idle, and hand over to a fresh waker before this
    # one's own Lambda deadline instead of cutting the work off.
    def gateway_get(path: str) -> object:
        return _gateway_get(runtime, runtime_arn, qualifier, runtime_session_id, binding, path)

    if not relay:
        _catch_up(runtime, runtime_arn, qualifier, runtime_session_id, binding, plan)

    held = _hold_while_busy(gateway_get, _context, poll_seconds, stop_reserve)
    if held == "deadline":
        if relay < max_relays:
            _relay(lambda_client, _context, relay + 1, before)
            LOGGER.info(
                "Sandbox %s still busy at the deadline; handed over to relay %s.",
                sandbox_id,
                relay + 1,
            )
            return {
                "sandboxId": sandbox_id,
                "runtimeSessionId": runtime_session_id,
                "status": result.get("statusCode"),
                "outcome": "relayed",
                "relay": relay + 1,
            }
        # The relay budget is a cost bound, not a correctness one: the container
        # also stops reporting busy after its own fuse, and its busy-to-idle
        # transition checkpoints. Stopping here commits what exists now.
        LOGGER.warning(
            "Sandbox %s still busy after %s relays; checkpointing and stopping.",
            sandbox_id,
            relay,
        )

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


#: How long after its due time a missed occurrence is still worth running. Past
#: this, a late "9 o'clock report" is noise, and the next occurrence is the plan.
_CATCH_UP_GRACE_SECONDS = 1800
#: A record not touched for this long, with no published schedule (an older
#: runtime), still gets the old periodic wake so its interval jobs catch up.
_LEGACY_WAKE_SECONDS = 6 * 3600
#: A sandbox held by something else is left alone -- unless the hold is this old,
#: which is a stale record (a container that died mid-start), not a live user.
_STALE_HOLD_SECONDS = 1200


def _wake_lead(restore_seconds: int | None) -> int:
    """Wake this far before the due time: the sandbox's own cold start, padded.

    Measured per sandbox and published on every receipt, so a 950 MB workspace
    that takes two minutes to restore is woken earlier than an empty one, with
    nobody configuring it. Bounded both ways: too little and the job's minute
    passes during the restore, too much and the machine endpoint's idle timeout
    reclaims the sandbox before the minute arrives.
    """
    if restore_seconds is None:
        return 180
    return max(90, min(600, int(restore_seconds * 1.5) + 30))


def _iso_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _wake_plan(session: Any, table_name: str, sandbox_id: str, now: float) -> dict[str, Any]:
    """Decide from the sandbox record whether this tick should wake it, and why."""
    plan: dict[str, Any] = {"wake": False, "reason": "", "due_at": None, "job": ""}
    if not table_name:
        plan.update(wake=True, reason="no record to consult")
        return plan
    try:
        item = (
            session.client("dynamodb")
            .get_item(
                TableName=table_name,
                Key={"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}},
                ProjectionExpression="#s, updatedAt, nextDueAt, nextDueJob, restoreSeconds",
                ExpressionAttributeNames={"#s": "state"},
            )
            .get("Item")
        )
    except (ClientError, KeyError) as error:
        # Unable to look: waking costs one cycle, a missed job costs a promise.
        plan.update(wake=True, reason=f"record unreadable ({error})")
        return plan
    if not item:
        plan["reason"] = "no such sandbox"
        return plan
    state = item.get("state", {}).get("S", "")
    updated = _iso_epoch(item.get("updatedAt", {}).get("S"))
    held_for = now - updated if updated is not None else None
    if state != "STOPPED" and held_for is not None and held_for < _STALE_HOLD_SECONDS:
        # Somebody is using it, or a wake is already in progress: the scheduler
        # inside the sandbox is running, so there is nothing to wake.
        plan["reason"] = f"sandbox is {state}"
        return plan
    raw_due = item.get("nextDueAt", {}).get("N")
    if raw_due is None:
        if held_for is None or held_for >= _LEGACY_WAKE_SECONDS:
            plan.update(wake=True, reason="no published schedule; periodic wake")
        else:
            plan["reason"] = "no published schedule; periodic wake not yet due"
        return plan
    due_at = int(raw_due)
    raw_restore = item.get("restoreSeconds", {}).get("N")
    lead = _wake_lead(int(raw_restore) if raw_restore is not None else None)
    plan.update(due_at=due_at, job=item.get("nextDueJob", {}).get("S", ""))
    if due_at == 0:
        plan["reason"] = "nothing scheduled"
    elif now < due_at - lead:
        plan["reason"] = f"due in {int(due_at - now)}s, lead {lead}s"
    elif updated is not None and updated >= due_at:
        # Already woken (and stopped) for this occurrence; a fresh due time
        # arrives with that wake's final receipt.
        plan["reason"] = "already handled this occurrence"
    elif now - due_at > _CATCH_UP_GRACE_SECONDS:
        plan["reason"] = "occurrence too old to catch up"
    else:
        plan.update(wake=True, reason=f"due at {due_at}, lead {lead}s")
    return plan


def _catch_up(
    runtime: Any,
    runtime_arn: str,
    qualifier: str,
    runtime_session_id: str,
    binding: str,
    plan: dict[str, Any],
) -> None:
    """Run a cron-expression job whose minute passed while the sandbox restored.

    An interval job stays due and fires on its own; an expression job is only
    due DURING its minute, and a slow restore can land after it. So once the
    gateway is up, a job the record says was due -- and has not run since --
    is triggered once. Best effort: logged, never raised.
    """
    due_at, job = plan.get("due_at"), plan.get("job")
    if not job or not isinstance(due_at, int) or due_at > time.time():
        return
    crons = _gateway_get(runtime, runtime_arn, qualifier, runtime_session_id, binding, "/api/crons")
    jobs = crons.get("jobs") if isinstance(crons, dict) else None
    match = (
        next(
            (j for j in jobs if isinstance(j, dict) and j.get("id") == job),
            None,
        )
        if isinstance(jobs, list)
        else None
    )
    if match is None:
        LOGGER.info("Catch-up: job %s no longer exists.", job)
        return
    last_run = match.get("last_run_ts")
    if isinstance(last_run, int | float) and last_run >= due_at:
        return
    _gateway_request(
        runtime,
        runtime_arn,
        qualifier,
        runtime_session_id,
        binding,
        "POST",
        f"/api/crons/{job}/run",
    )
    LOGGER.info("Catch-up: triggered job %s, due at %s.", job, due_at)


def _gateway_request(
    runtime: Any,
    runtime_arn: str,
    qualifier: str,
    runtime_session_id: str,
    binding: str,
    method: str,
    path: str,
) -> object:
    envelope = {
        "version": "kirocrew-agentcore.v1",
        "requestId": _ulid(),
        "bindingToken": binding,
        "operation": "kirocrew.http",
        "payload": {"method": method, "path": path, "transport": "http", "headers": {}, "body": ""},
    }
    try:
        answer = runtime.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier=qualifier,
            runtimeSessionId=runtime_session_id,
            payload=json.dumps(envelope).encode(),
        )
        return _gateway_body(answer["response"].read())
    except (ClientError, ValueError, KeyError, AttributeError) as error:
        LOGGER.warning("Gateway %s %s failed: %s", method, path, error)
        return None


def _gateway_get(
    runtime: Any,
    runtime_arn: str,
    qualifier: str,
    runtime_session_id: str,
    binding: str,
    path: str,
) -> object:
    """GET one gateway path through the machine endpoint; None when unreadable.

    Never raises: this feeds a busy check, and a probe that cannot be read must
    count as idle -- otherwise a broken gateway would keep its sandbox (and its
    bill) alive for the whole relay budget.
    """
    return _gateway_request(
        runtime, runtime_arn, qualifier, runtime_session_id, binding, "GET", path
    )


def _gateway_body(raw: bytes) -> object:
    """Return the first JSON document carried in the answer's event payloads."""
    try:
        events = json.loads(raw).get("events", [])
    except (ValueError, AttributeError, TypeError):
        return None
    for event in events:
        payload = event.get("payload", {}) if isinstance(event, dict) else {}
        if not isinstance(payload, dict):
            continue
        for value in payload.values():
            if not isinstance(value, str) or not value:
                continue
            for decode in (lambda text: base64.b64decode(text, validate=True), str.encode):
                try:
                    body = json.loads(decode(value))
                except (ValueError, TypeError, binascii.Error):
                    continue
                if isinstance(body, dict | list):
                    return body
    return None


def _sandbox_busy(gateway_get: Any) -> bool:
    """The same four signals the container uses for its own /ping answer.

    Kept in step with ``AwsRuntimeBackend._probe_background_activity``: task
    runner, chat turns in flight (which is how an app crew such as Issue Radar
    does its work), subagents, workflows.
    """
    runner = gateway_get("/api/taskrunner")
    if isinstance(runner, dict) and isinstance(runner.get("runs"), list):
        if any(isinstance(run, dict) and run.get("running") for run in runner["runs"]):
            return True
    health = gateway_get("/api/sessions/health")
    counts = health.get("counts") if isinstance(health, dict) else None
    if isinstance(counts, dict) and isinstance(counts.get("running"), int):
        if counts["running"] > 0:
            return True
    status = gateway_get("/api/status")
    if isinstance(status, dict) and isinstance(status.get("subagents"), int):
        if status["subagents"] > 0:
            return True
    workflows = gateway_get("/api/workflows/runs")
    if isinstance(workflows, dict) and isinstance(workflows.get("runs"), list):
        if any(
            isinstance(run, dict) and run.get("status") == "running" for run in workflows["runs"]
        ):
            return True
    return False


def _remaining_seconds(context: Any) -> float:
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    if callable(remaining):
        try:
            return float(remaining()) / 1000.0
        except (TypeError, ValueError):
            pass
    # No Lambda context (a direct call): fall back to the configured timeout.
    return float(os.environ.get("WAKE_TIMEOUT_SECONDS", "900"))


def _hold_while_busy(gateway_get: Any, context: Any, poll_seconds: int, stop_reserve: int) -> str:
    """Wait until the sandbox is idle; return "idle", or "deadline" if time ran out.

    Idle must be seen TWICE in a row: a crew finishing one turn and starting the
    next shows a gap of a second or two, and stopping on that gap is exactly the
    cut-off this exists to prevent. Each probe is also a real request, so the
    machine endpoint's short idle timeout never reclaims the sandbox while we wait.
    """
    started = time.monotonic()
    budget = _remaining_seconds(context) - stop_reserve
    quiet = 0
    while True:
        if _sandbox_busy(gateway_get):
            quiet = 0
        else:
            quiet += 1
            if quiet >= 2:
                return "idle"
        if time.monotonic() - started + poll_seconds > budget:
            return "deadline"
        time.sleep(poll_seconds)


def _relay(lambda_client: Any, context: Any, leg: int, before: int | None) -> None:
    """Start the next leg asynchronously, then let this one return.

    The next leg re-mints its binding through ``schedulerStart``: the sandbox's
    start lease is still live (the container heartbeats it), so the control plane
    hands back the SAME session rather than rotating it, and the work is never
    interrupted -- only the watcher changes.
    """
    function = getattr(context, "invoked_function_arn", "") or os.environ.get(
        "AWS_LAMBDA_FUNCTION_NAME", ""
    )
    if not function:
        raise RuntimeError("The waker cannot name itself to start a relay.")
    lambda_client.invoke(
        FunctionName=function,
        InvocationType="Event",
        Payload=json.dumps({"relay": leg, "generationBefore": before}).encode(),
    )


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
