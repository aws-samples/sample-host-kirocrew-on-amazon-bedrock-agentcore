provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

provider "awscc" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  resource_prefix = "${var.name_prefix}-${var.environment}"
  account_id      = data.aws_caller_identity.current.account_id
  partition       = data.aws_partition.current.partition
  dns_suffix      = data.aws_partition.current.dns_suffix
  region          = data.aws_region.current.region
  tags = merge(var.tags, {
    Application    = "kirocrew-agentcore"
    DeploymentMode = var.deployment_mode
    Environment    = var.environment
    ManagedBy      = "Terraform"
  })
}
