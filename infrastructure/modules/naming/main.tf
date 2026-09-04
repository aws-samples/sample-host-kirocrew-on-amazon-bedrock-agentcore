variable "name_prefix" {
  type = string
}

variable "environment" {
  type = string
}

variable "deployment_mode" {
  type = string
}

variable "account_id" {
  type = string
}

variable "region" {
  type = string
}

variable "extra_tags" {
  type    = map(string)
  default = {}
}

locals {
  prefix = "${var.name_prefix}-${var.environment}"
  tags = merge(var.extra_tags, {
    Application    = "kirocrew-agentcore"
    DeploymentMode = var.deployment_mode
    Environment    = var.environment
    ManagedBy      = "Terraform"
  })
  global_suffix = "${var.account_id}-${var.region}"
}

output "prefix" {
  value = local.prefix
}

output "global_suffix" {
  value = local.global_suffix
}

output "tags" {
  value = local.tags
}
