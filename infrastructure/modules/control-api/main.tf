variable "prefix" { type = string }
variable "issuer" { type = string }
variable "app_client_id" { type = string }
variable "user_pool_id" { type = string }
variable "user_pool_arn" { type = string }
variable "allowed_email_domains" { type = list(string) }
variable "allowed_origin" { type = string }
variable "sandbox_table_name" { type = string }
variable "sandbox_table_arn" { type = string }
variable "sandbox_table_key_arn" { type = string }
variable "binding_audience" { type = string }
variable "binding_key_arn" { type = string }
variable "broker_function_arn" { type = string }
variable "runtime_arn" { type = string }
variable "region" { type = string }
variable "runtime_qualifier" { type = string }
variable "deployment_mode" { type = string }
variable "frontend_compatibility_version" { type = string }
variable "log_retention_days" { type = number }
variable "source_directory" { type = string }
variable "auth_source_directory" { type = string }
variable "tags" { type = map(string) }

data "archive_file" "control" {
  type        = "zip"
  source_dir  = var.source_directory
  output_path = "${path.root}/.terraform/${var.prefix}-control.zip"
  excludes    = ["kirocrew_agentcore_control/__pycache__"]
}

data "aws_partition" "current" {}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "control" {
  name               = "${var.prefix}-control"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_cloudwatch_log_group" "control" {
  name              = "/aws/lambda/${var.prefix}-control"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "control" {
  statement {
    sid       = "SandboxState"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:DeleteItem"]
    resources = [var.sandbox_table_arn]
  }
  statement {
    sid       = "SandboxStateCryptography"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.sandbox_table_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${var.region}.${data.aws_partition.current.dns_suffix}"]
    }
  }
  statement {
    sid       = "SignAndVerifyBindings"
    actions   = ["kms:GetPublicKey", "kms:Sign", "kms:Verify"]
    resources = [var.binding_key_arn]
  }
  statement {
    sid       = "InvokePersistenceBroker"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.broker_function_arn]
  }
  statement {
    sid       = "StopConfiguredRuntimeOnly"
    actions   = ["bedrock-agentcore:StopRuntimeSession"]
    resources = [var.runtime_arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.control.arn}:*"]
  }
  statement {
    sid       = "ControlMetrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["KiroCrew/AgentCore"]
    }
  }
}

resource "aws_iam_role_policy" "control" {
  name   = "${var.prefix}-control"
  role   = aws_iam_role.control.id
  policy = data.aws_iam_policy_document.control.json
}

resource "aws_lambda_function" "control" {
  function_name    = "${var.prefix}-control"
  role             = aws_iam_role.control.arn
  runtime          = "python3.12"
  handler          = "kirocrew_agentcore_control.lambda_handler.handler"
  filename         = data.archive_file.control.output_path
  source_code_hash = data.archive_file.control.output_base64sha256
  timeout          = 30
  memory_size      = 512

  environment {
    variables = {
      ALLOWED_ORIGIN                 = var.allowed_origin
      APP_CLIENT_ID                  = var.app_client_id
      BINDING_AUDIENCE               = var.binding_audience
      BINDING_KEY_ARN                = var.binding_key_arn
      DEPLOYMENT_MODE                = var.deployment_mode
      FRONTEND_COMPATIBILITY_VERSION = var.frontend_compatibility_version
      ISSUER                         = var.issuer
      PERSISTENCE_BROKER_ARN         = var.broker_function_arn
      REGION                         = var.region
      REQUIRED_SCOPE                 = "aws.cognito.signin.user.admin"
      RUNTIME_ARN                    = var.runtime_arn
      RUNTIME_QUALIFIER              = var.runtime_qualifier
      SANDBOX_TABLE                  = var.sandbox_table_name
    }
  }

  tracing_config { mode = "Active" }
  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.control, aws_iam_role_policy.control]
}

resource "aws_apigatewayv2_api" "control" {
  name          = "${var.prefix}-control"
  protocol_type = "HTTP"
  tags          = var.tags
}

resource "aws_apigatewayv2_authorizer" "cognito" {
  api_id           = aws_apigatewayv2_api.control.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "cognito"

  jwt_configuration {
    audience = [var.app_client_id]
    issuer   = var.issuer
  }
}

resource "aws_apigatewayv2_integration" "control" {
  api_id                 = aws_apigatewayv2_api.control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.control.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

locals {
  routes = toset([
    "GET /control/v1/config",
    "GET /control/v1/sandbox",
    "DELETE /control/v1/sandbox",
    "POST /control/v1/sandbox/start",
    "POST /control/v1/sandbox/stop",
    "GET /control/v1/sandbox/checkpoints",
  ])
}

resource "aws_apigatewayv2_route" "control" {
  for_each = local.routes

  api_id             = aws_apigatewayv2_api.control.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.control.id}"
  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
  # Password-auth access tokens carry this scope; OAuth resource-server
  # scopes are only minted by the retired hosted-UI code flow.
  authorization_scopes = ["aws.cognito.signin.user.admin"]
}

# --- Gated authentication (registration and sign-in through Lambda only) ---

data "archive_file" "auth" {
  type        = "zip"
  source_dir  = var.auth_source_directory
  output_path = "${path.root}/.terraform/${var.prefix}-auth.zip"
  excludes    = ["kirocrew_agentcore_auth/__pycache__"]
}

resource "aws_iam_role" "auth" {
  name               = "${var.prefix}-auth"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

resource "aws_cloudwatch_log_group" "auth" {
  name              = "/aws/lambda/${var.prefix}-auth"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "auth" {
  statement {
    sid = "GatedCognitoAuthentication"
    actions = [
      "cognito-idp:AdminCreateUser",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminInitiateAuth",
      "cognito-idp:AdminSetUserPassword",
    ]
    resources = [var.user_pool_arn]
  }
  statement {
    sid       = "WriteLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.auth.arn}:*"]
  }
}

resource "aws_iam_role_policy" "auth" {
  name   = "${var.prefix}-auth"
  role   = aws_iam_role.auth.id
  policy = data.aws_iam_policy_document.auth.json
}

resource "aws_lambda_function" "auth" {
  function_name    = "${var.prefix}-auth"
  role             = aws_iam_role.auth.arn
  runtime          = "python3.12"
  handler          = "kirocrew_agentcore_auth.lambda_handler.handler"
  filename         = data.archive_file.auth.output_path
  source_code_hash = data.archive_file.auth.output_base64sha256
  timeout          = 10
  memory_size      = 256

  environment {
    variables = {
      ALLOWED_EMAIL_DOMAINS = join(",", var.allowed_email_domains)
      APP_CLIENT_ID         = var.app_client_id
      REGION                = var.region
      USER_POOL_ID          = var.user_pool_id
    }
  }

  tracing_config { mode = "Active" }
  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.auth, aws_iam_role_policy.auth]
}

resource "aws_apigatewayv2_integration" "auth" {
  api_id                 = aws_apigatewayv2_api.control.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.auth.invoke_arn
  integration_method     = "POST"
  payload_format_version = "2.0"
  timeout_milliseconds   = 15000
}

resource "aws_apigatewayv2_route" "auth" {
  for_each = toset([
    "POST /auth/v1/register",
    "POST /auth/v1/login",
    "POST /auth/v1/refresh",
  ])

  api_id             = aws_apigatewayv2_api.control.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.auth.id}"
  authorization_type = "NONE"
}

resource "aws_lambda_permission" "auth_api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auth.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control.execution_arn}/*/*"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.control.id
  name        = "$default"
  auto_deploy = true

  default_route_settings {
    detailed_metrics_enabled = true
    throttling_burst_limit   = 50
    throttling_rate_limit    = 25
  }
  tags = var.tags
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.control.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.control.execution_arn}/*/*"
}

resource "aws_cloudwatch_metric_alarm" "authorization_failures" {
  alarm_name          = "${var.prefix}-control-authorization-failures"
  namespace           = "AWS/ApiGateway"
  metric_name         = "4xx"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 10
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = []
  dimensions          = { ApiId = aws_apigatewayv2_api.control.id }
  tags                = var.tags
}

output "api_id" { value = aws_apigatewayv2_api.control.id }
output "api_endpoint" { value = aws_apigatewayv2_api.control.api_endpoint }
output "function_arn" { value = aws_lambda_function.control.arn }
output "role_arn" { value = aws_iam_role.control.arn }
