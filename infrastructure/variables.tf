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
  default     = "0.2.0"
}

variable "frontend_asset_directory" {
  description = "Directory containing the compiled upstream SPA and injected bootstrap assets."
  type        = string
  default     = "../frontend-shell/upstream/0.2.0"
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
  default     = ["kirocrew.control/invoke"]

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
