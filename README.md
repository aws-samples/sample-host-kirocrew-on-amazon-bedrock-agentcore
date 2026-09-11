# KiroCrew on Amazon Bedrock AgentCore

**English** | [简体中文](README.zh-CN.md)

Run the full KiroCrew experience in an isolated, durable cloud sandbox, without
modifying the upstream KiroCrew SPA or gateway.

Each user signs in through Amazon Cognito, receives a dedicated Amazon Bedrock
AgentCore microVM, and connects their own Kiro account (Builder ID or organization
SSO). Chat, sessions, and the interactive terminal stream to the browser, while the
workspace and the Kiro sign-in survive sandbox restarts through encrypted S3
checkpoints.

![KiroCrew running inside an AgentCore sandbox. The floating panel shows the sandbox restored and ready, and the user's own Kiro account signed in.](docs/dashboard.png)

*The unmodified KiroCrew dashboard, served from an AgentCore microVM. The floating
panel in the corner is the only UI this project adds: sandbox lifecycle, Kiro
sign-in, and a safe stop that checkpoints the workspace.*

> **Using a deployment someone else set up?** Start with the
> **[User Guide](docs/user-guide/README.md)** ([中文](docs/user-guide/README.zh-CN.md)):
> a screenshot walkthrough from registration through Kiro sign-in to the first chat
> session, with every control on the floating panel explained. The rest of this
> README is for people deploying and operating the sample.

> **Sample code, not for production use.** This repository is a reference
> implementation that demonstrates how to run KiroCrew on Amazon Bedrock AgentCore.
> It has not been hardened, load tested, or optimized for production workloads, and
> it carries no service-level commitment. Review and adapt it before any production
> deployment, in particular the authentication boundary, credential handling, IAM
> scoping, cost controls, and operational monitoring. You are responsible for the
> security and compliance of what you deploy.

> Deploy this sample into your own AWS account. Account IDs, Regions, and domains in
> this documentation are placeholders. Replace them with your own values.

## Contents

- [Why this project exists](#why-this-project-exists)
- [Architecture](#architecture)
- [Security and persistence model](#security-and-persistence-model)
- [Deploy the sample](#deploy-the-sample)
- [Operate the deployment](#operate-the-deployment)
- [Costs and cleanup](#costs-and-cleanup)
- [Local development](#local-development)
- [Repository layout](#repository-layout)

## Why this project exists

KiroCrew is designed to run locally: its browser SPA talks to a gateway on
`127.0.0.1` over HTTP, server-sent events (SSE), and WebSockets. AgentCore runs
applications remotely inside microVMs and exposes them through its data plane. This
project connects those two models while keeping the upstream release intact.

At a glance:

- **No upstream fork.** The pinned KiroCrew SPA and gateway are used as released.
- **One sandbox per user.** A Cognito identity maps to an isolated AgentCore session.
- **Native streaming behavior.** SSE chat output and raw terminal WebSocket frames are
  relayed incrementally rather than buffered.
- **Durable workspace.** Stop creates an encrypted checkpoint; start restores the
  latest committed generation.
- **User-owned Kiro identity.** Every user authenticates `kiro-cli` with their own
  account, and that sign-in is part of the checkpoint. The deployment holds no
  shared service identity.

## Architecture

![Architecture of KiroCrew on Amazon Bedrock AgentCore](docs/architecture.png)

[Open the SVG version](docs/architecture.svg)

A request makes the following journey:

1. **Sign-in and sandbox control.** The floating panel signs the user in with Cognito
   PKCE. The control Lambda creates or resumes the user's sandbox record, manages
   leases, and issues a short-lived binding token.
2. **Gateway interception.** The frontend shell intercepts the unmodified SPA's
   loopback HTTP, SSE, and WebSocket calls and encodes them as protocol envelopes.
3. **AgentCore transport.** Envelopes travel through the AgentCore runtime endpoint.
   The in-VM adapter validates each envelope against the JSON Schema contract and
   applies the route policy.
4. **Loopback replay.** The adapter replays the request against the real
   `kirocrew gateway` on `127.0.0.1` and streams the response, SSE events, or
   WebSocket frames back to the browser.
5. **Checkpoint and restore.** The persistence engine chunks and encrypts the
   workspace and commits it to S3 as a generation: on **Stop safely**, after a Kiro
   sign-in or sign-out, on a periodic interval when the workspace changed, when
   background work goes idle, and on SIGTERM. On start, it restores the latest
   generation before launching the gateway.

The diagram uses **blue** for protocol and chat streaming, **red** for sandbox
lifecycle, **green** for persistence, and **purple** for user authentication.

## Security and persistence model

- **Tenancy is enforced by the transport, not by route filtering.** AgentCore
  validates the Cognito JWT on every invocation, and the adapter verifies a binding
  token against the Cognito subject. The sandbox is a single-tenant microVM.
- **Route policy.** Every upstream route is tunneled by default. Only routes that
  mint the gateway's own credential (`/api/token`) or seize gateway lifecycle from
  the supervisor (`/api/shutdown`) are denied. Host-native features that cannot
  exist in a microVM answer `501`. The policy is pinned in
  `contracts/kirocrew/0.3.0-route-allowlist.json` and enforced by contract tests on
  both the Python adapter and the TypeScript shell.
- **Binding tokens** authorize the in-VM persistence broker for 30 minutes and are
  renewed transparently while the sandbox's lease is alive, so a long-open page
  keeps working without rotating the session.
- **Checkpoints** use per-sandbox KMS data keys and are committed as generations in
  S3. The latest two generations are retained; an offline auditor verifies them.
- **Restore** stages under `.agentcore/` inside the workspace on the microVM's
  container disk, prefetches chunks in parallel, and swaps entries into place with
  rollback support. The workspace disk is ephemeral: encrypted S3 checkpoints are
  the only durability layer.
- **Kiro sign-in** runs as a device flow in a PTY inside the sandbox. The Kiro CLI
  stores it under `~/.local/share/kiro-cli`, which is a checkpointed root, so a
  restored sandbox comes back signed in.
- **Gateway readiness** is detected from the gateway's readiness line and, as a
  fallback, from its health endpoint, so a restored sandbox becomes ready even when
  the gateway's own stdout is swallowed during model loading. A gateway that dies
  while the sandbox is running is restarted by the runtime; checkpoints freeze it
  only for the in-memory snapshot so they cannot trip its loop-stall watchdog.

See [docs/persistence.md](docs/persistence.md) for the full persistence contract.

## Deploy the sample

### Prerequisites

- An AWS account and a Region where Amazon Bedrock AgentCore Runtime is available.
- AWS CLI v2 configured with credentials that can create Cognito, CloudFront, S3,
  Lambda, DynamoDB, KMS, ECR, IAM, and AgentCore resources.
- Terraform 1.9 or later.
- Docker with buildx and a builder that can produce `linux/amd64` and `linux/arm64`
  images (a remote arm64 node or QEMU emulation both work).
- Python 3.12 with [`uv`](https://docs.astral.sh/uv/), Node.js 22 with npm 10.
- Each end user needs their own Kiro account (Builder ID or organization SSO).

### 1. Install dependencies and build the frontend

```bash
make setup                 # locked dependencies plus the pinned upstream KiroCrew SPA
npm run build:bootstrap    # build frontend-shell/dist/bootstrap.bundle.js
```

Terraform reads the bootstrap bundle and prepends the deployment configuration when
it uploads `bootstrap.js`, so build it before every `apply`.

### 2. Create the container registry

The runtime image lives in an ECR repository that Terraform creates. Apply that
module first so the repository exists before the image is pushed:

```bash
export AWS_REGION=us-east-2
export TF_STATE=$PWD/infrastructure/terraform.tfstate   # kept out of git
terraform -chdir=infrastructure init -backend=false
terraform -chdir=infrastructure apply -state="$TF_STATE" \
  -var="aws_region=$AWS_REGION" -target=module.runtime_common
```

### 3. Publish the runtime image

The image must stay below AgentCore's 2 GB limit; `make image-inspect` enforces
that limit and verifies the preinstalled development toolchain. The non-root sandbox
terminal includes:

- Python 3.12 with `pip`, `uv`, and `uvx`.
- Node.js 22.23.1 with npm 10.9.8, `npx`, and Corepack.
- `git`, Git LFS, GitHub CLI (`gh`), and the OpenSSH client.
- GCC/G++, `make`, and `pkg-config` for native Python and Node extensions.
- Common terminal utilities including `curl`, `jq`, `rg`, `fd`, `tree`, `file`,
  `rsync`, netcat, archive tools, and small text editors.

Large language SDKs and databases remain project-local to keep the base image
bounded.

```bash
make image-publish \
  IMAGE_RELEASE_TAG=0.3.0-microvm-r1 \
  EXPECTED_AWS_ACCOUNT_ID=<AWS_ACCOUNT_ID> \
  AWS_REGION=$AWS_REGION \
  ECR_REPOSITORY_URI=<AWS_ACCOUNT_ID>.dkr.ecr.$AWS_REGION.amazonaws.com/kirocrew-agentcore-dev-runtime
```

The last line of output is the immutable image reference. Keep its `sha256:` digest.

### 4. Deploy the stack

```bash
export TF_VAR_runtime_image_digest=sha256:<digest from step 3>
make infra-deploy EXPECTED_AWS_ACCOUNT_ID=<AWS_ACCOUNT_ID> AWS_REGION=$AWS_REGION
terraform -chdir=infrastructure output -state="$TF_STATE" deployment
```

The `deployment` output contains the CloudFront URL. Open it and create an
account directly in the floating panel: registration and sign-in go through a
gated Lambda that only accepts email addresses on the domains in the
`allowed_email_domains` variable (default `amazon.com`). Individual addresses
outside those domains can be admitted through `allowed_email_patterns`, a list
of regular expressions matched against the full address. New accounts confirm
their email address with a code sent to it, and the form can resend the code.
Nobody can register or
sign in against Cognito directly. After signing in, press **Start**. The first
start of a new sandbox takes about a minute; later starts restore the
checkpoint in seconds.

## Operate the deployment

- **Update the runtime.** Publish a new image (step 3), then run `make infra-deploy`
  with the new `TF_VAR_runtime_image_digest`. Terraform creates a new AgentCore
  runtime version and moves the live endpoint to it. Warm sessions on the previous
  version are recycled; a user reconnecting during the switch may briefly see
  **Sandbox needs attention** and can press **Start sandbox** (or reload the page
  with the **Reload** control) to reconnect.
- **Update the frontend.** Rebuild the bundle (`npm run build:bootstrap`) and run
  `make infra-deploy`. Terraform re-uploads `bootstrap.js`; then invalidate
  `/bootstrap.js` on the CloudFront distribution named in the `deployment` output.
- **Stop and start.** **Stop safely** checkpoints the workspace and releases compute.
  **Start** restores the latest committed generation. Sandboxes idle for
  `runtime_idle_session_timeout_seconds` (default 15 minutes) are scaled to zero by
  AgentCore. A sandbox whose task runner, subagents, or workflows are still
  working reports itself busy and stays alive past that timeout even with no
  browser connected; when the work finishes it commits a checkpoint and
  returns to normal idle reclaim. A task stuck busy is cut off after
  `KIROCREW_BUSY_MAX_SECONDS` (default 4 hours) so it cannot pin the microVM
  until the 8-hour session lifetime.
- **Observe.** The runtime ships its own logs (adapter, supervisor, checkpoint
  engine) to the `/aws/bedrock-agentcore/<prefix>` CloudWatch log group that
  Terraform creates; the platform's vended log group carries only its access log.
  Control-plane, persistence, and auth logs are in the three Lambda log groups. CloudWatch alarms can be
  routed with the `alarm_actions` variable. The full operations runbook, including
  log locations, lifecycle invariants, and known failure modes, is in
  [docs/operations.md](docs/operations.md).

## Costs and cleanup

The stack runs AgentCore Runtime sessions (billed per active session), two Lambda
functions, a DynamoDB table, S3 checkpoint storage, a KMS key per deployment, and a
CloudFront distribution. Idle sandboxes scale to zero, but checkpoints and the
distribution persist until you destroy the stack.

```bash
terraform -chdir=infrastructure destroy -state="$TF_STATE" -var="aws_region=$AWS_REGION"
```

`retain_persisted_data` (default `true`) keeps encrypted checkpoints, KMS keys, and
sandbox metadata on destroy. Set it to `false` to remove everything.

## Local development

```bash
make setup
make verify
```

`make verify` runs lock checks, formatting, linting, type checks, unit tests,
contract tests, and Terraform validation. Python unit tests enforce 100% line and
branch coverage. Focused targets:

```bash
make format-check          # Python, TypeScript, and Terraform formatting
make lint                  # ruff, Bandit, ESLint, and secret scan
make typecheck             # mypy and the TypeScript compiler
make unit                  # Python unit tests + Vitest
make contract              # generated assets + cross-language contracts
make terraform-validate    # Terraform initialization and validation
npm run test:playwright    # frontend shell browser tests
```

### Changing the protocol or route policy

Protocol schemas are the source of truth. After editing `contracts/schemas/*.json`
and the corresponding adapter schema copy, regenerate both language models:

```bash
uv run python tools/generate_protocol_models.py
```

A route-policy change must stay consistent across the Python adapter
(`adapter/src/kirocrew_agentcore_adapter/loopback.py`), the TypeScript shell
(`frontend-shell/src/remote-transport.ts`), and the two contract fixtures under
`contracts/kirocrew/` and `frontend-shell/upstream-contracts/`. The contract suite
checks that consistency and that every API literal in the pinned upstream bundle is
classified.

### Test strategy

- **Unit:** every Python module, with a 100% line and branch coverage gate; frontend
  behavior is covered by Vitest.
- **Contract:** TypeScript serialization is validated by Python models, generated
  assets are checked, upstream bundles are digest-pinned, and route coverage is
  enforced.
- **Browser:** Playwright covers panel states, dragging, device-code login, and the
  requirement that errors never lock users out of the page.
- **End to end:** `tests/e2e/` runs only against a deployed stack when
  `DEPLOYMENT_MODE` is set.

## Repository layout

| Path | Responsibility |
|---|---|
| `frontend-shell/` | Cognito PKCE, lifecycle UI, gateway interception, remote transport, and upstream SPA contract pins |
| `adapter/` | Protocol validation, loopback route policy, HTTP/SSE/WebSocket tunneling, and Kiro identity operations |
| `runtime/` | AgentCore entrypoint, session initialization, gateway supervision and restart, invocation handling, and checkpoint scheduling |
| `infrastructure/` | Terraform for CloudFront, S3, Cognito, Lambdas, DynamoDB, KMS, ECR, and AgentCore wiring |
| `infrastructure/functions/control/` | Sandbox lifecycle, leases, state transitions, and binding tokens |
| `infrastructure/functions/persistence/` | Chunked encrypted checkpoint/restore engine and Lambda broker |
| `contracts/` | JSON Schema, OpenAPI/AsyncAPI definitions, route policy, and upstream compatibility pins |
| `tests/` | Unit, cross-language contract, deployed-stack end-to-end, and browser UI tests |
| `tools/` | Protocol code generation, upstream SPA extraction, Terraform wrapper, and image tooling |
| `docs/` | Architecture source, screenshots, the persistence contract, and the end-user guide |
| `CLAUDE.md` (`AGENTS.md`) | Coding-agent onboarding: build/verify/deploy commands and load-bearing invariants |

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for how to report
security issues.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.
