module "naming" {
  source = "./modules/naming"

  name_prefix     = var.name_prefix
  environment     = var.environment
  deployment_mode = var.deployment_mode
  account_id      = local.account_id
  region          = local.region
  extra_tags      = var.tags
}

locals {
  binding_audience      = "urn:kirocrew:agentcore:${local.account_id}:${local.region}:${module.naming.prefix}"
  cognito_domain_prefix = "${module.naming.prefix}-${local.account_id}-${local.region}"
  cognito_domain        = "https://${local.cognito_domain_prefix}.auth.${local.region}.amazoncognito.com"
  runtime_image_uri     = "${module.runtime_common.ecr_repository_url}@${var.runtime_image_digest}"
  runtime_arn           = var.deployment_mode == "microvm" ? module.runtime_microvm[0].runtime_arn : "arn:${local.partition}:bedrock-agentcore:${local.region}:${local.account_id}:runtime/${module.naming.prefix}*"
  runtime_qualifier     = var.deployment_mode == "microvm" ? module.runtime_microvm[0].endpoint_qualifier : "DEFAULT"
}

module "persistence" {
  source = "./modules/persistence"

  prefix                    = module.naming.prefix
  binding_audience          = local.binding_audience
  global_suffix             = module.naming.global_suffix
  retain_data               = var.retain_persisted_data
  checkpoint_retention_days = var.checkpoint_retention_days
  log_retention_days        = var.log_retention_days
  alarm_actions             = var.alarm_actions
  tags                      = module.naming.tags
  source_directory          = "${path.module}/functions/persistence/src"

  runtime_session_token_ttl_seconds = var.runtime_max_lifetime_seconds
}

module "runtime_common" {
  source = "./modules/runtime-common"

  prefix              = module.naming.prefix
  binding_key_arn     = module.persistence.binding_key_arn
  broker_function_arn = module.persistence.broker_function_arn
  log_retention_days  = var.log_retention_days
  tags                = module.naming.tags
}

module "identity" {
  source = "./modules/identity"

  prefix        = module.naming.prefix
  domain_prefix = local.cognito_domain_prefix
  callback_urls = [for path in var.oauth_callback_paths : "${module.frontend.url}${path}"]
  logout_urls   = [for path in var.oauth_logout_paths : "${module.frontend.url}${path}"]
  retain_data   = var.retain_persisted_data
  tags          = module.naming.tags
}

module "runtime_microvm" {
  count  = var.deployment_mode == "microvm" ? 1 : 0
  source = "./modules/runtime-microvm"

  prefix                       = module.naming.prefix
  inbound_auth                 = "jwt"
  runtime_image_uri            = local.runtime_image_uri
  runtime_role_arn             = module.runtime_common.runtime_role_arn
  cognito_issuer               = module.identity.issuer
  cognito_app_client_id        = module.identity.app_client_id
  allowed_scopes               = var.runtime_allowed_scopes
  request_header_allowlist     = var.runtime_request_header_allowlist
  idle_session_timeout_seconds = var.runtime_idle_session_timeout_seconds
  max_lifetime_seconds         = var.runtime_max_lifetime_seconds
  environment_variables = {
    AWS_REGION       = local.region
    BINDING_AUDIENCE = local.binding_audience
    BINDING_KEY_ARN  = module.persistence.binding_key_arn
    COGNITO_ISSUER   = module.identity.issuer
    DEPLOYMENT_MODE  = "microvm"
    # The platform's stdout pipeline has proven unreliable; the runtime
    # ships its own log records directly to this dedicated group.
    KIROCREW_LOG_GROUP     = module.runtime_common.runtime_log_group
    PERSISTENCE_BROKER_ARN = module.persistence.broker_function_arn
  }
  tags = module.naming.tags
}

# A SECOND front door, for callers that have no browser and therefore no Cognito
# user token -- specifically the scheduled-job waker. A runtime supports exactly
# one inbound auth method, so this cannot be a flag on the runtime above; the
# AgentCore security guidance says to create a separate one, and this is that.
#
# Same image, same execution role, same broker, and the SAME sandbox: a sandbox is
# keyed by owner_hash derived from the Cognito subject, not by which runtime
# served the request. So a job woken through here lands in the user's own
# workspace rather than a parallel one.
#
# The idle timeout is deliberately far shorter than the browser runtime's. Memory
# is billed for every second of a session including its idle tail, so at the 900s
# default the tail is roughly 70% of a wake's bill -- while for a human that same
# 900s is what avoids a cold start every time they look away. One runtime cannot
# hold both profiles, and splitting the door is what makes each one right.
module "runtime_microvm_machine" {
  count  = var.deployment_mode == "microvm" && var.enable_machine_runtime ? 1 : 0
  source = "./modules/runtime-microvm"

  prefix       = "${module.naming.prefix}-machine"
  inbound_auth = "iam"
  # Unused for iam inbound auth, but the module's contract still requires them.
  cognito_issuer               = module.identity.issuer
  cognito_app_client_id        = module.identity.app_client_id
  allowed_scopes               = var.runtime_allowed_scopes
  runtime_image_uri            = local.runtime_image_uri
  runtime_role_arn             = module.runtime_common.runtime_role_arn
  request_header_allowlist     = var.runtime_request_header_allowlist
  idle_session_timeout_seconds = var.machine_runtime_idle_session_timeout_seconds
  max_lifetime_seconds         = var.runtime_max_lifetime_seconds
  environment_variables = {
    AWS_REGION       = local.region
    BINDING_AUDIENCE = local.binding_audience
    BINDING_KEY_ARN  = module.persistence.binding_key_arn
    COGNITO_ISSUER   = module.identity.issuer
    DEPLOYMENT_MODE  = "microvm"
    # The image is identical on both runtimes, so this variable is the ONLY
    # thing that tells the adapter which front door it is serving. It is what
    # makes a scheduler token acceptable here and refused on the browser
    # runtime, where a caller could otherwise use one to skip the Cognito
    # subject cross-check.
    INBOUND_AUTH           = "iam"
    KIROCREW_LOG_GROUP     = module.runtime_common.runtime_log_group
    PERSISTENCE_BROKER_ARN = module.persistence.broker_function_arn
  }
  tags = module.naming.tags
}

module "control_api" {
  source = "./modules/control-api"

  prefix                         = module.naming.prefix
  issuer                         = module.identity.issuer
  app_client_id                  = module.identity.app_client_id
  allowed_origin                 = module.frontend.url
  sandbox_table_name             = module.persistence.sandbox_table_name
  sandbox_table_arn              = module.persistence.sandbox_table_arn
  sandbox_table_key_arn          = module.persistence.snapshot_key_arn
  binding_audience               = local.binding_audience
  binding_key_arn                = module.persistence.binding_key_arn
  broker_function_arn            = module.persistence.broker_function_arn
  runtime_arn                    = local.runtime_arn
  runtime_qualifier              = local.runtime_qualifier
  region                         = local.region
  deployment_mode                = var.deployment_mode
  frontend_compatibility_version = var.frontend_compatibility_version
  log_retention_days             = var.log_retention_days
  source_directory               = "${path.module}/functions/control/src"
  auth_source_directory          = "${path.module}/functions/auth/src"
  user_pool_id                   = module.identity.user_pool_id
  user_pool_arn                  = module.identity.user_pool_arn
  allowed_email_domains          = var.allowed_email_domains
  allowed_email_patterns         = var.allowed_email_patterns
  persisted_paths                = var.persisted_paths
  tags                           = module.naming.tags
}

module "scheduler" {
  # Three conditions, all necessary. Without the machine runtime there is no door
  # a scheduler can knock on at all, and without a subject AND a sandbox id the
  # control plane cannot mint a binding -- it stores only a one-way owner hash, so
  # it can verify the pair but never derive it.
  count = (
    var.deployment_mode == "microvm"
    && var.enable_machine_runtime
    && var.enable_scheduled_wake
    && var.wake_cognito_subject != ""
    && var.wake_sandbox_id != ""
  ) ? 1 : 0
  source = "./modules/scheduler"

  prefix                     = module.naming.prefix
  schedule_expression        = var.wake_schedule_expression
  schedule_enabled           = var.wake_schedule_enabled
  schedule_timezone          = var.wake_schedule_timezone
  wake_lead_seconds          = var.wake_lead_seconds
  wake_dwell_seconds         = var.wake_dwell_seconds
  cognito_subject            = var.wake_cognito_subject
  sandbox_id                 = var.wake_sandbox_id
  sandbox_table_name         = module.persistence.sandbox_table_name
  sandbox_table_arn          = module.persistence.sandbox_table_arn
  sandbox_key_arn            = module.persistence.snapshot_key_arn
  control_function_arn       = module.control_api.function_arn
  control_function_name      = module.control_api.function_name
  machine_runtime_arn        = module.runtime_microvm_machine[0].runtime_arn
  machine_endpoint_qualifier = module.runtime_microvm_machine[0].endpoint_qualifier
  wake_path                  = var.wake_path
  log_retention_days         = var.log_retention_days
  source_directory           = "${path.module}/functions/waker/src"
  tags                       = module.naming.tags
}

module "frontend" {
  source = "./modules/frontend"

  prefix          = module.naming.prefix
  global_suffix   = module.naming.global_suffix
  asset_directory = abspath("${path.module}/${var.frontend_asset_directory}")
  bootstrap_path  = abspath("${path.module}/../frontend-shell/dist/bootstrap.bundle.js")
  api_endpoint    = module.control_api.api_endpoint
  cognito_domain  = local.cognito_domain
  region          = local.region
  dns_suffix      = local.dns_suffix
  public_config = {
    auth = {
      basePath       = "/auth/v1"
      allowedDomains = var.allowed_email_domains
    }
    region                       = local.region
    shellOrigin                  = module.frontend.url
    upstreamOrigin               = module.frontend.url
    frontendCompatibilityVersion = var.frontend_compatibility_version
  }
  tags = module.naming.tags
}

check "no_instances_in_shared_stack" {
  assert {
    condition     = var.deployment_mode == "microvm" || var.deployment_mode == "instances"
    error_message = "Only explicit microvm or guarded instances profiles are accepted."
  }
}
