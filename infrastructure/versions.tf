terraform {
  required_version = "= 1.12.2"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.10.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = "= 1.56.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "= 2.7.1"
    }
  }
}

variable "deployment_mode" {
  description = "AgentCore deployment profile selected by build and plan commands."
  type        = string
  default     = "microvm"

  validation {
    condition     = contains(["microvm", "instances"], var.deployment_mode)
    error_message = "deployment_mode must be microvm or instances."
  }
}
