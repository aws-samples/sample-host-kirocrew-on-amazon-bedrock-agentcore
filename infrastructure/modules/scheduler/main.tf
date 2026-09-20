variable "prefix" { type = string }
variable "schedule_expression" { type = string }
variable "schedule_enabled" { type = bool }
variable "schedule_timezone" { type = string }
variable "wake_lead_seconds" { type = number }
variable "wake_dwell_seconds" { type = number }
variable "cognito_subject" { type = string }
variable "sandbox_id" { type = string }
variable "sandbox_table_name" { type = string }
variable "sandbox_table_arn" { type = string }
variable "sandbox_key_arn" { type = string }
variable "control_function_arn" { type = string }
variable "control_function_name" { type = string }
variable "machine_runtime_arn" { type = string }
variable "machine_endpoint_qualifier" { type = string }
variable "wake_path" { type = string }
variable "log_retention_days" { type = number }
variable "source_directory" { type = string }
variable "tags" { type = map(string) }

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

data "archive_file" "waker" {
  type        = "zip"
  source_dir  = var.source_directory
  output_path = "${path.root}/.terraform/${var.prefix}-waker.zip"
  excludes    = ["kirocrew_agentcore_waker/__pycache__"]
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

resource "aws_iam_role" "waker" {
  name               = "${var.prefix}-waker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = var.tags
}

data "aws_iam_policy_document" "waker" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.waker.arn}:*"]
  }

  # The control plane is the ONLY component holding the binding signing key, so
  # the waker cannot mint its own credential even if this role were misused.
  statement {
    sid       = "MintSchedulerBinding"
    actions   = ["lambda:InvokeFunction"]
    resources = [var.control_function_arn]
  }

  # Scoped to the machine runtime alone. The browser runtime is deliberately NOT
  # listed: it is OAuth-fronted and unreachable this way, and naming it would
  # only suggest otherwise.
  statement {
    sid       = "WakeMachineEndpoint"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [var.machine_runtime_arn, "${var.machine_runtime_arn}/*"]
  }

  # Read-only, and one item. The waker verifies that a wake actually PERSISTED by
  # comparing the committed generation before and after, because the runtime's own
  # response cannot be relied on to say so: `sandbox.prepare_stop` answers with an
  # empty event list in some states, and nothing in that answer separates "already
  # committed" from "refused" from "worked". The record is the source of truth.
  statement {
    sid       = "ReadSandboxGeneration"
    actions   = ["dynamodb:GetItem"]
    resources = [var.sandbox_table_arn]
  }

  # The table is encrypted with a customer-managed key, so GetItem alone is not
  # enough -- without this the read fails with "KMS key access denied" and the
  # waker cannot answer the one question it exists to answer. Decrypt only: the
  # waker never writes to the table, and a wake must never be able to.
  statement {
    sid       = "DecryptSandboxRecord"
    actions   = ["kms:Decrypt"]
    resources = [var.sandbox_key_arn]
    condition {
      test     = "StringEquals"
      variable = "kms:ViaService"
      values   = ["dynamodb.${data.aws_region.current.region}.amazonaws.com"]
    }
  }

  # A caller that can attribute a request to an arbitrary user id is a caller
  # that can act as any user. The AWS guidance is to deny this explicitly
  # wherever it is not needed, rather than rely on it not being called.
  statement {
    sid       = "DenyActingAsAnotherUser"
    effect    = "Deny"
    actions   = ["bedrock-agentcore:InvokeAgentRuntimeForUser"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "waker" {
  name   = "${var.prefix}-waker"
  role   = aws_iam_role.waker.id
  policy = data.aws_iam_policy_document.waker.json
}

resource "aws_cloudwatch_log_group" "waker" {
  name              = "/aws/lambda/${var.prefix}-waker"
  retention_in_days = var.log_retention_days
  tags              = var.tags
}

resource "aws_lambda_function" "waker" {
  function_name    = "${var.prefix}-waker"
  role             = aws_iam_role.waker.arn
  runtime          = "python3.12"
  handler          = "kirocrew_agentcore_waker.lambda_handler.handler"
  filename         = data.archive_file.waker.output_path
  source_code_hash = data.archive_file.waker.output_base64sha256
  # Budgeted from measurements, not guessed. A restore took 46-65s, a real
  # workspace's final checkpoint took 92s, and the generation poll allows 30s on top
  # of the configurable dwell. Dying while holding a claimed sandbox is the one
  # outcome worse than not waking it, so the headroom is deliberate.
  timeout     = min(900, 360 + var.wake_dwell_seconds)
  memory_size = 512

  environment {
    variables = {
      COGNITO_SUBJECT            = var.cognito_subject
      SANDBOX_ID                 = var.sandbox_id
      CONTROL_FUNCTION_NAME      = var.control_function_name
      MACHINE_RUNTIME_ARN        = var.machine_runtime_arn
      MACHINE_ENDPOINT_QUALIFIER = var.machine_endpoint_qualifier
      WAKE_PATH                  = var.wake_path
      WAKE_DWELL_SECONDS         = tostring(var.wake_dwell_seconds)
      SANDBOX_TABLE_NAME         = var.sandbox_table_name
    }
  }

  depends_on = [aws_cloudwatch_log_group.waker]
  tags       = var.tags
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    # Without this, any account able to reach the scheduler service principal
    # could borrow the role. The pair is the standard confused-deputy guard.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.prefix}-waker-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
  tags               = var.tags
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.prefix}-waker-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.waker.arn
    }]
  })
}

resource "aws_scheduler_schedule" "waker" {
  name       = "${var.prefix}-waker"
  group_name = "default"

  # Kept as a variable rather than only as create/destroy, so the schedule can be
  # silenced during an investigation without tearing down the Lambda, its role and
  # its log group -- which is what an operator actually wants when a wake is
  # misbehaving and the logs are the evidence.
  state = var.schedule_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone

  # OFF, and this is load-bearing rather than a default left alone. A flexible
  # window smears the firing time across up to fifteen minutes, and a cron job
  # inside the sandbox that keeps a `cron_expr` misses its minute PERMANENTLY
  # when the sandbox is not awake for it: unlike an interval job, which comes
  # back still owing a run, an expression job silently skips the occurrence.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.waker.arn
    role_arn = aws_iam_role.scheduler.arn

    # A wake is not instant -- the microVM has to start and the workspace has to
    # be restored before the in-sandbox scheduler exists to notice the time. The
    # schedule therefore fires EARLY by the restore budget, so the sandbox is
    # already awake when the minute it cares about arrives.
    input = jsonencode({ leadSeconds = var.wake_lead_seconds })

    retry_policy {
      # ZERO retries, and this is the opposite of what it started as. A retry
      # looked free next to a missed occurrence, so it was capped at one rather
      # than none -- but the wake now DWELLS for two minutes before it
      # checkpoints, and EventBridge retried while the first attempt was still
      # holding the sandbox. Two concurrent wakes each computed "next generation
      # = N+1", one wrote the objects and the other wrote the receipt, and the
      # result was a committed generation whose manifest digest did not match its
      # own object. The next wake had to fall back to the previous generation and
      # throw that cycle's work away.
      #
      # Seven consecutive wakes were clean; the single corrupt generation appeared
      # exactly at the occurrence that ran three times. So a missed occurrence
      # costs one cycle of work, and an overlapping retry costs a cycle AND leaves
      # an invalid checkpoint behind. Skipping is strictly cheaper.
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

output "waker_function_name" { value = aws_lambda_function.waker.function_name }
output "waker_function_arn" { value = aws_lambda_function.waker.arn }
output "schedule_name" { value = aws_scheduler_schedule.waker.name }
output "schedule_arn" { value = aws_scheduler_schedule.waker.arn }
