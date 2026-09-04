variable "prefix" { type = string }
variable "sandbox_table_arn" { type = string }
variable "sandbox_table_key_arn" { type = string }
variable "binding_key_arn" { type = string }
variable "broker_function_arn" { type = string }
variable "log_retention_days" { type = number }
variable "tags" { type = map(string) }

resource "aws_ecr_repository" "runtime" {
  name                 = "${var.prefix}-runtime"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "KMS" }
  tags = var.tags
}

resource "aws_ecr_lifecycle_policy" "runtime" {
  repository = aws_ecr_repository.runtime.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the most recent 25 immutable releases"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 25
      }
      action = { type = "expire" }
    }]
  })
}

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "aws_iam_policy_document" "runtime_assume" {
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

resource "aws_iam_role" "runtime" {
  name               = "${var.prefix}-runtime"
  assume_role_policy = data.aws_iam_policy_document.runtime_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "SandboxLeaseOnly"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [var.sandbox_table_arn]
  }
  statement {
    sid       = "SandboxStateCryptography"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [var.sandbox_table_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"]
    }
  }
  statement {
    sid       = "VerifySandboxBindings"
    actions   = ["kms:GetPublicKey", "kms:Verify"]
    resources = [var.binding_key_arn]
  }
  statement {
    sid       = "InvokePersistenceBroker"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.broker_function_arn]
  }
  statement {
    sid       = "WriteRuntimeLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.runtime.arn}:*"]
  }
  statement {
    sid       = "WriteTelemetry"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["KiroCrew/AgentCore"]
    }
  }
  statement {
    sid       = "PullRuntimeImage"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid       = "ReadRuntimeImage"
    actions   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    resources = [aws_ecr_repository.runtime.arn]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "${var.prefix}-runtime"
  role   = aws_iam_role.runtime.id
  policy = data.aws_iam_policy_document.runtime.json
}

resource "aws_cloudwatch_log_group" "runtime" {
  name              = "/aws/bedrock-agentcore/${var.prefix}"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

data "aws_iam_policy_document" "deployment_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
    condition {
      test     = "Bool"
      variable = "aws:MultiFactorAuthPresent"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role" "deployment" {
  name                 = "${var.prefix}-deployment"
  assume_role_policy   = data.aws_iam_policy_document.deployment_assume.json
  max_session_duration = 3600
  tags                 = var.tags
}

output "ecr_repository_url" { value = aws_ecr_repository.runtime.repository_url }
output "ecr_repository_arn" { value = aws_ecr_repository.runtime.arn }
output "runtime_role_arn" { value = aws_iam_role.runtime.arn }
output "deployment_role_arn" { value = aws_iam_role.deployment.arn }
output "runtime_log_group" { value = aws_cloudwatch_log_group.runtime.name }
