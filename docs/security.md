# Security model

**English** | [简体中文](security.zh-CN.md)

This document states what the sample protects, what it deliberately does not, and
how it would be hardened for stricter deployments. It is written against the
[AWS agent security framework](https://aws.amazon.com/cn/blogs/china/agent-security-framework-design-model-amazon/)
(isolation, zero standing credentials, deterministic tool authorization, egress
control, auditability), so each gap is named rather than implied.

> This is sample code. Read this page together with the "not for production use"
> notice in the [README](../README.md).

## What this deployment is

Each user gets the **full KiroCrew developer workbench** (chat, terminal, code
execution, MCP servers) inside their own AgentCore microVM. That shapes the threat
model:

- The **user is trusted inside their own sandbox**. Anything the agent can do, the
  user's terminal can also do. The boundary this sample defends is *between*
  users, and between a sandbox and the deployment's shared data.
- This is the same trust level as KiroCrew on a laptop, with stronger isolation:
  a per-user microVM instead of the user's own machine, and no standing access to
  shared storage.
- It is **not** a restricted business agent. Such an agent would need default-deny
  egress and per-tool authorization; see [Hardening profiles](#hardening-profiles).

## Controls in place

| Control | How it is enforced | Where |
|---|---|---|
| One microVM per user | Each Cognito subject maps to its own AgentCore session | `infrastructure/functions/control/` |
| Caller identity on every invocation | AgentCore `customJWTAuthorizer` validates the Cognito JWT | `infrastructure/modules/runtime-microvm/` |
| User ↔ sandbox ↔ session binding | KMS-signed binding token (30 min, renewed while the lease lives); the adapter and the broker both verify it against the Cognito subject | `adapter/.../transport.py`, `persistence/lambda_handler.py` |
| No data access in the sandbox | The execution role can only verify bindings, invoke the broker, write its own logs and metrics, and (optionally) read one Agent Registry. DynamoDB, S3 and KMS data keys are reached only through the broker | `infrastructure/modules/runtime-common/main.tf` |
| Brokered, short-lived storage access | The broker checks the binding, confirms the record still names that session, then returns presigned URLs signed with SigV4 | `infrastructure/functions/persistence/` |
| Encrypted checkpoints | Per-sandbox KMS data key, encryption inside the VM, generation commit with rollback on restore | [persistence.md](persistence.md) |
| Gateway credential stays in the VM | `/api/token` and `/api/shutdown` are denied by the pinned route policy, enforced by contract tests on both sides | `contracts/kirocrew/0.3.0-route-allowlist.json` |
| Gated sign-up | Registration and sign-in go through an auth Lambda that enforces an email-domain allowlist | `infrastructure/functions/auth/` |
| Unattended wake without a stored secret | EventBridge Scheduler → waker Lambda with an IAM role only → a separate IAM-authorized runtime endpoint. The adapter honours scheduler invocations only on that endpoint | [design-scheduled-jobs.md](design-scheduled-jobs.md) |
| Registry access without user credentials | A stdio SigV4 MCP bridge signs Registry calls with the execution role, refuses non-`https` endpoints, and is scoped to one Registry ARN with four read-only actions | `runtime/.../sigv4_mcp_proxy.py`, [operations.md](operations.md) |
| Tenant negative tests | Mismatched subject, missing JWT, and forged scheduler claims are rejected | `tests/unit/test_adapter_transport.py` |

## Credential inventory

"The sandbox holds no data-plane AWS permission" is true. "The agent holds no
long-lived credential" is **not**: the user's Kiro sign-in lives in the sandbox.

| Credential | Held by | Readable by the agent/terminal | In checkpoints | Lifetime | Revocation |
|---|---|---|---|---|---|
| Cognito access / refresh token | Browser | No | No | Cognito settings | Cognito sign-out / revoke |
| Binding token | Browser, per request | In transit only | No | 30 min | Expiry, session rotation |
| Runtime-session token | Runtime process | Yes (same VM) | No | Platform session lifetime | Expiry |
| Execution role STS credentials | Every process in the VM | Yes | No | Short-lived | Role policy change |
| Presigned S3 URL | Persistence engine | Yes, briefly | No | Minutes | Expiry |
| Kiro Builder ID / SSO token | `~/.local/share/kiro-cli` | **Yes** | **Yes**, encrypted | Kiro-managed | `Sign out of Kiro` |
| MCP / third-party tokens a user adds | `~/.kiro`, `~/.config` (tool-dependent) | **Yes** | **Yes**, encrypted | Provider-managed | Provider-side revoke |

The execution role is **shared by all sandboxes of a deployment**. That is safe
today because it carries no data access and only read-only Registry actions; it
stops being safe the moment downstream business permissions are added to it.

## Known gaps and boundaries

1. **Sign-in is password-based, not PKCE.** The floating panel sends the email and
   password to the auth Lambda, which calls Cognito `ADMIN_USER_PASSWORD_AUTH`.
   MFA challenges are rejected (`CHALLENGE_NOT_SUPPORTED`) although the pool
   allows optional MFA. The control API checks the generic
   `aws.cognito.signin.user.admin` scope rather than the defined
   `kirocrew.control/invoke` scope.
2. **Public egress.** The runtime uses `NetworkMode = "PUBLIC"`. A developer
   workbench needs the internet (Kiro service, Git, package registries, MCP), but
   it also means a prompt-injected command can reach any destination.
3. **No deterministic per-tool authorization.** Apart from the denied routes,
   every KiroCrew route is tunneled; tool approval is KiroCrew's own application
   layer. Agent Registry is **discovery only** and grants nothing.
4. **Third-party tokens share the Kiro sign-in's fate.** Anything a user signs in
   to inside the sandbox is checkpointed with the workspace.
5. **Checkpoint retention is not enforced yet.** `RetentionManager` has no
   production caller (container-side deletion is intentionally forbidden), and the
   scheduled `audit` broker operation is a stub. Older generations accumulate
   until a broker-side sweep exists.
6. **Platform access logs carry invocation payloads.** Prompts and file content
   can appear in `/aws/vendedlogs/bedrock-agentcore/...`. Kiro output is
   redacted by pattern (`redact_kiro_output`), which is best-effort, not a
   guarantee.

## Hardening profiles

The two profiles below are a plan, not implemented features.

**Developer sandbox** (this sample, for trusted engineers):

- Keep internet egress, but block instance metadata and link-local addresses and
  internal CIDRs, and log DNS/HTTP destinations.
- Switch sign-in to Authorization Code + PKCE or a corporate IdP so the
  deployment never handles passwords; enable MFA; require the dedicated
  `kirocrew.control/invoke` scope.
- Move third-party credentials out of the checkpoint into a credential provider
  (for example AgentCore Identity) instead of files under `~/.kiro` / `~/.config`.
- Add a broker-side retention sweep and a real integrity audit.

**Restricted agent** (business actions such as email, CRM, GitHub writes, AWS
changes):

- VPC network mode with default-deny egress to an allowlist (Kiro, Bedrock,
  Gateway, approved repositories and package mirrors).
- Route high-risk tools through AgentCore Gateway with policy (Cedar) evaluated on
  user, tool, resource and arguments, plus human confirmation for destructive or
  outbound actions. Downstream systems still validate business state.
- Keep the execution role minimal; put downstream permissions on Gateway targets
  and credential providers, never on the shared role.
- Correlate application logs, AgentCore Observability traces and CloudTrail by a
  shared request ID, and keep secrets and prompt bodies out of logs by
  construction rather than by redaction.

## Tests to add

Existing tests cover binding mismatch and forged scheduler claims. Worth adding
before any wider rollout: reuse of a binding token after session rotation;
attempts from inside a sandbox to read another tenant's checkpoint keys; a
prompt-injection fixture that asks the agent to upload `~/.local/share/kiro-cli`;
egress to metadata and internal addresses; and account deletion across Cognito,
DynamoDB, S3 object versions and logs.
