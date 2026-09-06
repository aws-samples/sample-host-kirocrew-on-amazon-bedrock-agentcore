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
| Runtime stdout (adapter, supervisor, checkpoint engine) | `/aws/vendedlogs/bedrock-agentcore/runtime/APPLICATION_LOGS/<runtime-id>` — delivered through CloudWatch vended-log deliveries, **not** the role-writable group |
| Platform access log (every InvokeAgentRuntime with payload) | same group, `BedrockAgentCoreRuntime_ApplicationLogs` stream |
| Control plane (start/stop/leases/history) | `/aws/lambda/<prefix>-control` |
| Persistence broker (checkpoint commits, restores) | `/aws/lambda/<prefix>-persistence` |
| Gated auth (register/login/reset) | `/aws/lambda/<prefix>-auth` |

The `/aws/bedrock-agentcore/runtimes/...-DEFAULT` groups only receive logs
for sessions on the DEFAULT endpoint; live traffic uses the named endpoint
and lands in the vended group above.

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
  SIGTERM. Data loss from an unclean stop is bounded by the interval.
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
- **`424 Received error (503) from runtime`** on invocations: the runtime
  answered before its session initialized, or a stale container failed
  authorization after rotation. Check the vended application log for the
  restore report and `SessionInitializationError`.
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

## Documentation map

| Document | Covers |
|---|---|
| `README.md` | Architecture, deploy, operate, develop |
| `docs/persistence.md` | Checkpoint/restore contract (English, normative) |
| `docs/persistence-design.zh-CN.md` | 持久化设计（中文详解） |
| `docs/architecture-design.zh-CN.md` | 整体架构设计（中文详解） |
| `docs/operations.md` | This runbook |

When code changes a behavior described here, update the section in the same
pull request — the document is part of the deliverable, not an afterthought.
