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
  template = {
    AWSTemplateFormatVersion = "2010-09-09"
    Description              = "AgentCore microVM runtime and live endpoint for ${var.prefix}"
    Resources = {
      Runtime = {
        Type = "AWS::BedrockAgentCore::Runtime"
        Properties = {
          AgentRuntimeName = local.runtime_name
          AgentRuntimeArtifact = {
            ContainerConfiguration = {
              ContainerUri = var.runtime_image_uri
            }
          }
          AuthorizerConfiguration = {
            CustomJWTAuthorizer = {
              DiscoveryUrl   = "${var.cognito_issuer}/.well-known/openid-configuration"
              AllowedClients = [var.cognito_app_client_id]
              AllowedScopes  = var.allowed_scopes
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
            RequestHeaderAllowlist = var.request_header_allowlist
          }
          RoleArn = var.runtime_role_arn
          Tags    = var.tags
        }
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
      condition     = length(var.request_header_allowlist) > 0 && length(var.request_header_allowlist) <= 20
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
