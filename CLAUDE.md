# Agent guide

Onboarding for any coding agent working in this repository. `AGENTS.md` is a
symlink to this file. Read `README.md` for the product and architecture, and
`docs/operations.md` for the runbook and the full incident history. This file
is the short path to being productive without repeating past mistakes.

This repository is published to a public `aws-samples` repository. **Never
commit real account IDs, Region-specific ARNs, CloudFront domains, live URLs,
credentials, or test accounts.** Deployment-specific values live outside the
tree (see "Deployment specifics" below), and a secret scan runs in
`make verify`.

## What this is

KiroCrew is a local-first app: a browser SPA talks to a gateway on `127.0.0.1`
over HTTP, SSE, and WebSockets. This project runs the **unmodified** pinned
KiroCrew SPA and gateway inside an Amazon Bedrock AgentCore microVM and bridges
the two models. A browser shell intercepts the SPA's `fetch`/`WebSocket`/
`EventSource` and tunnels every call over AgentCore `InvokeAgentRuntime` to a
Python adapter beside the gateway. One Cognito identity maps to one durable
sandbox; the workspace and the user's Kiro sign-in survive restarts through
encrypted S3 checkpoints.

## Repository map

`README.md` has the full table. The parts you touch most:

- `frontend-shell/` — browser shell: Cognito PKCE, lifecycle panel, the
  `fetch`/WS/SSE interception (`bootstrap.ts`), and the remote transport
  (`remote-transport.ts`, `agentcore-channel.ts`).
- `adapter/` — protocol validation, loopback route policy, HTTP/SSE/WS
  tunneling (`transport.py`, `loopback.py`), Kiro identity (`identity.py`).
- `runtime/` — AgentCore entrypoint, session init, gateway supervision
  (`supervisor.py`), invocation handling and checkpoint scheduling
  (`aws_runtime.py`).
- `infrastructure/` — Terraform; `functions/control/` (lifecycle, leases,
  state, binding tokens) and `functions/persistence/` (chunked encrypted
  checkpoint/restore engine and broker).
- `contracts/` — JSON Schema, OpenAPI/AsyncAPI, route allowlist, upstream
  compatibility pins. The contract suite enforces these across Python and TS.
- `tests/` — unit (100% Python line+branch required), contract, deployed-stack
  e2e; `frontend-shell/` has vitest and Playwright suites.

## Build, verify, deploy

Run these through `make` / the venv / node binaries directly. **The interactive
shell wraps `uv`, `npm`, and `node` in zsh functions that fail under a
non-interactive tool shell** — call `.venv/bin/python -m pytest`,
`node node_modules/vitest/vitest.mjs run`, or `make` targets instead of the bare
commands.

1. `make verify` — the full gate: lock check, format, lint, types, unit
   (100% Python line+branch coverage), contracts, terraform validate. This must
   pass before any deploy.
2. `make image-publish IMAGE_RELEASE_TAG=<version>-microvm-rN EXPECTED_AWS_ACCOUNT_ID=<acct> AWS_REGION=<region> ECR_REPOSITORY_URI=<repo>`
   — multi-arch build+push, only when `runtime/`, `adapter/`, or image inputs
   changed. Takes ~15 minutes; the last output line is the immutable digest.
3. `npm run build:bootstrap` — **always before `terraform apply`.** Terraform
   prepends deployment config to the bundle when it uploads `bootstrap.js`;
   applying with a stale bundle serves old code with new config (historically a
   blank page).
4. `terraform -chdir=infrastructure apply` with `-var runtime_image_digest=<digest>`,
   then invalidate `/bootstrap.js` (or `/*` after an upstream SPA change) on the
   CloudFront distribution. A control/persistence Lambda-only change needs the
   apply but no image rebuild.
5. Live regression: sign in → Start → ready → confirm a real KiroCrew call
   (not just the panel) → Stop safely → restart → confirm restore.

Existing microVM sessions keep the old image until they end, so a live
regression needs a fresh session started after the apply.

## Invariants — read before changing these paths

These encode real incidents (full history in `docs/operations.md`). The
through-line: **the panel, the lease, and `/ping` can all say healthy while
KiroCrew is not actually serving.** Confirm with a real invocation.

- **Checkpoint quiescing** (`persistence/.../checkpoint.py`): a non-final
  checkpoint may freeze the gateway (SIGSTOP) only for the in-memory snapshot,
  then must resume before uploading. The upstream gateway hard-exits if its
  event loop is frozen past its loop-stall budget; the supervisor pins that
  budget to the maximum via `config.local.json`.
- **Gateway lifecycle** (`runtime/.../aws_runtime.py`,`supervisor.py`): the
  backend restarts a dead gateway, but **never before the session is
  initialized** (the initializer owns it during restore; starting early
  corrupts the restore).
- **Session rotation & warm containers**: every authoritative start rotates the
  `runtimeSessionId`. On a binding mismatch the initializer consults the
  authoritative DynamoDB record: superseded → checkpoint + SIGTERM self so the
  platform reschedules; still authoritative → refuse the stale caller without
  exiting; cross owner/sandbox → always refuse.
- **Zero data access in the microVM** (`runtime-common/main.tf`, `aws_runtime.py`,
  persistence `lambda_handler.py`): the execution role is shared by the user's
  shell, so it has no DynamoDB, S3, or data-key KMS grants. Every sandbox-record
  operation is a narrow, server-defined broker operation (`readRecord`, `lease`,
  `acquireInit`, `heartbeatInit`, `heartbeatLease`, `healReady`, `markReady`,
  `markError`) gated on the caller's token; the broker never accepts caller
  expressions. A guard test fails on any `dynamodb:` action in the runtime role.
  The broker mints a runtime-session token on `acquireInit` so heartbeats and
  checkpoints outlive the browser's 30-minute binding token.
- **Reclaim from READY** (persistence `lambda_handler.py::_acquire_init`): idle
  reclaim leaves the record `READY` with nothing serving, so a replacement
  container must be able to claim it — but only once the start lease is dead
  (90s), which is what separates a container that is gone from a live warm
  owner (that one republishes READY via `healReady`). Narrowing this back to
  `STARTING`/`RESTORING` self-locks the sandbox: every invocation is refused
  while the panel reads ready, and Stop is refused too.
- **Stop is best-effort**: the JWT-authed runtime rejects the control plane's
  SigV4 `StopRuntimeSession`, so stop finalizes STOPPED on the committed
  checkpoint and lets idle reclaim free the microVM.
- **No managed session storage**: workspace is ephemeral container disk; S3
  checkpoints are the only durability layer. A guard test prevents re-adding it.
- **Logs**: runtime logs ship to a role-writable CloudWatch group
  (`cloudwatch_logs.py`); the platform vended-log pipeline is unreliable. When
  diagnosing, read that group, and grep for `KiroCrew gateway exited with code`
  and `Rejecting invocation bound to`.
- **Contracts & upstream**: an upstream KiroCrew bump touches
  `runtime/kirocrew-artifact.json`, `pyproject.toml`, the Dockerfile args, the
  extracted SPA, the versioned route-allowlist, and both fixtures; the contract
  suite enforces consistency across Python, TS, and fixtures.

## Deployment specifics (not in the repo)

Real account ID, Region, CloudFront distribution and domain, ECR repository,
the DynamoDB table name, log group names, and any test account are
deployment-local and are deliberately not committed. Read them from your own
`terraform output`, the AWS console, or the deployment's own operational notes,
and supply them at deploy time. This repository ships only placeholders.

## Working conventions

- **Documentation is part of the deliverable, in the same change.** When a
  behavior changes, update every document that describes it before calling the
  work done: `docs/operations.md` (runbook, incident history), `docs/persistence.md`
  (normative contract), `README.md` **and its mirror `README.zh-CN.md`** (both
  READMEs must say the same thing), and the Chinese design docs under `docs/`
  where they cover the area. Grep for the old behavior's key words across
  `README*.md docs/*.md CLAUDE.md` before you finish — 2026-09-07 found the
  Chinese README four days and seven changes behind the English one, a stale
  route-allowlist version pin, and a log-location table pointing at the wrong
  group. Drift like that is a defect, not a nice-to-have.
- Commit only when asked; if on `main`, branch first unless told otherwise.
  Preserve the user as author and add the agent trailer the project uses.
- Terraform state for this deployment is a local file passed with `-state`; it
  is gitignored. Do not commit state.
