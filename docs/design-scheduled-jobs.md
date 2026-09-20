# Design: scheduled jobs that run with nobody watching

Status: draft for review. Nothing here is implemented yet.

## The problem, stated as a user would

"Write my daily report every morning at 9." Today that cannot happen unless a
human is looking at the browser tab when the clock strikes.

## Why every obvious workaround fails

Measured on a live `microvm` deployment in `ap-southeast-1`, not reasoned from
docs:

| Attempt | Outcome |
|---|---|
| Click `Stop safely` and walk away | The sandbox stops. Nothing runs. |
| Just walk away | Idle reclaim takes the compute. Nothing runs. |
| Leave it running overnight | `maxLifetime` caps a microVM at **8 hours** — the platform ceiling, not a setting. It cannot span a night. |
| Let the job keep its own sandbox alive | `/ping` reports `HealthyBusy` only for task-runner runs, subagents and workflow runs. `_probe_background_activity()` does not look at cron at all, so a scheduled job cannot hold up the sandbox it needs. |

Observed directly: a 13h43m window overnight with no probe output, and a
separate ~2h47m window the day before. Neither self-healed.

## The two facts that shape everything

### Waking is two layers, not one

This is the finding that invalidates the naive framing "have something wake the
sandbox".

| Layer | Who starts it |
|---|---|
| The microVM | The platform, on invocation |
| **The KiroCrew gateway — which is where the cron scheduler lives** | **Only a successful claim + restore** |

Evidence: container `ba277042` lived 21:59:58 → 05:54:04 UTC — nearly eight
hours — answering health pings the whole time. It received four
`POST /invocations`, all **403**. No `RESTORING`, no checkpoint, no cron
activity. The compute was up; the application never started, so the scheduler
did not exist.

A scheduler that only starts compute achieves nothing. It must drive an
invocation that **completes the claim and the restore**.

### A failed wake costs 25x a successful one

That eight-hour container was billed the whole time. Memory is billed per second
across the entire session including idle; only CPU is free when idle.

| | Billed wall clock | Memory @2 GB |
|---|---|---|
| Successful wake (3 min work + 15 min idle tail) | ~1090 s | ~$0.006 |
| **Failed wake (claim refused, sits to `maxLifetime`)** | **~28400 s** | **~$0.15** |

So the design needs failure detection and deliberate teardown, not just a
happy path. A wake that fails silently is the expensive case.

## What AWS already provides

Three earlier candidate designs — a machine account with a stored password, a
second IAM-authorized control-plane route, a new broker token type — were all
reinventions. The platform has first-class answers.

**`InvokeAgentRuntime` can be called on behalf of a user.** The API takes
`runtimeUserId` (wire header `X-Amzn-Bedrock-AgentCore-Runtime-User-Id`), and the
docs state it plainly: *"making a call to InvokeAgentRuntime on behalf of a user
ID"*. It is gated by two IAM actions —
`bedrock-agentcore:InvokeAgentRuntime` and
`bedrock-agentcore:InvokeAgentRuntimeForUser`.

**`runtimeSessionId` is a caller-supplied parameter**, so the scheduler can
address the same session the sandbox record already names rather than creating an
orphan.

**AgentCore Identity mints tokens for an absent user.**
`get_workload_access_token_for_user_id(workloadName, userId)` exists precisely for
a backend acting for a user who is not present.

**EventBridge Scheduler** supplies cron expressions with timezone handling and a
configurable flexible-time-window, which matters — see the precision requirement
below.

Net effect: **no long-lived credential has to be stored anywhere** — identity is
asserted by the platform. With one catch that the next section covers: these are
IAM-authorized flows, and the runtime this deployment already runs is configured
for JWT inbound auth instead, so reaching them requires a second front door
rather than just calling them.

## Architecture

```
EventBridge Scheduler          cron expression + IANA timezone
      | one target per owner, or one sweeper for all
      v
Waker Lambda                   IAM role only; holds no user secret
      | invoke_agent_runtime(runtimeUserId=…, runtimeSessionId=…, payload=…)
      v
AgentCore Runtime              provisions the microVM
      v
Adapter                        NEW: accepts a scheduler-originated invocation
      v
Claim + restore                the existing path, unchanged
      v
KiroCrew gateway starts        the cron scheduler arms itself and fires what is due
      v
Checkpoint                     the existing durability path, now working
      v
Deliberate teardown            do not leave it to idle reclaim
```

## The one piece that is genuinely ours to build

### What the deployed runtime actually allows

Read from the live runtime rather than assumed:

```
authorizerConfiguration   customJWTAuthorizer -> Cognito user pool,
                          allowedScopes ["aws.cognito.signin.user.admin"]
requestHeaderConfiguration requestHeaderAllowlist ["Authorization",
                          "X-Correlation-Id", "Last-Event-ID"]
lifecycleConfiguration     idleRuntimeSessionTimeout 900, maxLifetime 28800
```

This invalidates the obvious plan. **With `customJWTAuthorizer` set, IAM
invocation is not available**: the docs are explicit that *"if you're integrating
your agent with OAuth, you can't use the AWS SDK to call `InvokeAgentRuntime`"*.
So `runtimeUserId` and `InvokeAgentRuntimeForUser` — an IAM-authorized flow —
cannot be used against this runtime as configured. Any design resting on them is
wrong for this deployment.

It also explains the adapter's three gates: the `Authorization` header is
allowlisted and carries a real Cognito user access token, which is where
`_cognito_subject` gets its subject from.

### The shape that follows

**A second runtime endpoint for machine callers, same image, IAM inbound auth.**

A runtime's authorizer is per-runtime, so a scheduler-facing runtime can use IAM
while the browser-facing one keeps `customJWTAuthorizer`. Both run the same
image and address the same sandbox, because a sandbox is keyed by
`owner_hash = sha256("kirocrew-owner-v1\0" + cognito_subject)` — not by runtime.

Why this beats the alternatives:

- **No stored credential.** The waker's IAM role is the identity. Minting a
  Cognito *user* token without the user means holding that user's password
  (`ADMIN_USER_PASSWORD_AUTH`) or a refresh token — a long-lived secret, in a
  second place, for an account that can do everything the human can.
- **`InvokeAgentRuntimeForUser` is a separate IAM action**, so it can be granted
  to the waker alone. The browser path cannot forge a user id it has no
  permission to assert.
- **Lifecycle is per-runtime too.** The scheduler-facing runtime can set
  `idleRuntimeSessionTimeout` near its floor of 60 s instead of the 900 s
  default, which is the single biggest cost lever — the idle tail is ~70% of a
  wake's bill. The browser-facing runtime keeps 900 s, where a short timeout
  would mean constant cold starts for a human.

Splitting the front door is what makes those two profiles possible at all; one
runtime cannot be both patient for humans and frugal for robots.

### What the adapter still needs

Reading `KmsBindingVerifier.verify` closes this down further than "a second
inbound identity". A binding token is KMS-signed and already carries everything
the request needs:

```python
claims = {"type": "binding", "aud": …, "subjectHash": sha256(issuer\0subject),
          "sandboxId": …, "runtimeSessionId": …, "exp": …}
```

The `Authorization` header is used for exactly one thing: recomputing
`subjectHash` to compare against the token. **The token is the authority; the
header is a cross-check.** So the scheduler path does not need a new
authentication mechanism, only a new *token type* that the same verifier accepts
without a header to cross-check against — because a KMS signature over
`type: "scheduler"` claims already proves a trusted issuer minted it for that
subject.

That keeps the crypto, the lease check, and every downstream path untouched.

### The owner hash is one-way, and that shapes who can be woken

Both halves of a wake need the PLAIN Cognito subject:

```python
Identity.subject_hash = sha256(f"{issuer}\x00{subject}")   # what the token carries
registry.get_or_create(cognito_subject)                    # how the record is found
```

The record itself keeps only `owner_hash`, derived one-way from that subject. So a
waker that starts from *which sandbox* cannot recover *whose* it is — and that is a
deliberate privacy property, not an oversight.

Consequences, in the order they should be taken:

- **First working version: the waker is configured with the subject.** Honest for
  a single-owner deployment, which is what step 3 targets anyway. The control
  plane should require the waker to name BOTH the subject and the sandbox id and
  verify they correspond, so a wrong subject cannot wake somebody else's sandbox
  rather than silently creating a new one.
- **Multi-user needs the subject recoverable without storing it in the clear.**
  The workable shape is a KMS-encrypted copy of the subject on the record,
  decryptable only by the control plane's role: the waker names a sandbox, the
  control plane decrypts, and no plaintext subject is ever at rest. That keeps the
  property the hash exists to provide while making unattended wake possible for
  more than one person.

Not solved here, and not needed for the first end-to-end wake.

Three pieces, in dependency order:

1. **The CONTROL PLANE mints the scheduler token, not the broker.** Tracing both
   showed the broker is the wrong home: its `runtime-session` token requires the
   record to already name the session, which is untrue for a fresh wake. The
   control plane's `_start` already does exactly what a waker needs — pick a
   session id, acquire the start lease, and `issue_binding(identity, record)` —
   so the scheduler path is a variant of `_start` rather than new machinery.

   The waker reaches it by **invoking the control Lambda directly**, not through
   API Gateway, which sidesteps the JWT authorizer entirely and makes
   `lambda:InvokeFunction` the trust boundary. No new API Gateway route, no
   second authorizer, no stored credential.

   The identity it issues against is derived from the sandbox RECORD the waker
   names, not from anything a caller asserts — which is what the security
   guidance requires ("derive user-id from the authenticated principal").
2. **`KmsBindingVerifier` accepts `type: "scheduler"`** with no subject
   cross-check, and the adapter skips `_cognito_subject` on that path. Everything
   downstream — lease authorization, sandbox claim, restore — is untouched,
   because the claims are shaped identically to a binding token.
3. **The adapter only honours it on the machine endpoint.** Both runtimes run the
   same image, so the image cannot tell them apart on its own — the machine
   runtime must pass a distinguishing environment variable, and the adapter must
   refuse a scheduler token when that variable is absent. Without this, a browser
   caller reaching the JWT runtime could present a scheduler token and skip the
   header cross-check entirely.

Point 3 is the one that must not be forgotten, and it is why this was not
implemented in the same sitting as the endpoint: a half-built auth path is worse
than none, since the missing half is the part that keeps the two doors apart.

Deliberately NOT a shared secret between waker and adapter, and NOT a custom
allowlisted header carrying an unauthenticated claim — a header is only as
trustworthy as the set of principals that can set it, so the IAM action must be
the boundary, not the header.

### Open question that gates implementation — settled

The machine endpoint exists and was probed. One IAM-signed invocation carrying
`runtimeUserId` and no binding token:

```
client error : HTTP 424 RuntimeClientError, "Received error (400) from runtime"
container    : POST /invocations 400 337, on a microVM created 8s after the call
```

**400, not 403.** The adapter received the request and rejected it as malformed —
no `bindingToken` in the payload — which is a rejection from *before* the auth
gates. So SigV4 inbound auth, the platform hop, microVM provisioning and the
adapter are all reachable on this path. Everything that remains is inside the
adapter.

And the header question turns out not to matter. The security guidance says the
user id on the IAM path is *"an opaque string without IdP verification"* — so
`X-Amzn-Bedrock-AgentCore-Runtime-User-Id` carries **no more trust than a field in
the payload**; both are caller-asserted. The trust comes entirely from the IAM
policy on the endpoint. So the waker can pass the owner in the payload it already
has to construct, the adapter can trust it because the endpoint admits exactly
one role, and discovering whether the platform injects that header — which would
need an image rebuild to log — stops being necessary at all.

The guidance's actual requirement is met either way: *derive user-id from the
authenticated principal, not from arbitrary client-supplied values*. The waker
resolves the owner from the sandbox record it reads, not from anything a client
sent it.

## Where the schedule lives

The scheduler outside needs one fact: **when to wake next**. The job
definitions themselves are a different question.

**Recommended: definitions stay authoritative inside the sandbox; the next-due
time is published outward.**

`crons.json` under `home/.kiro` is where the user's jobs already live, it is
already covered by the persistence allowlist, and the UI already edits it. Moving
authority outside would mean a second place to edit and a sync problem in both
directions.

Instead the sandbox writes one field — the next due timestamp — into the existing
DynamoDB record on checkpoint, and the waker reads it. One direction, one field,
no duplication of the job bodies.

The cost is staleness: a job created now is invisible outside until the next
checkpoint. In practice creating a job mutates the workspace, and a workspace
change is exactly what triggers a periodic checkpoint, so the window is bounded
by the checkpoint interval (300 s default) rather than by user behaviour.

**Rejected: an external table as the source of truth.** It reads simpler until
you ask where the user edits a job, and then it needs a write path from the UI
into the table plus reconciliation against the sandbox copy.

## Precision, and a trap in it

Two schedule kinds behave differently on a missed occurrence, and this decides
how tight the wake has to be:

- **`--every` / `--at`** — `last_run_ts` is untouched while a job is overdue, so
  the job stays due and fires on the first tick after the gateway returns. One
  catch-up run, not N.
- **A cron expression** — *"only due while its expression matches the current
  minute"*. Miss the minute and **the occurrence is lost silently.**

"Every morning at 9" is the second kind. So a wake must land inside the target
minute, and cold start plus restore has to fit in it. One measured restore:
`RESTORING` → `READY` in **6 seconds** — but on a nearly-empty workspace. A
restore of a real workspace has not been timed, and that number, not the
scheduler, is the precision budget.

Consequences for the design:

- EventBridge Scheduler's flexible time window must be **OFF** for
  cron-expression jobs; a smeared invocation window loses occurrences.
- The waker should fire **early by the restore budget**, not on the minute.
- Long term, the honest fix is upstream: make a cron-expression job stay due
  briefly after its minute, the way interval jobs already do. Then wake precision
  stops being load-bearing. Worth raising with the maintainers separately.

## Failure handling, which is where the money is

A wake that cannot claim the sandbox must not be left to rot for eight hours.

- The waker records an attempt before invoking and reconciles afterwards, so a
  wake that never produced a checkpoint is *visible* rather than silent.
- After the work completes, tear down deliberately rather than waiting out
  `idleRuntimeSessionTimeout`. At the 900 s default the idle tail is roughly 70%
  of a wake's bill.

### The refusal this design will actually hit

`main` already handles one self-lock: `_acquire_init` makes a `READY` record
claimable **once the start lease is dead** (`a67fcb6`, merged via #14). That
covers a sandbox the platform reclaimed with nothing left serving it.

It does not cover the case a scheduler runs into. `_lease_heartbeat`
(`aws_runtime.py`) renews the start lease every 30 s *until the process dies* —
it never asks whether a client is still attached. So a container that is alive
but abandoned holds a lease that never expires, the record never becomes
claimable, and every later invocation is refused. Observed on this deployment: a
lease held **109 hours** across **137 consecutive refusals**, cleared only by a
conditional write by hand, after which the abandoned container noticed within
16 seconds and stopped on its own.

**Nobody being attached is the definition of an unattended run**, so this is the
refusal a scheduled wake meets, not the one already fixed. Two candidate
directions, neither yet proposed upstream:

- Make the heartbeat conditional on a client being attached, so an abandoned
  container lets its own lease lapse and `main`'s existing invariant then applies.
  Smaller and more general, but changes liveness semantics for every session.
- Let a confirmed takeover cross a *live* lease. The control plane already has a
  `confirmed_inactive` parameter for exactly this, with **no caller anywhere in
  the repository** — the mechanism was designed and never wired.

The second is what this deployment ships today as an explicit `Take over` route
and button. That was written off earlier as redundant with `a67fcb6`; it is not
— it addresses the lease-still-alive case that `a67fcb6` deliberately excludes.

## First unattended wake, measured (2026-09-16)

Run against the live deployment with no browser, no user token, and nobody
watching. Two hops:

```
control plane (direct Lambda invoke)   HTTP 200
                                       session EXAMPLE-…, authoritative=True
machine endpoint (invoke_agent_runtime) 503, body length 383
  container: "Invocation rejected during initialization:
              A live container still holds …"
```

**The 503/383 shape is the lease-contention refusal, not an identity refusal.**
An identity refusal is `403` with body 327 — seen repeatedly today from the
browser tab retrying with an expired token. So the scheduler token was accepted:
signature verified, type recognised, subject cross-check correctly skipped, and
execution reached the lease check. Pieces one, two and three all work in
production.

What stopped it is the refusal this design already predicted: an abandoned but
ALIVE container holding a lease that never expires, because `_lease_heartbeat`
renews until the process dies and never asks whether a client is attached. Nobody
being attached is the definition of an unattended run, so a scheduled wake meets
it every time.

### Two envelope bugs worth recording

The first attempt answered `400` with body 337, which reads identically to a
rejected token — same status, same length as an invocation carrying no token at
all. The cause was the envelope: `http-invocation.schema.json` requires `version`
(a const), `requestId` (a ULID), and `payload` (required even when empty), and
**that validation runs BEFORE authorization**. So a malformed envelope and a
refused token are indistinguishable from outside, and the auth result is
unreadable until the envelope is right.

Worth fixing upstream: `validate_schema` failing should not share a status and
body length with an auth refusal, when one is a caller mistake and the other is a
security decision.

## The wake works end to end (2026-09-17)

The whole path ran with no browser, no user token, and nobody watching:

```
EventBridge Scheduler   rate(2 hours), flexible window OFF
Waker Lambda            direct invoke of the control plane   HTTP 200
                        claimed sbx_EXAMPLE  authoritative=True  state=STARTING
machine endpoint        kirocrew.http GET /api/status        HTTP 200, 3188 bytes
container               Restore outcome=restored generation=114
gateway                 cron={'running': True, 'jobs': 1, 'enabled': 1}
```

That last line is the point of the whole design. It is the gateway's own answer,
so it is the only available evidence that KiroCrew itself came up rather than just
the microVM — and the cron scheduler inside the restored workspace is running, with
the job that was registered before the sandbox went to sleep still enabled. The
in-sandbox probe survived the checkpoint and came back live.

### `ping` is in the contract and implemented by nothing

The first attempt at this reached a genuinely misleading failure: `400` with body
341, arriving AFTER the sandbox had been claimed and restored. Everything hard had
already succeeded.

`http-invocation.schema.json` lists `ping` in its `operation` enum, so envelope
validation accepts it. But the production backend implements only
`kirocrew.ws.send`, `kirocrew.ws.close` and `kirocrew.http`, and refuses anything
else with "The loopback operation is unsupported." The `ping` handler that exists
lives in a test double, not on any deployed path.

So the published contract promises an operation no backend serves. A caller who
writes against the contract gets a 400 saying their operation is unsupported,
while the contract says it is supported. **Worth reporting upstream** alongside the
status-collision item above.

Finding it required `tools/error_fingerprint.py`, because AgentCore returns the
container's response body to nobody — the logged body length is the only
fingerprint that escapes.

### A refused wake must not look like a failed one

The deployed schedule immediately produced a case the design had reasoned about
but not handled: a wake fired while the PREVIOUS wake's container was still alive,
and the machine endpoint answered 503. The Lambda raised, which is the wrong
outcome twice over.

It is wrong on intent: something already holds the sandbox, which means the sandbox
is awake, which is exactly what a scheduled wake wanted. And it is wrong on cost:
an unhandled error makes the schedule retry, and a FAILED wake is the expensive
outcome, not a missed one — a container keeps billing for as long as it holds a
session, measured at roughly 25x a clean wake. A retry storm costs far more than a
skipped occurrence.

The waker now reports `outcome=already-awake` and stops. Only 503 is treated this
way; a 400 or 403 is a real defect and still surfaces. The status has to be
scraped out of the SDK's sentence, and an unrecognised message deliberately yields
no status rather than assuming contention, so a genuine defect cannot be quietly
filed as success.

### Still open

**Voluntary lease release is unverified in production.** The code is in and unit
tested, but no run has produced its log line. The container that finally let go
had been started from the OLD image, and the one running the new code went silent
after its invocation — the platform freezes an idle microVM, and from outside a
frozen container is indistinguishable from one that deliberately let go. Proving
it needs a container kept warm while genuinely unattended, which is a state that
has to be constructed on purpose.

**Next-due is still not published outward.** The schedule's cadence is fixed in
Terraform, so a job rescheduled inside the sandbox is invisible to the waker.

### Eight unattended wakes, and a checkpoint story that is not yet settled
(2026-09-18)

The schedule ran every two hours overnight with the dwell-and-commit change
deployed. Every occurrence woke the sandbox: restored, gateway up, and the
gateway's own answer reporting `cron={'running': True, 'jobs': 1, 'enabled': 1}`.
Seven of them restored cleanly with a single attempt.

Committed generations advanced from 119 to 137, so work IS being persisted. But
**every wake logged `Committed generation uncommitted`**, which means the waker
cannot read the checkpoint result — and a wake that cannot tell a persisted cycle
from a lost one is the exact failure this design set out to remove. The schedule is
DISABLED until that is understood; the capability is real, the reporting is not.

What is established:

* A wake with `state=STARTING` on the record committed generation 137 and returned
  a 1796-byte response.
* A wake against a record already in `state=STOPPING`, reusing the same runtime
  session, returned `{"events": []}` — 14 bytes, no committed event, no new
  generation. Observed twice, both times uncontended.
* So `sandbox.prepare_stop` appears to be a silent no-op once its session has
  already committed a final checkpoint, and the waker reports that identically to a
  genuine failure.

What is NOT established: why all eight overnight wakes logged `uncommitted` while
generations still advanced by roughly two per wake. Two commits per cycle suggests
the periodic checkpoint is contributing, which would mean the durable state owes
more to the container outliving the interval than to the deliberate commit. Not
guessed at here; it needs the response shape captured per wake.

### Concurrent wakes corrupt a generation

One generation went bad overnight, and it was not random:

```
generation-134-invalid: IntegrityError:
  Committed manifest digest does not match object.
```

The 07:30 wake had to fall back from 134 to 133 and discard that cycle. Generation
134 was written during the ONE occurrence that ran three times — 05:30, 05:34 and
05:35 — while the earlier seven single-run occurrences were all clean.

The cause is a retry that overlaps its own first attempt. `maximum_retry_attempts`
was 1, reasoned as "a failed wake is expensive, so allow one retry" — but the wake
now DWELLS for two minutes before committing, which makes a retry land while the
first attempt still holds the sandbox. Both computed "next generation = N+1", one
wrote the objects and the other wrote the receipt, and the committed digest no
longer matched its own object.

Retries are now ZERO. A missed occurrence costs one cycle of work; an overlapping
retry costs a cycle AND leaves an invalid checkpoint behind, so skipping is
strictly cheaper. The upstream integrity check is what caught this and fell back
to the last good generation rather than restoring corruption — worth noting as a
design that behaved well under a caller's mistake.

`wake_schedule_enabled` was added so the schedule can be silenced without
destroying the waker, its role and its log group, which are where the evidence
lives when a wake misbehaves.

## Storage reclamation is a broker problem, not a runtime one

Not part of scheduled jobs, but discovered while building it and it changes what
"wire up the existing GC" means.

`RetentionManager.mark()` / `.sweep()` have no production caller, and the reason
is not that someone forgot. **They cannot work from the container at all**:

```python
# remote.py, container side
def internal_delete(self, sandbox_id: str, category: str, name: str) -> None:
    raise BrokerAuthorizationError("Runtime checkpoint deletion is not permitted.")
```

An unconditional refusal that never even reaches the broker — and the broker
exposes no delete operation to reach. So `sweep()`, which calls `remove_chunk` →
`internal_delete`, can only ever succeed against the in-memory store the unit
tests use. That is why its only callers are tests.

This is deliberate and correct: a container that could delete checkpoint objects
could destroy the user's only copy of their workspace. The boundary should stay.

Wiring the sweep into the runtime was tried and reverted — it committed, then
logged a traceback every 300 s in production while reclaiming nothing.

**The shape that would work** puts the sweep in the broker, which needs the
reference set without holding the sandbox key. `mark()` gets it by decrypting the
retained manifests, which the broker cannot do. But chunk digests are hashes of
**ciphertext** (`upload_chunk(digest, blob.ciphertext)`), so they disclose nothing
about the plaintext — which means the commit object the broker already writes
could carry the digest list in the clear, and then the broker can mark and sweep
with no key and no container involvement.

That is a schema addition to `commits/N.json` plus a broker operation, and it is
the third mechanism found here that was designed and left unreachable, after
`confirmed_inactive` and the `rate(6 hours)` audit stub.

Until then storage grows without bound at roughly 2.6 MB per generation.

## Staged plan

1. ~~Stand up a second runtime endpoint with IAM inbound auth.~~ **Done** —
   `kirocrew-example_machine_runtime`, no authorizer, 120 s idle timeout, opt-in
   behind `enable_machine_runtime`.
2. ~~Adapter accepts a scheduler identity from the machine endpoint~~ **Done** —
   control plane mints the binding, the adapter recognises `type: "scheduler"` and
   skips the subject cross-check, and it does so ONLY where `INBOUND_AUTH=iam`, so
   the same image refuses a scheduler binding on the browser door.
3. ~~Waker Lambda + one EventBridge schedule.~~ **Done** — one wake proven end to
   end: mint → claim → restore generation 114 → gateway 200 → cron confirmed
   running inside the restored workspace. Single owner, opt-in behind
   `enable_scheduled_wake`, flexible time window OFF, retries capped at one.
4. **Publish next-due outward** from the sandbox and have the waker read it,
   replacing the fixed cadence. Not started.
5. **Failure accounting and teardown**, measured against the $0.15 failed-wake
   number rather than assumed. Partly done: a contended wake no longer retries,
   which was the cheapest half. Nothing yet detects a wake that claimed a sandbox
   and then left it billing.

Steps 1 to 3 carried the risk and are closed. Steps 4 and 5 are refinement: the
capability exists and runs unattended without them.

Two verifications remain outstanding rather than unstarted — voluntary lease
release, and a real workspace's restore time, which is what `wake_lead_seconds`
should be fitted to. The only restore ever timed was six seconds against an EMPTY
workspace, so the current lead is a deliberately loose guess.
