variable "aws_region" {
  description = "AWS region for all regional resources."
  type        = string
}

variable "name_prefix" {
  description = "Lowercase deployment name used to derive resource names."
  type        = string
  default     = "kirocrew-agentcore"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,31}$", var.name_prefix))
    error_message = "name_prefix must be 3-32 lowercase alphanumeric or hyphen characters and begin with a letter."
  }
}

variable "environment" {
  description = "Deployment environment label."
  type        = string
  default     = "dev"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.environment))
    error_message = "environment must be 2-16 lowercase alphanumeric or hyphen characters."
  }
}

variable "frontend_compatibility_version" {
  description = "Pinned upstream KiroCrew frontend compatibility version."
  type        = string
  default     = "0.3.0"
}

variable "persisted_paths" {
  description = "Workspace paths included in checkpoints, shown in the panel. Must mirror PersistencePolicy in the persistence engine; a unit test pins the two together."
  type        = list(string)
  default = [
    "/mnt/workspace/home/.kiro",
    "/mnt/workspace/home/.config",
    "/mnt/workspace/home/.local/share/kiro-cli",
    "/mnt/workspace/artifacts",
    "/mnt/workspace/knowledge",
    "/mnt/workspace/memory",
    "/mnt/workspace/projects",
    "/mnt/workspace/user",
  ]
}

variable "frontend_asset_directory" {
  description = "Directory containing the compiled upstream SPA and injected bootstrap assets."
  type        = string
  default     = "../frontend-shell/upstream/0.3.0"
}

variable "oauth_callback_paths" {
  description = "Exact same-origin Cognito callback paths."
  type        = set(string)
  default     = ["/oauth/callback"]

  validation {
    condition     = length(var.oauth_callback_paths) > 0 && alltrue([for path in var.oauth_callback_paths : startswith(path, "/") && !strcontains(path, "?") && !strcontains(path, "#")])
    error_message = "OAuth callback paths must be absolute paths without query strings or fragments."
  }
}

variable "oauth_logout_paths" {
  description = "Exact same-origin Cognito logout paths."
  type        = set(string)
  default     = ["/"]

  validation {
    condition     = length(var.oauth_logout_paths) > 0 && alltrue([for path in var.oauth_logout_paths : startswith(path, "/") && !strcontains(path, "?") && !strcontains(path, "#")])
    error_message = "OAuth logout paths must be absolute paths without query strings or fragments."
  }
}

variable "retain_persisted_data" {
  description = "Retain encrypted snapshots, KMS keys, and sandbox metadata when the stack is torn down."
  type        = bool
  default     = true
}

variable "checkpoint_retention_days" {
  description = "Days to retain noncurrent checkpoint object versions."
  type        = number
  default     = 35

  validation {
    condition     = var.checkpoint_retention_days >= 15
    error_message = "checkpoint_retention_days must be at least 15 days to outlive managed-session expiry."
  }
}

variable "log_retention_days" {
  description = "CloudWatch log retention."
  type        = number
  default     = 30
}

variable "alarm_actions" {
  description = "SNS topic ARNs or other CloudWatch alarm action ARNs."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags merged with mandatory deployment tags."
  type        = map(string)
  default     = {}
}


variable "runtime_image_digest" {
  description = "Immutable multi-architecture runtime image digest published to the regional ECR repository."
  type        = string
  default     = "sha256:9df63e89172e82546da174fc1a9234423e2f81be2732c9ad79ff6df0a2fffedd"

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.runtime_image_digest))
    error_message = "runtime_image_digest must be a sha256 OCI digest."
  }
}

variable "runtime_idle_session_timeout_seconds" {
  description = "Seconds before AgentCore scales an idle runtime session to zero."
  type        = number
  default     = 900

  validation {
    condition     = var.runtime_idle_session_timeout_seconds >= 60 && var.runtime_idle_session_timeout_seconds <= 28800
    error_message = "runtime_idle_session_timeout_seconds must be between 60 and 28800."
  }
}

variable "runtime_max_lifetime_seconds" {
  description = "Maximum lifetime in seconds for an AgentCore runtime session."
  type        = number
  default     = 28800

  validation {
    condition     = var.runtime_max_lifetime_seconds >= 60 && var.runtime_max_lifetime_seconds <= 28800
    error_message = "runtime_max_lifetime_seconds must be between 60 and 28800."
  }
}

variable "runtime_allowed_scopes" {
  description = "Cognito OAuth scopes accepted by the AgentCore custom JWT authorizer."
  type        = list(string)
  default     = ["aws.cognito.signin.user.admin"]

  validation {
    condition     = length(var.runtime_allowed_scopes) > 0
    error_message = "At least one runtime OAuth scope is required."
  }
}

variable "runtime_request_header_allowlist" {
  description = "HTTP/SSE/WebSocket headers forwarded by AgentCore ingress to the runtime adapter."
  type        = list(string)
  default = [
    "Authorization",
    "Last-Event-ID",
    "X-Correlation-Id",
  ]

  validation {
    condition     = length(var.runtime_request_header_allowlist) > 0 && length(var.runtime_request_header_allowlist) <= 20 && length(distinct(var.runtime_request_header_allowlist)) == length(var.runtime_request_header_allowlist)
    error_message = "runtime_request_header_allowlist must contain 1-20 unique headers."
  }
}

variable "allowed_email_domains" {
  description = "Email domains permitted to register and sign in through the gated auth API."
  type        = list(string)
  default     = ["amazon.com"]

  validation {
    condition     = length(var.allowed_email_domains) > 0 && alltrue([for domain in var.allowed_email_domains : can(regex("^[a-z0-9.-]+\\.[a-z]{2,}$", domain))])
    error_message = "At least one lowercase email domain is required."
  }
}

variable "allowed_email_patterns" {
  description = "Regular expressions matched case-insensitively against the full email address; a match admits the address even when its domain is not in allowed_email_domains."
  type        = list(string)
  default     = ["^cosintfs@qq\\.com$"]

  validation {
    condition     = alltrue([for pattern in var.allowed_email_patterns : length(trimspace(pattern)) > 0])
    error_message = "Email patterns must be non-empty regular expressions."
  }
}

variable "enable_machine_runtime" {
  description = "Create a second microVM runtime with IAM inbound auth, for scheduled wakes that have no browser and therefore no Cognito user token."
  type        = bool
  default     = false
}

variable "machine_runtime_idle_session_timeout_seconds" {
  description = "Idle timeout for the machine-auth runtime. Memory bills for the whole session including its idle tail, so an unattended wake wants this near the 60s floor; the browser runtime keeps a long timeout to avoid cold starts for a human."
  type        = number
  default     = 120

  validation {
    condition     = var.machine_runtime_idle_session_timeout_seconds >= 60 && var.machine_runtime_idle_session_timeout_seconds <= 28800
    error_message = "AgentCore accepts 60-28800 seconds for a microVM idle session timeout."
  }
}

variable "enable_scheduled_wake" {
  description = "Create an EventBridge schedule that wakes the sandbox so its in-sandbox cron jobs can fire with nobody watching. Requires enable_machine_runtime, because a browser-fronted runtime cannot be invoked by a machine at all."
  type        = bool
  default     = false
}

variable "wake_schedule_expression" {
  description = "EventBridge Scheduler expression for the wake, e.g. cron(50 8 * * ? *) or rate(6 hours). Fire EARLY of the moment the in-sandbox job cares about: the sandbox has to be claimed and restored before its scheduler exists to notice the time."
  type        = string
  default     = "cron(50 8 * * ? *)"
}

variable "wake_schedule_enabled" {
  description = "Whether the wake schedule actually fires. Set false to silence it during an investigation without destroying the waker, its role or its log group -- the logs are usually the evidence you need when a wake is misbehaving."
  type        = bool
  default     = true
}

variable "wake_schedule_timezone" {
  description = "IANA timezone the wake expression is read in. Naming it explicitly is what makes a wall-clock schedule survive daylight saving; UTC would drift an hour against the user's day."
  type        = string
  default     = "Asia/Singapore"
}

variable "wake_lead_seconds" {
  description = "How far ahead of the in-sandbox job the wake fires, in seconds. Keep this CLOSE to the restore time: the container is reclaimed after the machine runtime's idle timeout, so waking far too early guarantees the sandbox is asleep again before the job's minute arrives. Six observed restores took 46-65 seconds."
  type        = number
  default     = 90

  validation {
    condition     = var.wake_lead_seconds >= 0 && var.wake_lead_seconds <= 3600
    error_message = "Lead time must be between 0 and 3600 seconds."
  }
}

variable "wake_dwell_seconds" {
  description = "How long the waker holds the sandbox awake after the gateway answers, so a due job actually fires before the checkpoint is taken. Set 0 to skip both the dwell and the explicit checkpoint, which persists nothing unless the container happens to outlive the periodic checkpoint interval."
  type        = number
  default     = 120

  validation {
    condition     = var.wake_dwell_seconds >= 0 && var.wake_dwell_seconds <= 600
    error_message = "Dwell must be between 0 and 600 seconds, and must leave room inside the waker's own timeout."
  }
}

variable "wake_cognito_subject" {
  description = "Cognito subject whose sandbox the schedule wakes. It has to be configured rather than discovered: the record stores only a one-way owner hash, so nothing in the deployment can recover whose sandbox it is. Supporting several owners needs the subject stored encrypted per record, which this deployment does not yet do."
  type        = string
  default     = ""
}

variable "wake_sandbox_id" {
  description = "Sandbox id the schedule expects to wake. Asserted against the subject's actual sandbox and refused on mismatch, so a stale or mistyped subject fails loudly instead of quietly waking the wrong workspace."
  type        = string
  default     = ""
}

variable "wake_path" {
  description = "Gateway path the wake requests once the sandbox is restored. Reading status is enough: the request exists to prove KiroCrew itself came up, since the container starting is NOT the same as the gateway that owns the cron scheduler starting."
  type        = string
  default     = "/api/status"
}
