variable "prefix" { type = string }
variable "binding_audience" { type = string }
variable "global_suffix" { type = string }
variable "retain_data" { type = bool }
variable "checkpoint_retention_days" { type = number }
variable "log_retention_days" { type = number }
variable "alarm_actions" { type = list(string) }
variable "tags" { type = map(string) }
variable "source_directory" { type = string }

resource "aws_kms_key" "snapshots" {
  description             = "${var.prefix} checkpoint envelope encryption"
  enable_key_rotation     = true
  deletion_window_in_days = var.retain_data ? 30 : 7
  tags                    = var.tags
}

resource "aws_kms_alias" "snapshots" {
  name          = "alias/${var.prefix}-snapshots"
  target_key_id = aws_kms_key.snapshots.key_id
}

resource "aws_kms_key" "bindings" {
  description              = "${var.prefix} sandbox binding signatures"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = var.retain_data ? 30 : 7
  tags                     = var.tags
}

resource "aws_kms_alias" "bindings" {
  name          = "alias/${var.prefix}-bindings"
  target_key_id = aws_kms_key.bindings.key_id
}

resource "aws_s3_bucket" "snapshots" {
  bucket        = "${var.prefix}-snapshots-${var.global_suffix}"
  force_destroy = !var.retain_data
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.snapshots.arn
      sse_algorithm     = "aws:kms"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  rule {
    id     = "checkpoint-version-retention"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = var.checkpoint_retention_days
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
  depends_on = [aws_s3_bucket_versioning.snapshots]
}

resource "aws_s3_bucket_policy" "snapshots" {
  bucket = aws_s3_bucket.snapshots.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.snapshots.arn, "${aws_s3_bucket.snapshots.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

resource "aws_dynamodb_table" "sandboxes" {
  name         = "${var.prefix}-sandboxes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.snapshots.arn
  }
  deletion_protection_enabled = var.retain_data
  tags                        = var.tags
}

data "archive_file" "broker" {
  type        = "zip"
  source_dir  = var.source_directory
  output_path = "${path.root}/.terraform/${var.prefix}-persistence.zip"
  excludes    = ["kirocrew_agentcore_persistence/__pycache__"]
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "broker" {
  name               = "${var.prefix}-persistence-broker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "broker" {
  statement {
    sid       = "SandboxMetadata"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.sandboxes.arn]
  }
  statement {
    sid       = "SandboxStateCryptography"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
    resources = [aws_kms_key.snapshots.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"]
    }
  }
  statement {
    sid       = "CheckpointObjects"
    actions   = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.snapshots.arn}/sandboxes/*"]
  }
  statement {
    sid       = "CheckpointDiscoveryForAuditor"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.snapshots.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["sandboxes/*"]
    }
  }
  statement {
    sid       = "SnapshotCryptography"
    actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.snapshots.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["s3.${data.aws_region.current.region}.${data.aws_partition.current.dns_suffix}"]
    }
  }
  statement {
    sid       = "BrokerEnvelopeCryptography"
    actions   = ["kms:Decrypt", "kms:GenerateDataKey"]
    resources = [aws_kms_key.snapshots.arn]
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:application"
      values   = ["kirocrew-agentcore"]
    }
    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:purpose"
      values   = ["sandbox-checkpoint"]
    }
  }
  statement {
    sid       = "VerifyBindingAndSignCheckpointReceipt"
    actions   = ["kms:Sign", "kms:Verify"]
    resources = [aws_kms_key.bindings.arn]
  }
  statement {
    sid       = "BasicLogging"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.broker.arn}:*"]
  }
}

data "aws_region" "current" {}
data "aws_partition" "current" {}

resource "aws_iam_role_policy" "broker" {
  name   = "${var.prefix}-persistence-broker"
  role   = aws_iam_role.broker.id
  policy = data.aws_iam_policy_document.broker.json
}

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/aws/lambda/${var.prefix}-persistence"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "broker" {
  function_name    = "${var.prefix}-persistence"
  role             = aws_iam_role.broker.arn
  runtime          = "python3.12"
  handler          = "kirocrew_agentcore_persistence.lambda_handler.handler"
  filename         = data.archive_file.broker.output_path
  source_code_hash = data.archive_file.broker.output_base64sha256
  timeout          = 60
  memory_size      = 1024

  environment {
    variables = {
      BINDING_AUDIENCE  = var.binding_audience
      BINDING_KEY_ARN   = aws_kms_key.bindings.arn
      CHECKPOINT_BUCKET = aws_s3_bucket.snapshots.id
      KMS_KEY_ARN       = aws_kms_key.snapshots.arn
      SANDBOX_TABLE     = aws_dynamodb_table.sandboxes.name
    }
  }
  tracing_config { mode = "Active" }
  tags       = var.tags
  depends_on = [aws_cloudwatch_log_group.broker, aws_iam_role_policy.broker]
}

resource "aws_cloudwatch_event_rule" "integrity_audit" {
  name                = "${var.prefix}-checkpoint-integrity"
  schedule_expression = "rate(6 hours)"
  tags                = var.tags
}

resource "aws_cloudwatch_event_target" "integrity_audit" {
  rule      = aws_cloudwatch_event_rule.integrity_audit.name
  target_id = "persistence-broker"
  arn       = aws_lambda_function.broker.arn
  input     = jsonencode({ operation = "audit" })
}

resource "aws_lambda_permission" "integrity_audit" {
  statement_id  = "AllowScheduledIntegrityAudit"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.broker.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.integrity_audit.arn
}

output "sandbox_table_name" { value = aws_dynamodb_table.sandboxes.name }
output "sandbox_table_arn" { value = aws_dynamodb_table.sandboxes.arn }
output "snapshot_bucket_name" { value = aws_s3_bucket.snapshots.id }
output "snapshot_key_arn" { value = aws_kms_key.snapshots.arn }
output "binding_key_arn" { value = aws_kms_key.bindings.arn }
output "broker_function_arn" { value = aws_lambda_function.broker.arn }
output "broker_role_arn" { value = aws_iam_role.broker.arn }
