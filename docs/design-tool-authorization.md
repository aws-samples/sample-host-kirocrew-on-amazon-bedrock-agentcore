# Design: per-user and per-group tool authorization

Status: proposal for #40. Phase 1 is implemented behind `enable_tool_authorization`
(`infrastructure/modules/tool-authorization`); phases 2 and 3 are not.

## Problem

Every sandbox runs under one shared Runtime execution role. That role lists the
AgentCore Registry and calls the Gateway, so every user sees every registered
tool and can call any of them. A finance-only MCP server is visible and callable
from an engineer's sandbox. "Listed in Registry" and "allowed to call" are the
same thing today.

## Goal

- User A sees and can call tool X; user B in another group sees neither.
- A call to a tool outside your group is denied **at the Gateway**, even if the
  sandbox knows the endpoint.
- Works for scheduled wakes, when no browser and no user token exist.
- The sandbox never holds a long-lived credential.

## Not a goal

- Replacing Kiro's own enterprise MCP governance (an allow-list per Kiro
  profile). That stays, and is coarser: per profile, not per user.
- Authorization inside a tool. A tool that needs row-level rules still enforces
  them itself, from the identity the Gateway passes through.

## Where enforcement belongs

| Layer | Decides | Notes |
|---|---|---|
| AgentCore Gateway + Policy (Cedar) | **may this principal call this tool** | default-deny; `permit` rules on token claims such as `groups`. The only layer the sandbox cannot bypass. Supports `LOG_ONLY` for a dry run. |
| Gateway interceptor or Registry filter | **which tools this principal sees** | discovery hygiene, not security: hiding a tool is not denying it. |
| Kiro MCP governance | which MCP servers a Kiro profile may load | org-wide guard rail, already supported. |

Sources: [AGENTSEC02-BP01 Implement tool authorization](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentsec02-bp01.html),
[Gateway interceptors for fine-grained access](https://aws.amazon.com/blogs/machine-learning/apply-fine-grained-access-control-with-bedrock-agentcore-gateway-interceptors/).

## The hard part: whose token does the sandbox present?

The Gateway's inbound side is an OAuth resource server: it needs a JWT that
names the user. The sandbox has a binding token (ours, not OAuth), and a
scheduled wake has no user token at all.

| Option | Interactive | Scheduled wake | Stores a credential? | Verdict |
|---|---|---|---|---|
| A. Forward the browser's Cognito access token into the sandbox, renew with the binding | yes | **no** (token expires; nobody renews at 3 a.m.) | no | phase 1 only |
| B. Store the user's Cognito refresh token, encrypted | yes | yes | **yes, long-lived** | rejected |
| C. Control plane mints a short-lived user JWT from our own issuer | yes | yes | no | superseded by D |
| D. **Broker gets a Cognito machine token for the owner** (client credentials + pre token generation trigger) | yes | yes | client secret, broker only | **proposed** |

### Option C

1. A KMS asymmetric key (`ECC_NIST_P256`, sign only) is the issuer key. Its
   public JWKS and an OIDC discovery document are published as static objects on
   the existing CloudFront distribution under `/.well-known/`.
2. The sandbox asks the broker for a tool token (new operation
   `tools.token`). The broker already verifies the binding token, so it knows
   the sandbox and its owner hash. It looks up the owner's Cognito subject (the
   wake enrollment from #39 already stores it, KMS-sealed) and the user's current
   groups (`AdminListGroupsForUser`), and refuses if the user is disabled.
3. The broker signs a JWT: `iss` = our issuer, `aud` = the Gateway,
   `sub` = Cognito subject, `groups` = current groups, `exp` = 15 minutes. The
   sandbox caches it and asks again shortly before expiry.
4. The Gateway's `customJWTAuthorizer` trusts our issuer and audience. Cedar
   policies `permit` per tool on `principal.getTag("groups")`.
5. The Runtime execution role loses `bedrock-agentcore:*Registry*` and Gateway
   invoke permissions. Discovery goes through the Gateway (its search tool, or an
   interceptor that filters `ListTools` by the same claims).

Option C runs into a Gateway limit found while building phase 1: a Gateway's
`CustomJWTAuthorizer` takes exactly ONE discovery URL, so it cannot trust
Cognito for interactive calls and our own issuer for scheduled ones. Option C
would need either a second Gateway or every call routed through our issuer, plus
a JWKS we host and rotate ourselves.

### Option D (proposed for phase 2)

Keep Cognito as the only issuer, so the phase 1 Gateway works unchanged:

1. A second app client in the existing user pool, confidential (has a secret),
   `client_credentials` only, one custom scope such as `kirocrew-tools/invoke`.
   The secret sits in Secrets Manager; only the broker role may read it.
2. A pre token generation trigger on event version `V3_0` (the pool is on the
   Plus tier, which customises machine access tokens). For
   `TokenGeneration_ClientCredentials` from that client only, it reads the owner
   the broker names in `aws_client_metadata`, checks the user exists and is
   enabled, and adds the owner's current groups as a claim. Every other trigger
   source passes through unchanged.
3. The sandbox asks the broker for a tool token (new operation `tools.token`).
   The broker already verified the binding token, so it knows the owner; it
   unseals the owner's Cognito subject from the wake enrollment (#39) and calls
   the token endpoint with that subject as client metadata. The token lives
   15 minutes; the sandbox caches it and asks again before expiry.
4. The Gateway adds the machine client to `AllowedClients`. Cedar policies read
   the group claim the trigger added, so the same `permit` rules cover both
   paths; interactive calls can move to this path as well.

What changes about trust: anyone holding the machine client's secret can name
any user, the same power option C gives whoever can sign with its key. The
secret is readable only by the broker role, and the broker only names the owner
of the sandbox whose binding token it verified. The sandbox never sees the
secret.

Still to confirm on the test Gateway before building on it: that Cognito
accepts the custom group claim name on a machine token (`cognito:groups` itself
may be reserved), and that Cedar sees it as a principal tag.

Why this is safe: group membership is read at mint time, so removing a user
from a group takes effect within 15 minutes; disabling the user stops new tokens
at once. The signing key never leaves KMS. A token copied out of a sandbox is
good for at most 15 minutes, for that user's tools only.

Same pattern as the scheduled wake (#39): identity comes from a record the
control plane wrote while the user was signed in, not from anything the
sandbox claims.

## Plan

1. **Phase 1 (interactive only, small):** option A behind a flag, Gateway with
   Cognito as the JWT authorizer, Cedar policies in `LOG_ONLY`. Proves the
   policies and the group model with real traffic.
2. **Phase 2:** option D: machine client, pre token generation trigger, broker
   `tools.token`; scheduled wakes get tool access through the same Gateway.
3. **Phase 3:** remove Registry and Gateway permissions from the Runtime role;
   Cedar to `ENFORCE`.

### Phase 1 as built

| Piece | What it is |
|---|---|
| Groups | `<prefix>-tools-finance`, `<prefix>-tools-eng` in the existing user pool |
| Tools | one demo Lambda behind target `demo-tools`: `finance_report`, `eng_status` |
| Gateway | MCP, `CUSTOM_JWT` against the user pool, `AllowedClients` = the browser client (Cognito access tokens carry `client_id`, not `aud`) |
| Policies | one `permit` per group on `principal.getTag("cognito:groups")`, engine in `LOG_ONLY` |

The Gateway role invokes only the demo Lambda and reaches only its own policy
engine and gateway (matched by name, since the IDs exist only after the stack).
Sandboxes are not pointed at this Gateway yet; phase 1 is exercised with a
signed-in user's access token from outside the sandbox.

## Acceptance

Two Cognito groups, two users, one tool per group:
- Each user's `ListTools` shows only their tool.
- A direct call to the other group's tool is denied by the Gateway (and visible
  in Gateway logs as a policy deny).
- The same holds inside a scheduled wake with the browser closed.
- Removing a user from a group revokes access within 15 minutes.

## Open questions

- Gateway's `customJWTAuthorizer`: confirm it accepts an ES256 issuer we host and
  a custom `groups` claim in Cedar (`principal.getTag`), on a test Gateway before
  phase 2.
- Whether the Gateway's built-in search tool honours the same policies, or needs
  the interceptor.
