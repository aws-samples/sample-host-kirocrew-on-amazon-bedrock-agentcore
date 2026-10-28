# Design: VPC network mode for sandboxes

Status: proposal. Nothing here is implemented; `infrastructure/modules/runtime-microvm`
hard-codes `NetworkMode = "PUBLIC"`.

## Problem

Customers who keep source code on an internal Git server (GitLab, Gerrit, a
private Bitbucket) cannot use the hosted sandbox: in `PUBLIC` mode the microVM
reaches only the public internet, so `git clone` of an internal repository fails.
The same applies to internal package mirrors, artifact stores and internal APIs.
The ask we heard is "code must not leave the intranet".

## Not a goal

- Keeping prompts and code away from the model. The agent sends code to Kiro and
  the model provider to do its job; VPC mode does not change that. It keeps
  **repository traffic** private and lets the operator restrict where else the
  sandbox can connect. Say this plainly to customers.
- Private access to the browser UI (CloudFront stays public; see *Later*).

## What the sandbox talks to today

| Destination | Why | Path in VPC mode |
|---|---|---|
| Persistence broker (Lambda `Invoke`) | checkpoint grants, lease | interface endpoint `lambda` |
| KMS | unwrap the sandbox data key | interface endpoint `kms` |
| CloudWatch Logs | runtime log shipping | interface endpoint `logs` |
| S3 (presigned URLs from the broker) | checkpoint chunks and manifests | gateway endpoint `s3` |
| ECR (image pull at start) | container image | interface endpoints `ecr.api`, `ecr.dkr` + S3 gateway (layer bucket) |
| Kiro service and Kiro login | model calls, device-code login | **public internet** (NAT) |
| `prod.download.cli.kiro.dev`, package registries, GitHub | updates, `npm`/`pip`, public repos | public internet (NAT), optional |
| Customer Git / mirrors / APIs | the reason for this mode | VPC routing: peering, Transit Gateway or Direct Connect |

Kiro has no PrivateLink endpoint that we know of, so a NAT path is required for
the agent to work at all.

## Proposal

1. **Terraform switch.** Add `network_mode = "PUBLIC" | "VPC"` (default `PUBLIC`)
   plus `vpc_subnet_ids` and `vpc_security_group_ids` to the root module, passed to
   both Runtime stacks as `NetworkConfiguration.NetworkModeConfig`. `VPC` without
   subnets or security groups fails `terraform validate` (a `check` block).
2. **Bring your own VPC.** The module does not create the VPC. The customer
   supplies private subnets (two AZs) with a route to their Git network and to a
   NAT gateway. This keeps their network team in control and avoids us owning
   routing.
3. **Endpoints module (optional).** `enable_vpc_endpoints = true` creates the
   endpoints in the table above, each with a policy scoped to this deployment's
   resources (the broker function ARN, the KMS key, the log group, the snapshot
   bucket and the ECR layer bucket). Without it, the same traffic goes through NAT.
4. **Egress restriction (documented, not built).** Customers who want "only Kiro
   and our Git" put AWS Network Firewall or a proxy in front of the NAT with a
   domain allowlist. We document the domains Kiro needs instead of building a
   firewall module.
5. **Security group.** Outbound 443 to the endpoints and NAT, outbound 22/443 to
   the Git CIDR the customer names; no inbound (AgentCore invokes the microVM
   through the service, not through the VPC).

## Platform facts to respect

- Runtimes created after the May 2026 rollout get **no service-managed S3
  gateway**: all S3 access, including image layers at startup, must be reachable
  through the customer's VPC (S3 gateway endpoint or NAT). Source:
  [VpcConfig](https://docs.aws.amazon.com/bedrock-agentcore-control/latest/APIReference/API_VpcConfig.html).
- AWS recommends ECR, S3 and Logs endpoints for VPC-mode container agents and
  scoping the S3 endpoint policy to the ECR layer bucket. Source:
  [Runtime security best practices](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-security-best-practices.html).
- Switching an existing Runtime between modes is an update of the
  `NetworkConfiguration`; test on a fresh deployment first, since a failed image
  pull leaves the Runtime unable to start any session.

## Cost

NAT gateway hourly + per-GB, plus one hourly charge per interface endpoint per AZ
(five endpoints x two AZs). This is the first fixed cost in an otherwise
scale-to-zero stack; say so in the README.

## Validation plan

1. Deploy a test stack with `network_mode = "VPC"`, endpoints on, NAT on.
2. Start a sandbox: image pull, restore, checkpoint and Stop safely all succeed.
3. From the in-sandbox terminal, `git clone` a repository on a host reachable only
   inside the VPC.
4. Turn NAT off: the sandbox starts and checkpoints (endpoints only) but Kiro calls
   fail with a clear error, which confirms the dependency table.

## Later

- Private UI access (an internal ALB + PrivateLink to the AgentCore data plane)
  for customers who do not want a public CloudFront page.
- An identity-provider-backed sign-in instead of Cognito self-registration.
