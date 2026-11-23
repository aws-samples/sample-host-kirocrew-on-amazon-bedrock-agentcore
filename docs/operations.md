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

## Optional: AWS Agent Registry as an MCP server

Set `agent_registry_id` (and `agent_registry_region`, default `us-east-1`) to
expose one Agent Registry to every sandbox as the `agent-registry` MCP server.

- The Registry MCP endpoint authenticates with SigV4, which the Kiro CLI MCP
  client cannot produce. The supervisor therefore registers
  `kirocrew_agentcore_runtime.sigv4_mcp_proxy` as a stdio server; it signs each
  request with the runtime execution role, so no user credential enters the
  image or the sandbox.
- The entry is written to `$KIROCREW_HOME/mcp.json`, the file KiroCrew reads
  MCP servers from (not `config.local.json`). Other servers in that file are
  preserved. KiroCrew reads it when the gateway starts, so a running sandbox
  picks it up after Stop safely → Start.
- The execution role gets four read-only actions scoped to that one Registry
  ARN: `InvokeRegistryMcp`, `SearchDiscoverableRegistryRecords`,
  `ListDiscoverableRegistryRecords`, `BatchGetDiscoverableRegistryRecord`.
  `InvokeRegistryMcp` alone is enough for `tools/list` but not for a tool call.
- The Registry may live in another region than the runtime; it stores
  records, it does not proxy traffic.

## Optional: per-group tool authorization (tool Gateway)

Set `enable_tool_authorization = true` to deploy an AgentCore Gateway whose
tools are authorized per Cognito group by Cedar policies. The design and the
phases are in [design-tool-authorization.md](design-tool-authorization.md).

- **What it creates:** two Cognito groups (finance, engineering), a demo Lambda
  with one tool per group, the Gateway (Cognito JWT authorizer), a policy engine
  with one permit policy per group, a machine app client whose secret only the
  persistence broker can read, and a pre-token-generation trigger.
- **How a sandbox calls it:** every sandbox gets a `tool-gateway` MCP server in
  `$KIROCREW_HOME/mcp.json`. The runtime asks the broker (`toolToken`) for a
  15-minute access token for the sandbox's owner, writes it to a `0600` file
  outside the checkpointed roots, and refreshes it 5 minutes before expiry. A
  stdio proxy sends each MCP request with the current token. Scheduled jobs use
  the same path, so they run with the owner's groups when no browser is open.
- **Who the token belongs to:** the broker reads the owner from the sandbox
  record (the identity the control plane stored at the owner's last Start),
  never from the request. The trigger refuses unknown, disabled or unconfirmed
  users and copies the owner's current groups into the token. In audit logs the
  caller is the machine client; the user is the `kirocrew_owner` claim.
- **Mode:** `tool_authorization_policy_mode` is `LOG_ONLY` by default (decisions
  are recorded, nothing is denied). Set it to `ENFORCE` once the recorded
  decisions match the intended rules; then `tools/list` hides tools the caller
  may not use and a direct call is refused with `Tool Execution Denied`.
  Switching modes updates the Gateway in place; its URL does not change.
- **Where decisions show up:** CloudWatch metrics `AllowDecisions` and
  `DenyDecisions` per tool in the `AWS/Bedrock-AgentCore` namespace. Per-call
  traces need CloudWatch Transaction Search enabled.
- **Prerequisite:** the owner must have pressed Start at least once after
  scheduled wake was enabled, because that is when the identity is stored.

## Optional: team context (shared steering and knowledge)

Set `team_context_repo_url` to an `https://` Git repository (public for now)
laid out like [`examples/team-context`](../examples/team-context); set
`team_context_subdir` when the content is not at the repository root and
`team_context_ref` for a branch other than `main`. Every sandbox then:

- mirrors the repository read-only into `~/.kiro/team-context/repo` before the
  gateway starts and every 30 minutes;
- installs `steering/*.md` as global steering `~/.kiro/steering/team--<name>.md`
  (a user's own steering files are never touched);
- keeps the last good copy when the repository cannot be reached.

Add `~/.kiro/team-context/repo/<subdir>/knowledge` once as a KiroCrew knowledge
folder source to search the shared documents. Change the content with a pull
request on the repository; edits made inside a sandbox's mirror are discarded.
Each sync logs `Team context synced revision=... installed=N removed=N skipped=N`.
Design and next phases: [design-team-context.md](design-team-context.md).

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
- **Browser tool Chromium**: the image ships the pinned Chromium read-only
  in `/opt/kirocrew-browser`, with the shared libraries it links. The
  supervisor links `home/.kiro/crew/browser-harness/chromium` to it before
  the gateway starts, and checkpoints exclude that directory. "Redownload
  browser" in Settings fails against the read-only copy; it is not needed.

## Where a cold start spends its time

Measured on the Singapore dev deployment (V1, one test sandbox, 2026-11-20
to 2026-11-22). Read the two timing lines in the runtime application log:

- `Restore timing generation=N files=… chunks=… bytes=… manifest=…s
  fetch_and_write=…s (waiting_on_downloads=…s) install=…s`
- `KiroCrew start timing total=…s first_output=…s output_lines=…
  port_answered=…s token_mint_attempts=…`

| Phase | Typical | What sets it |
|---|---|---|
| Request to restore start | ~3 s | Restore reads only the newest two commit records (#116). Before, every record was fetched twice: ~190 s at 744 generations. |
| Restore download | ~10 MB/s | Bytes, not file or chunk count. 96 download workers instead of 32 saved 10%, so the limit is the microVM's bandwidth (inferred). 0.81 GB took 88 s. |
| Gateway start | 24–32 s | `port_answered` ~11–13 s, then one token mint. |

To make cold starts faster, keep bytes out of the checkpoint: anything the
image can ship or the start can rebuild. What remains in a typical home is
installed apps (`app-sources`, `apps`, ~400 MB): they are the code apps
execute, so re-cloning them at start would be slower than restoring them.
A test fixture of 20,000 small files (`projects/default/bigtest`) once made
up 22,000 of a sandbox's 35,000 files; check `files=` when a restore is
slow.

## Why this deployment stays on Runtime V1

Decision (2026-11-23): run V1, not V2. Measured on the Singapore dev
deployment with the same test sandbox, reopening after Stop safely:

| Platform | Result |
|---|---|
| V2, no page pin | Not open after 10 minutes |
| V2, page pin (branch `feat/v2-page-pin`, first build) | About 13 minutes. The pinner held only 1,020 of 5,131 files; the descriptor fix on the branch was never deployed |
| V1, same period | About 10 minutes |
| V1, after #116–#118 | About 2.5 minutes |

Why V2 does not help this design:

- V2 restores the microVM from a snapshot, so the platform part of a cold
  start gets faster. Here that part was never the bottleneck. Every start
  downloads and decrypts the whole workspace from S3 (about 0.8 GB, ~90 s at
  ~10 MB/s), and that costs the same on V1 and V2.
- After a V2 restore, file reads are slow until pages come back from the
  lazily fetched disk. KiroCrew's start reads about 5,000 files. Page pins
  cover file contents but not directory metadata, and holding the pins in
  the serving process failed the V2 snapshot outright ("The runtime process
  exited unexpectedly"); another team hit the same error.
- The Quick on AgentCore design that is fast on V2 mounts user files on
  demand (S3 Files) instead of restoring a workspace, so it has almost
  nothing to read at start. Reaching its numbers here would mean replacing
  the checkpoint restore with an on-demand mount, not tuning V2.

What V2 would bring: elastic memory billing, which may cost less; this was
not measured.

Revisit when any of these happens: AWS announces an end date for V1, the
workspace moves to an on-demand mount, or the V2 slow-read issue is fixed
on the platform side. Existing runtimes keep their version on update, so
staying on V1 needs no action today.

**New deployments:** since 2026-11-13 the service creates a runtime as V2
when no version is given, and `runtime_platform_version` defaults to `null`
(unset). Set `runtime_platform_version = "V1"` in your tfvars when creating
a new deployment, or it starts on V2.

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
- **Upstream upgrade** (new KiroCrew wheel): on a clean branch run
  `uv run python tools/upgrade_kirocrew.py <version>`. It rewrites every
  pin (artifact record, wheel URL, Dockerfile args, adapter policy version,
  Terraform defaults, docs, tests), moves both versioned contracts, relocks,
  records the lock digest, re-extracts the SPA and prints the `/api/*`
  literals that appeared or disappeared. Review each new route against the
  deny list in `loopback.py`, then re-run with `--accept-routes` to record
  the new literal digest; the contract suite refuses an unreviewed one.
  Read the release notes for config keys the supervisor pins or new
  host-native features: those changes stay manual.
  Deploy the new runtime image, `module.frontend` and `module.control_api`
  together: the control plane hands the browser
  `frontend_compatibility_version`, and a control plane still on the old
  version makes every page answer "The browser and runtime versions are
  incompatible" while the sandbox sits in `STARTING` with no runtime log.
- **Image in chat shows "Image failed to load"**: an `<img>` pointing at
  `/api/...` bypasses the patched `fetch`, and CloudFront answers with the
  SPA's `index.html`. `installSandboxImages` (`local-routes.ts`) loads such
  images through the channel as `blob:` URLs (#118). If one still fails,
  check whether its `src` is a same-origin `/api/` URL.
- **Text in browser screenshots shows as boxes**: the image ships
  `fonts-liberation` (Latin, Greek, Cyrillic) and `fonts-wqy-microhei`
  (Chinese). Other scripts (Arabic, Devanagari, Thai and others) still need
  extra fonts.

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
- **A reclaimed sandbox self-locked at READY.** AgentCore reclaims an idle
  microVM without telling the control plane, so the record was left `READY`
  while nothing was serving. `acquireInit` accepted only `STARTING` or
  `RESTORING`, so the replacement container the platform started for the next
  invocation could not claim the record: every `POST /invocations` was refused
  while the panel showed **Sandbox ready**, Kiro sign-in reported
  `Sign-in failed`, the terminal came up blank, and **Stop travelled the same
  blocked path**, so the user could not even stop the sandbox to clear it.
  Observed live on 2026-09-11 for a user whose container logged nothing but the
  platform's own `/ping` for its entire life while the record read `READY`, and
  whose history showed a reclaim/restart cycle every 10-15 minutes.
  *Invariant:* `READY` is claimable **once the start lease is dead** — a
  90-second-stale lease is what distinguishes a container that is gone from a
  live warm owner, which republishes `READY` through `healReady` instead of
  having its restore taken away. The broker refuses the claim while the lease
  is alive and names the clause that rejected it, so a retryable cause is not
  reported as a permanent one. Credit to the analysis in PR #11, whose branch
  proposed the same fix on the pre-broker code.
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
| `docs/security.md` (`security.zh-CN.md`) | Security model, credential inventory, known gaps, hardening profiles |
| `docs/design-scheduled-jobs.md` | Scheduled wake: waker, enrollment, unattended runtime |
| `docs/design-tool-authorization.md` | Per-group tool authorization: tool Gateway, Cedar, broker-minted owner tokens |
| `docs/design-vpc-network-mode.md` | VPC network mode (design) |
| `docs/design-team-context.md` | Team context: shared steering and knowledge mirrored from one Git repository |
| `docs/user-guide/` | End-user walkthrough with screenshots |
| `docs/operations.md` | This runbook |
| `CLAUDE.md` (`AGENTS.md`) | Agent onboarding: build/verify/deploy commands and the invariants above |

When code changes a behavior described here, update the section in the same
pull request — the document is part of the deliverable, not an afterthought.
