output "deployment" {
  description = "Non-secret deployment descriptor for clients and operators."
  value = {
    application_url                = module.frontend.url
    aws_region                     = local.region
    deployment_mode                = var.deployment_mode
    frontend_compatibility_version = var.frontend_compatibility_version
    protocol_version               = "kirocrew-agentcore.v1"
  }
}

output "browser_oauth" {
  description = "Public Cognito OAuth metadata consumed by the browser shell."
  value = {
    app_client_id          = module.identity.app_client_id
    authorization_endpoint = "${local.cognito_domain}/oauth2/authorize"
    issuer                 = module.identity.issuer
    logout_endpoint        = "${local.cognito_domain}/logout"
    token_endpoint         = "${local.cognito_domain}/oauth2/token"
  }
}

output "control_api" {
  description = "Non-secret control-plane endpoint metadata."
  value = {
    api_id      = module.control_api.api_id
    same_origin = "${module.frontend.url}/control/v1"
  }
}

output "resource_descriptors" {
  description = "Non-secret resource names used by deployment and audit automation."
  value = {
    cloudfront_distribution_id = module.frontend.distribution_id
    ecr_repository_url         = module.runtime_common.ecr_repository_url
    runtime_execution_role_arn = module.runtime_common.runtime_role_arn
    sandbox_table_name         = module.persistence.sandbox_table_name
    snapshot_bucket_name       = module.persistence.snapshot_bucket_name
  }
}


output "agentcore_runtime" {
  description = "Non-secret AgentCore runtime and live endpoint descriptors."
  value = var.deployment_mode == "microvm" ? {
    endpoint_arn       = module.runtime_microvm[0].endpoint_arn
    endpoint_id        = module.runtime_microvm[0].endpoint_id
    endpoint_qualifier = module.runtime_microvm[0].endpoint_qualifier
    image_uri          = module.runtime_microvm[0].runtime_image_uri
    runtime_arn        = module.runtime_microvm[0].runtime_arn
    runtime_id         = module.runtime_microvm[0].runtime_id
    runtime_version    = module.runtime_microvm[0].runtime_version
  } : null
}


output "scheduled_wake" {
  description = "Non-secret descriptors for the scheduled wake, or null when it is not enabled."
  value = length(module.scheduler) > 0 ? {
    function_name = module.scheduler[0].waker_function_name
    schedule_name = module.scheduler[0].schedule_name
    expression    = var.wake_schedule_expression
    timezone      = var.wake_schedule_timezone
    # Surfaced because it is the number to revisit once a real workspace restore
    # has been timed. The only measurement so far was against an empty one.
    lead_seconds = var.wake_lead_seconds
  } : null
}
