variable "prefix" { type = string }
variable "runtime_image_uri" { type = string }
variable "runtime_role_arn" { type = string }
variable "cognito_issuer" { type = string }
variable "cognito_app_client_id" { type = string }
variable "allowed_scopes" { type = list(string) }
variable "request_header_allowlist" { type = list(string) }
variable "idle_session_timeout_seconds" { type = number }
variable "max_lifetime_seconds" { type = number }
variable "environment_variables" { type = map(string) }
variable "tags" { type = map(string) }

# A runtime supports exactly ONE inbound auth method, so a deployment that needs
# both a browser front door and a machine one needs two runtimes -- which is what
# the AgentCore security guidance prescribes ("A runtime can support one method at
# a time; create separate versions for different authentication types").
#
#   jwt  Browser callers present a Cognito user access token. The subject the
#        adapter authorizes against comes from that token's claims.
#   iam  SigV4 callers. No authorizer block at all; the caller must hold
#        bedrock-agentcore:InvokeAgentRuntime, and user identity travels in the
#        X-Amzn-Bedrock-AgentCore-Runtime-User-Id header.
#
# The IAM path's user id is an OPAQUE string with no IdP verification, so it is
# only safe where the caller is trusted to resolve identity upstream and the
# invoke permission is narrow. Grant InvokeAgentRuntimeForUser to the scheduler
# role alone, and deny it everywhere a JWT is available.
variable "inbound_auth" {
  type = string
  validation {
    condition     = contains(["jwt", "iam"], var.inbound_auth)
    error_message = "inbound_auth must be jwt or iam."
  }
}

data "aws_cloudformation_type" "runtime" {
  type      = "RESOURCE"
  type_name = "AWS::BedrockAgentCore::Runtime"
}

data "aws_cloudformation_type" "endpoint" {
  type      = "RESOURCE"
  type_name = "AWS::BedrockAgentCore::RuntimeEndpoint"
}

locals {
  runtime_name  = "${replace(var.prefix, "-", "_")}_runtime"
  endpoint_name = "${replace(var.prefix, "-", "_")}_live"
  # `Authorization` is only accepted in the allowlist when a customJWTAuthorizer
  # is configured -- the control plane rejects the runtime outright otherwise
  # ("Authorization header can be specified in requestHeaderAllowlist only when
  # runtime is set up with customJWTAuthorizer for OAuth based authorization").
  # That coupling is enforced, not advisory, so an IAM runtime drops the header
  # rather than carrying one it could never receive.
  header_allowlist = var.inbound_auth == "jwt" ? var.request_header_allowlist : [
    for header in var.request_header_allowlist :
    header if lower(header) != "authorization"
  ]
  # Omitted entirely for `iam`: AgentCore reads the ABSENCE of an authorizer as
  # SigV4 inbound auth, so an empty or partial block is not the same thing.
  authorizer_properties = var.inbound_auth == "jwt" ? {
    AuthorizerConfiguration = {
      CustomJWTAuthorizer = {
        DiscoveryUrl   = "${var.cognito_issuer}/.well-known/openid-configuration"
        AllowedClients = [var.cognito_app_client_id]
        AllowedScopes  = var.allowed_scopes
      }
    }
  } : {}
  runtime_properties = merge(local.authorizer_properties, {
    AgentRuntimeName = local.runtime_name
    AgentRuntimeArtifact = {
      ContainerConfiguration = {
        ContainerUri = var.runtime_image_uri
      }
    }
    EnvironmentVariables = var.environment_variables
    # No filesystem configuration blocks: the workspace lives on the
    # microVM's own container disk, and durability comes exclusively
    # from the encrypted S3 checkpoints. Managed per-session storage
    # (1GB quota, 14-day retention) was dropped after its quota and
    # validation semantics caused repeated incidents.
    LifecycleConfiguration = {
      IdleRuntimeSessionTimeout = var.idle_session_timeout_seconds
      MaxLifetime               = var.max_lifetime_seconds
    }
    NetworkConfiguration = {
      NetworkMode = "PUBLIC"
    }
    ProtocolConfiguration = "HTTP"
    RequestHeaderConfiguration = {
      RequestHeaderAllowlist = local.header_allowlist
    }
    RoleArn = var.runtime_role_arn
    Tags    = var.tags
  })
  template = {
    AWSTemplateFormatVersion = "2010-09-09"
    # The `jwt` wording is preserved verbatim from before this module gained a
    # second auth mode. A description is cosmetic, but it is part of the template
    # body, so changing it would rewrite the existing stack and mint a new runtime
    # VERSION for nothing.
    Description = var.inbound_auth == "jwt" ? (
      "AgentCore microVM runtime and live endpoint for ${var.prefix}"
      ) : (
      "AgentCore microVM runtime and live endpoint for ${var.prefix} (IAM inbound auth)"
    )
    Resources = {
      Runtime = {
        Type       = "AWS::BedrockAgentCore::Runtime"
        Properties = local.runtime_properties
      }
      LiveEndpoint = {
        Type = "AWS::BedrockAgentCore::RuntimeEndpoint"
        Properties = {
          AgentRuntimeId      = { "Fn::GetAtt" = ["Runtime", "AgentRuntimeId"] }
          AgentRuntimeVersion = { "Fn::GetAtt" = ["Runtime", "AgentRuntimeVersion"] }
          Name                = local.endpoint_name
          Description         = "Live immutable microVM endpoint"
          Tags                = var.tags
        }
      }
    }
    Outputs = {
      RuntimeArn = {
        Value = { "Fn::GetAtt" = ["Runtime", "AgentRuntimeArn"] }
      }
      RuntimeId = {
        Value = { "Fn::GetAtt" = ["Runtime", "AgentRuntimeId"] }
      }
      RuntimeVersion = {
        Value = { "Fn::GetAtt" = ["Runtime", "AgentRuntimeVersion"] }
      }
      EndpointArn = {
        Value = { "Fn::GetAtt" = ["LiveEndpoint", "AgentRuntimeEndpointArn"] }
      }
      EndpointId = {
        Value = { "Fn::GetAtt" = ["LiveEndpoint", "Id"] }
      }
      EndpointQualifier = {
        Value = local.endpoint_name
      }
    }
  }
}

resource "aws_cloudformation_stack" "runtime" {
  name          = "${var.prefix}-runtime-microvm-v2"
  template_body = jsonencode(local.template)
  tags          = var.tags

  lifecycle {
    precondition {
      condition     = can(regex("@sha256:[0-9a-f]{64}$", var.runtime_image_uri))
      error_message = "The microVM runtime image must use an immutable ECR @sha256 digest URI."
    }
    precondition {
      condition     = length(local.header_allowlist) > 0 && length(local.header_allowlist) <= 20
      error_message = "AgentCore request header allowlist must contain 1-20 headers."
    }
    precondition {
      condition     = data.aws_cloudformation_type.runtime.type_name == "AWS::BedrockAgentCore::Runtime" && data.aws_cloudformation_type.endpoint.type_name == "AWS::BedrockAgentCore::RuntimeEndpoint"
      error_message = "The selected AWS region must register official AgentCore Runtime and RuntimeEndpoint CloudFormation types."
    }
  }
}

output "runtime_arn" { value = aws_cloudformation_stack.runtime.outputs["RuntimeArn"] }
output "runtime_id" { value = aws_cloudformation_stack.runtime.outputs["RuntimeId"] }
output "runtime_version" { value = aws_cloudformation_stack.runtime.outputs["RuntimeVersion"] }
output "endpoint_arn" { value = aws_cloudformation_stack.runtime.outputs["EndpointArn"] }
output "endpoint_id" { value = aws_cloudformation_stack.runtime.outputs["EndpointId"] }
output "endpoint_qualifier" { value = aws_cloudformation_stack.runtime.outputs["EndpointQualifier"] }
output "runtime_image_uri" { value = var.runtime_image_uri }
