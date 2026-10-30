# Phase 1 of #40 (docs/design-tool-authorization.md): a Gateway that
# authenticates with the user's Cognito access token and evaluates Cedar
# policies per group in LOG_ONLY mode. Nothing is denied yet: the policy
# engine only records what it WOULD decide, so real traffic can prove the
# policies and the group model before anything is enforced.
#
# Off by default (`enable_tool_authorization`). It adds new resources only and
# changes nothing the sandboxes already use.

variable "prefix" { type = string }
variable "tags" { type = map(string) }
variable "user_pool_id" { type = string }
variable "cognito_issuer" { type = string }
variable "cognito_app_client_id" { type = string }

variable "policy_mode" {
  description = "Gateway policy engine mode. Phase 1 keeps LOG_ONLY; ENFORCE belongs to phase 3."
  type        = string
  default     = "LOG_ONLY"
  validation {
    condition     = contains(["LOG_ONLY", "ENFORCE"], var.policy_mode)
    error_message = "policy_mode must be LOG_ONLY or ENFORCE."
  }
}

data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "aws_cloudformation_type" "gateway" {
  type      = "RESOURCE"
  type_name = "AWS::BedrockAgentCore::Gateway"
}

data "aws_cloudformation_type" "policy_engine" {
  type      = "RESOURCE"
  type_name = "AWS::BedrockAgentCore::PolicyEngine"
}

locals {
  # AgentCore names allow letters, digits and underscores; the policy engine
  # ID is "<name>-<10 chars>", which the IAM statements below match.
  base_name    = replace(var.prefix, "-", "_")
  engine_name  = "${local.base_name}_tools_policy"
  gateway_name = "${var.prefix}-tools"
  # Target names allow letters, digits and single hyphens only.
  target_name      = "demo-tools"
  finance_group    = "${var.prefix}-tools-finance"
  eng_group        = "${var.prefix}-tools-eng"
  arn_prefix       = "arn:aws:bedrock-agentcore:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
  engine_arn_glob  = "${local.arn_prefix}:policy-engine/${local.engine_name}-*"
  gateway_arn_glob = "${local.arn_prefix}:gateway/${local.gateway_name}-*"
}

# --- Groups -----------------------------------------------------------------
# Cognito puts a user's groups in the access token's `cognito:groups` claim,
# which the Gateway turns into a principal tag for Cedar.

resource "aws_cognito_user_group" "finance" {
  user_pool_id = var.user_pool_id
  name         = local.finance_group
  description  = "May call the finance demo tool (tool authorization phase 1)."
}

resource "aws_cognito_user_group" "eng" {
  user_pool_id = var.user_pool_id
  name         = local.eng_group
  description  = "May call the engineering demo tool (tool authorization phase 1)."
}

# --- Demo tool Lambda -------------------------------------------------------

data "archive_file" "tools" {
  type        = "zip"
  source_file = "${path.module}/src/tools.py"
  output_path = "${path.root}/.terraform/${var.prefix}-demo-tools.zip"
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "tools" {
  name               = "${var.prefix}-demo-tools"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
  tags               = var.tags
}

resource "aws_cloudwatch_log_group" "tools" {
  name              = "/aws/lambda/${var.prefix}-demo-tools"
  retention_in_days = 14
  tags              = var.tags
}

data "aws_iam_policy_document" "tools_logs" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.tools.arn}:*"]
  }
}

resource "aws_iam_role_policy" "tools_logs" {
  role   = aws_iam_role.tools.id
  policy = data.aws_iam_policy_document.tools_logs.json
}

resource "aws_lambda_function" "tools" {
  function_name    = "${var.prefix}-demo-tools"
  role             = aws_iam_role.tools.arn
  runtime          = "python3.12"
  handler          = "tools.handler"
  architectures    = ["arm64"]
  timeout          = 10
  filename         = data.archive_file.tools.output_path
  source_code_hash = data.archive_file.tools.output_base64sha256
  tags             = var.tags
  depends_on       = [aws_iam_role_policy.tools_logs]
}

# --- Gateway service role ---------------------------------------------------

data "aws_iam_policy_document" "gateway_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.prefix}-tools-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_trust.json
  tags               = var.tags
}

# From the AgentCore guide "AgentCore Gateway and Policy in AgentCore IAM
# Permissions". Resources are matched by name prefix, because the IDs are only
# known once the stack below exists and the Gateway needs the role first.
data "aws_iam_policy_document" "gateway" {
  statement {
    sid       = "InvokeDemoTools"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.tools.arn]
  }
  statement {
    sid       = "PolicyEngineConfiguration"
    actions   = ["bedrock-agentcore:GetPolicyEngine"]
    resources = [local.engine_arn_glob]
  }
  statement {
    sid       = "PolicyEngineAuthorization"
    actions   = ["bedrock-agentcore:AuthorizeAction", "bedrock-agentcore:PartiallyAuthorizeActions"]
    resources = [local.engine_arn_glob, local.gateway_arn_glob]
  }
}

resource "aws_iam_role_policy" "gateway" {
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

# --- Gateway, target and Cedar policies (CloudFormation) ---------------------

locals {
  gateway_ref = { "Fn::GetAtt" = ["Gateway", "GatewayArn"] }

  # Tool names reach Cedar as "<target>___<tool>".
  policies = {
    FinancePolicy = {
      name  = "${local.base_name}_finance_tools"
      tool  = "finance_report"
      group = local.finance_group
    }
    EngPolicy = {
      name  = "${local.base_name}_eng_tools"
      tool  = "eng_status"
      group = local.eng_group
    }
  }

  template = {
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = "KiroCrew tool authorization phase 1 (#40): Gateway + Cedar policies in ${var.policy_mode}."
    Resources = merge(
      {
        PolicyEngine = {
          Type = "AWS::BedrockAgentCore::PolicyEngine"
          Properties = {
            Name        = local.engine_name
            Description = "Per-group tool policies for ${var.prefix}."
          }
        }
        Gateway = {
          Type      = "AWS::BedrockAgentCore::Gateway"
          DependsOn = ["PolicyEngine"]
          Properties = {
            Name           = local.gateway_name
            Description    = "Tool Gateway that authenticates with the user's Cognito access token."
            ProtocolType   = "MCP"
            RoleArn        = aws_iam_role.gateway.arn
            AuthorizerType = "CUSTOM_JWT"
            AuthorizerConfiguration = {
              CustomJWTAuthorizer = {
                DiscoveryUrl = "${var.cognito_issuer}/.well-known/openid-configuration"
                # Cognito access tokens carry `client_id`, not `aud`.
                AllowedClients = [var.cognito_app_client_id]
              }
            }
            PolicyEngineConfiguration = {
              Arn  = { "Fn::GetAtt" = ["PolicyEngine", "PolicyEngineArn"] }
              Mode = var.policy_mode
            }
          }
        }
        DemoTarget = {
          Type = "AWS::BedrockAgentCore::GatewayTarget"
          Properties = {
            GatewayIdentifier                = { "Fn::GetAtt" = ["Gateway", "GatewayIdentifier"] }
            Name                             = local.target_name
            Description                      = "Two demo tools, one per group."
            CredentialProviderConfigurations = [{ CredentialProviderType = "GATEWAY_IAM_ROLE" }]
            TargetConfiguration = {
              Mcp = {
                Lambda = {
                  LambdaArn = aws_lambda_function.tools.arn
                  ToolSchema = {
                    InlinePayload = [
                      {
                        Name        = "finance_report"
                        Description = "Returns the demo finance report. Finance group only."
                        InputSchema = { Type = "object", Properties = {} }
                      },
                      {
                        Name        = "eng_status"
                        Description = "Returns the demo engineering status. Engineering group only."
                        InputSchema = { Type = "object", Properties = {} }
                      },
                    ]
                  }
                }
              }
            }
          }
        }
      },
      {
        for logical_id, p in local.policies : logical_id => {
          Type = "AWS::BedrockAgentCore::Policy"
          # Policies are validated against the schema generated from the
          # Gateway's tools, so the target must exist first.
          DependsOn = ["DemoTarget"]
          Properties = {
            PolicyEngineId = { "Fn::GetAtt" = ["PolicyEngine", "PolicyEngineId"] }
            Name           = p.name
            Description    = "Members of ${p.group} may call ${p.tool}."
            ValidationMode = "FAIL_ON_ANY_FINDINGS"
            Definition = {
              Cedar = {
                # hasTag first: reading a missing tag is an error that denies.
                # `like` matches the group wherever it appears in the claim;
                # the two group names are not substrings of each other.
                Statement = {
                  "Fn::Sub" = [
                    join("\n", [
                      "permit(",
                      "  principal is AgentCore::OAuthUser,",
                      "  action == AgentCore::Action::\"${local.target_name}___${p.tool}\",",
                      "  resource == AgentCore::Gateway::\"$${GatewayArn}\"",
                      ")",
                      "when {",
                      "  principal.hasTag(\"cognito:groups\") &&",
                      "  principal.getTag(\"cognito:groups\") like \"*${p.group}*\"",
                      "};",
                    ]),
                    { GatewayArn = local.gateway_ref },
                  ]
                }
              }
            }
          }
        }
      },
    )
    Outputs = {
      GatewayArn      = { Value = local.gateway_ref }
      GatewayId       = { Value = { "Fn::GetAtt" = ["Gateway", "GatewayIdentifier"] } }
      GatewayUrl      = { Value = { "Fn::GetAtt" = ["Gateway", "GatewayUrl"] } }
      PolicyEngineArn = { Value = { "Fn::GetAtt" = ["PolicyEngine", "PolicyEngineArn"] } }
    }
  }
}

resource "aws_cloudformation_stack" "tools" {
  name          = "${var.prefix}-tool-authorization"
  template_body = jsonencode(local.template)
  tags          = var.tags
  depends_on    = [aws_iam_role_policy.gateway]

  lifecycle {
    precondition {
      condition     = data.aws_cloudformation_type.gateway.type_name == "AWS::BedrockAgentCore::Gateway" && data.aws_cloudformation_type.policy_engine.type_name == "AWS::BedrockAgentCore::PolicyEngine"
      error_message = "The selected AWS region must register the AgentCore Gateway and PolicyEngine CloudFormation types."
    }
  }
}

output "gateway_arn" { value = aws_cloudformation_stack.tools.outputs["GatewayArn"] }
output "gateway_url" { value = aws_cloudformation_stack.tools.outputs["GatewayUrl"] }
output "policy_engine_arn" { value = aws_cloudformation_stack.tools.outputs["PolicyEngineArn"] }
output "finance_group" { value = aws_cognito_user_group.finance.name }
output "eng_group" { value = aws_cognito_user_group.eng.name }
