# Operations and troubleshooting

This runbook records how the deployment actually behaves in production and
where to look when it does not. Keep it in sync with the code: every claim
here names the module that implements it, and contract or unit tests pin the
load-bearing ones.

## Deployment pipeline

Every change ships through the same sequence. Skipping a step produces the
failure noted next to it.

1. `make verify` — the full gate (lock check, format, lint, types, unit with
   100% line+branch Python coverage, contracts, terraform validate).
2. `make image-publish IMAGE_RELEASE_TAG=<version>-microvm-rN ...` — only
   when `runtime/`, `adapter/`, or image inputs changed. The last output
   line is the immutable digest for `runtime_image_digest`.
3. `npm run build:bootstrap` — **always before `terraform apply`**. Terraform
   prepends deployment configuration to the built bundle when it uploads
   `bootstrap.js`; applying with a stale bundle serves old code with new
   config (historically: a blank page).
4. `terraform apply` with the pinned digest, then invalidate `/bootstrap.js`
   (or `/*` after an upstream SPA change) on the CloudFront distribution.
5. Live regression: register → sign in → Start → ready → Stop safely →
   restart. `tests/e2e/` covers this when `DEPLOYMENT_MODE` is set.

## Where the logs are

| What | Where |
|---|---|
| Runtime application log (adapter, supervisor, checkpoint engine) | `/aws/bedrock-agentcore/<prefix>` — the role-writable group Terraform creates; the runtime ships its own records there (`cloudwatch_logs.py`, `KIROCREW_LOG_GROUP`), one stream per container. The platform's vended `APPLICATION_LOGS` delivery has proven unreliable and is not relied on. |
| Platform access log (every InvokeAgentRuntime with payload) | `/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/<runtime-id>`, `BedrockAgentCoreRuntime_ApplicationLogs` stream |
| Control plane (start/stop/leases/history) | `/aws/lambda/<prefix>-control` |
| Persistence broker (checkpoint commits, restores) | `/aws/lambda/<prefix>-persistence` |
| Gated auth (register/login/reset) | `/aws/lambda/<prefix>-auth` |

The `/aws/bedrock-agentcore/runtimes/...-DEFAULT` groups only receive logs
for sessions on the DEFAULT endpoint; live traffic uses the named endpoint.
When diagnosing, start with the runtime application log group above.

## Sandbox lifecycle invariants

Implemented in `infrastructure/functions/control/` and pinned by
`tests/unit/test_sandbox_registry.py` and `test_control_lambda_handler.py`.

- **State machine**: `STOPPED → STARTING → RESTORING → READY ⇄ BUSY/
  CHECKPOINTING → STOPPING → STOPPED`, with `ERROR` reachable from live
  states and restartable (`ERROR → STARTING|STOPPED`).
- **Session rotation**: every authoritative start mints a fresh AgentCore
  `runtimeSessionId`, and the lease owner follows it. A session that died
  without a clean stop is never reused — reusing a poisoned id answered 424
  on every restart until the platform reclaimed the corpse, which parked
  sandboxes in `ERROR`. Resumes inside a live lease keep the session.
- **History events**: the control API records one DynamoDB event per state
  version when a poll observes the record (conditional put, 7-day TTL,
  timestamped with the record's own `updatedAt`). `GET
  /control/v1/sandbox/history` serves them with the persisted paths; the
  panel's *MicroVM uptime* is derived from the newest `STARTING` event,
  which is accurate precisely because of session rotation.
- **Keepalive**: `/ping` answers `HealthyBusy` during initialization and
  while background work (task runner, subagents, workflows) is running —
  probed over loopback with a 10s cache and a 4h stuck-busy fuse
  (`KIROCREW_BUSY_*`). The busy→idle transition immediately commits a
  durability checkpoint.
- **Durability**: checkpoints commit on Stop safely (final), after Kiro
  sign-in/out, every `KIROCREW_CHECKPOINT_INTERVAL_SECONDS` (300) when the
  workspace fingerprint changed, at the busy→idle transition, and on
  SIGTERM. Data loss from an unclean stop is bounded by the interval. A
  non-final checkpoint freezes the gateway (SIGSTOP) only while the
  snapshot is read into memory; uploads and the commit run with the
  gateway live (`checkpoint.py`).
- **Gateway liveness**: the upstream gateway is a child process that can
  die on its own (its loop-stall watchdog `_exit`s after a long event-loop
  freeze; the supervisor pins that budget to 300s via
  `config.local.json`). `ProductionRuntimeBackend.ensure_gateway`
  restarts a dead gateway before the next tunnelled request and from the
  `/ping` path, with a 30s backoff between failed attempts; while it is
  down the transport answers `503 KIROCREW_UNAVAILABLE` (retryable)
  instead of an opaque 500. The supervisor logs `KiroCrew gateway exited
  with code N. Output tail: ...` once per death — that line is the
  forensic record, look for it first.
- **Workspace disk**: `/mnt/workspace` lives on the microVM's container
  disk and is ephemeral — encrypted S3 checkpoints are the only durability
  layer. AgentCore managed session storage is deliberately **not**
  configured: its 1GB quota (filled to two thirds by the embedding model
  before r44) and 14-day retention caused repeated incidents, and the
  checkpoint engine already covers stop/resume. The runtime logs the
  actual disk capacity at startup (`Workspace disk at ...`); check that
  line in the application log group when diagnosing space issues. The
  embedding model ships inside the image (`KIROCREW_EMBED_MODEL_PATH`)
  and is excluded from checkpoints; restore skips such excluded entries
  when replaying manifests from before the exclusion.

## Known failure modes and what to do

- **Sandbox stuck in `ERROR` or a stale `READY`**: fixed causes were session
  reuse (now rotated) and swallowed early stops (now queued until the
  channel opens). If a record still wedges, reset it:
  `state=STOPPED`, clear `leaseOwner/leaseExpiresAt/activeRequestId`, keep
  `lastCheckpointGeneration` — the next start restores the checkpoint.
- **Panel says `Sandbox ready` but KiroCrew shows `Gateway offline` and
  every request fails (historically a few minutes after any workspace
  change)**: the gateway died during a checkpoint freeze. Before r50 the
  first periodic checkpoint SIGSTOPped it for the whole upload (~30s),
  its 25s loop-stall watchdog killed it on resume, nothing restarted it,
  and `/ping` stayed `Healthy`, so the record stayed READY while the
  browser bridge — which announced WebSocket `open` before any tunnel
  existed — reset the SPA's backoff on every attempt and produced 8–15
  invocations/s of `500 INTERNAL_ERROR`. Fixed in r50 on all four sides
  (snapshot-only freeze, pinned budget, auto-restart, honest `open`). If
  it recurs, the runtime log carries `KiroCrew gateway exited with code`
  with the gateway's last output lines and `Upstream KiroCrew gateway
  restart failed` with the reason.
- **`424 Received error (503) from runtime`** on invocations: the runtime
  answered before its session initialized, or a stale container failed
  authorization after rotation. Check the vended application log for the
  restore report and `SessionInitializationError`.
- **Stop safely leaves the panel on "Saving and stopping" forever**: the
  control plane's `StopRuntimeSession` call is SigV4-signed, but the runtime
  is configured with a `CustomJWTAuthorizer`, so the data plane rejects it
  with `AccessDeniedException: Authorization method mismatch`. The control
  Lambda now treats a permanent stop rejection as best-effort: the final
  checkpoint is already committed and idle reclaim frees the microVM, so it
  finalizes `STOPPED` on the committed checkpoint instead of stranding the
  record at `STOPPING`. The lingering session is torn down by
  `runtime_idle_session_timeout_seconds`. A prompt teardown would need the
  browser's JWT to drive `StopRuntimeSession` directly — tracked, not yet
  built. Look for `Runtime stop rejected (status 403); finalizing STOPPED`
  in the control log.
- **Stop safely appears to do nothing**: the prepare-stop travels over the
  runtime channel; if the channel is still handshaking the intent is queued
  and flushed on open (`browser-app.ts`). A stop that never lands is not a
  data-loss event — the periodic checkpoint and the SIGTERM final
  checkpoint still run, and idle reclaim stops the session within
  `runtime_idle_session_timeout_seconds`.
- **White page after deploy**: stale `bootstrap.js` — rebuild the bundle and
  re-apply (pipeline step 3), then invalidate.
- **Upstream upgrade** (new KiroCrew wheel): update
  `runtime/kirocrew-artifact.json`, `runtime/pyproject.toml`,
  `runtime/Dockerfile` args, relock, re-extract the SPA, create the
  versioned route-allowlist and upstream-contract files, and review every
  added `/api/*` literal — the contract suite enforces the digest and the
  deny-list consistency across Python, TypeScript, and both fixtures.

## Incident history and load-bearing invariants

Most outages in this system shared one shape: a **health signal reported ready
while the real serving path was broken**. The lease, the DynamoDB state, and
`/ping` all answered healthy while invocations failed. When you add a health
indicator, make it observe the path that actually serves traffic. The specific
incidents and the invariants that now prevent them:

- **Checkpoint froze the gateway to death.** A periodic checkpoint SIGSTOPs the
  upstream gateway; the original code held it frozen through the whole S3
  upload (tens of seconds). The gateway's own loop-stall watchdog hard-exits
  after its budget, so it killed itself on resume, nothing restarted it, and
  `/ping` still said healthy. *Invariant:* a non-final checkpoint freezes the
  gateway only for the in-memory snapshot, then resumes before uploading
  (`checkpoint.py`); the supervisor pins the gateway's loop-stall budget to its
  maximum through `config.local.json`; the backend restarts a dead gateway
  (`ProductionRuntimeBackend.ensure_gateway`) and never before the session is
  initialized.
- **Stop safely stranded the record at STOPPING.** The runtime uses a
  `CustomJWTAuthorizer`, so its data plane rejects the control plane's
  SigV4-signed `StopRuntimeSession` (`AccessDeniedException: Authorization
  method mismatch`). *Invariant:* the teardown is best-effort — the committed
  final checkpoint plus idle reclaim make it safe to finalize STOPPED anyway.
- **Session rotation vs warm-container reuse.** Every authoritative start mints
  a fresh `runtimeSessionId`; AgentCore can route it to a warm container still
  bound to an older session. *Invariant:* the initializer consults the
  authoritative DynamoDB record on a binding mismatch — if it was superseded it
  checkpoints and SIGTERMs itself so the platform reschedules; if it is still
  authoritative the caller is a stale page and is refused without exiting;
  cross owner/sandbox is always refused.
- **Managed session storage caused repeated incidents** (1GB quota filled by the
  embedding model, 14-day retention, restore校验 rejecting excluded entries). It
  is deliberately not configured; the workspace is ephemeral container disk and
  encrypted S3 checkpoints are the only durability layer. A guard test prevents
  re-adding it.
- **Binding token expiry killed long sessions.** A 30-minute binding token with
  no renewal turned a long-open page into a 503 storm. *Invariant:* a runtime
  lease heartbeat keeps the session alive, `start` inside a live lease renews the
  token without rotating the session, and the browser renews five minutes early.
- **The execution role was a cross-tenant primitive.** The runtime role once held
  table-wide DynamoDB read/write so the container could manage its own record,
  but every process in the microVM shares that role, the user's terminal
  included, so any sandbox user could read or corrupt other tenants' records.
  *Invariant:* the role has no data access at all. Record operations go through
  the persistence broker as narrow server-defined operations authorized by the
  caller's token; a guard test rejects any `dynamodb:` grant on the runtime role.
  The per-invocation lease check caches approvals for 15 seconds per binding, so
  a rotated-away session is refused within that window rather than instantly.
  Because the broker now stands between the runtime and its record, it also
  mints a runtime-session token (capped at `runtime_max_lifetime_seconds`) when a
  container wins `acquireInit`; lease heartbeats and background checkpoints run
  on that token, which also closes the older gap where a sandbox with no browser
  attached for over 30 minutes could no longer commit checkpoints.
- **Opaque 5xx with no logs.** The transport mapped every failure to an opaque
  envelope and logged nothing. *Invariant:* initialization rejections and
  unhandled invocation errors now log their cause; runtime logs ship straight to
  a role-writable CloudWatch group because the platform vended-log pipeline has
  proven unreliable.

The through-line for a new agent: **do not trust the panel, the lease, or
`/ping` as evidence that KiroCrew is actually serving.** Confirm with a real
invocation against the named endpoint, and read the runtime application log
group, not the platform vended group.

## Documentation map

| Document | Covers |
|---|---|
| `README.md` | Architecture, deploy, operate, develop |
| `docs/persistence.md` | Checkpoint/restore contract (English, normative) |
| `docs/persistence-design.zh-CN.md` | 持久化设计（中文详解） |
| `docs/architecture-design.zh-CN.md` | 整体架构设计（中文详解） |
| `docs/operations.md` | This runbook |
| `CLAUDE.md` (`AGENTS.md`) | Agent onboarding: build/verify/deploy commands and the invariants above |

When code changes a behavior described here, update the section in the same
pull request — the document is part of the deliverable, not an afterthought.
