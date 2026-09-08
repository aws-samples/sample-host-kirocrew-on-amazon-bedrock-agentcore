from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
INFRA = ROOT / "infrastructure"


def terraform(relative: str) -> str:
    return (INFRA / relative).read_text(encoding="utf-8")


def all_terraform() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in INFRA.rglob("*.tf"))


def test_providers_are_exactly_pinned_and_identifiers_are_derived() -> None:
    versions = terraform("versions.tf")
    assert 'required_version = "= 1.12.2"' in versions
    assert 'version = "= 6.10.0"' in versions
    assert 'version = "= 1.56.0"' in versions
    assert 'version = "= 2.7.1"' in versions
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", all_terraform())
    providers = terraform("providers.tf")
    assert 'data "aws_caller_identity" "current"' in providers
    assert 'data "aws_partition" "current"' in providers
    assert 'data "aws_region" "current"' in providers


def test_frontend_is_private_oac_https_only_and_security_header_protected() -> None:
    frontend = terraform("modules/frontend/main.tf")
    for setting in (
        "block_public_acls       = true",
        "block_public_policy     = true",
        "restrict_public_buckets = true",
        'origin_access_control_origin_type = "s3"',
        'signing_behavior                  = "always"',
        'viewer_protocol_policy     = "redirect-to-https"',
        'minimum_protocol_version       = "TLSv1.2_2021"',
        "content_security_policy",
        "strict_transport_security",
        'path_pattern               = "/control/*"',
    ):
        assert setting in frontend
    assert "values   = [aws_cloudfront_distribution.frontend.arn]" in frontend


def test_control_role_can_use_encrypted_sandbox_table_only_through_dynamodb() -> None:
    control = terraform("modules/control-api/main.tf")
    root = terraform("main.tf")
    assert 'variable "sandbox_table_key_arn" { type = string }' in control
    assert 'sid       = "SandboxStateCryptography"' in control
    assert (
        'actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]'
        in control
    )
    assert "resources = [var.sandbox_table_key_arn]" in control
    assert 'variable = "kms:ViaService"' in control
    assert (
        'values   = ["dynamodb.${var.region}.${data.aws_partition.current.dns_suffix}"]' in control
    )
    assert "sandbox_table_key_arn          = module.persistence.snapshot_key_arn" in root


def test_cognito_is_admin_created_public_pkce_client() -> None:
    identity = terraform("modules/identity/main.tf")
    assert "allow_admin_create_user_only = true" in identity
    assert "generate_secret = false" in identity
    # Sign-in happens only through the gated auth Lambda: the browser holds
    # no flow that could take a password to Cognito directly.
    assert (
        "explicit_auth_flows                  = "
        '["ALLOW_ADMIN_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]'
    ) in identity
    assert '"ALLOW_USER_PASSWORD_AUTH"' not in identity
    assert '"ALLOW_USER_SRP_AUTH"' not in identity
    assert 'allowed_oauth_flows                  = ["code"]' in identity
    assert "callback_urls                        = var.callback_urls" in identity
    assert "logout_urls                          = var.logout_urls" in identity
    assert 'prevent_user_existence_errors        = "ENABLED"' in identity


def test_cognito_password_policy_is_length_plus_a_letter_and_a_number() -> None:
    identity = terraform("modules/identity/main.tf")
    # The rule a person can hold in their head, and the one every rejection
    # message states. Pinned so the policy and those messages cannot drift
    # apart: infrastructure/functions/auth ... lambda_handler._STRENGTH_REQUIREMENT
    # and the sign-in note in frontend-shell/src/shell.ts both spell out this
    # exact requirement.
    assert "minimum_length                   = 8" in identity
    assert "require_lowercase                = true" in identity
    assert "require_numbers                  = true" in identity
    assert "require_symbols                  = false" in identity
    assert "require_uppercase                = false" in identity


def test_gated_auth_lambda_is_least_privilege_and_domain_limited() -> None:
    control_api = terraform("modules/control-api/main.tf")
    # Registration and sign-in routes are unauthenticated by design; the
    # Lambda is the gate. Its Cognito permissions are pinned to the pool.
    assert 'authorization_type = "NONE"' in control_api
    assert '"POST /auth/v1/register",' in control_api
    assert '"POST /auth/v1/confirm",' in control_api
    assert '"POST /auth/v1/resend",' in control_api
    assert '"POST /auth/v1/login",' in control_api
    assert '"POST /auth/v1/refresh",' in control_api
    assert '"POST /auth/v1/forgot",' in control_api
    assert '"POST /auth/v1/reset",' in control_api
    assert '"cognito-idp:AdminCreateUser",' in control_api
    assert '"cognito-idp:AdminGetUser",' in control_api
    assert '"cognito-idp:AdminInitiateAuth",' in control_api
    assert "resources = [var.user_pool_arn]" in control_api
    assert "ALLOWED_EMAIL_DOMAINS" in control_api
    assert "ALLOWED_EMAIL_PATTERNS" in control_api
    # Authenticated control routes require the password-auth scope.
    assert 'authorization_scopes = ["aws.cognito.signin.user.admin"]' in control_api


def test_runtime_role_cannot_access_snapshot_objects() -> None:
    runtime = terraform("modules/runtime-common/main.tf")
    assert 'actions   = ["lambda:InvokeFunction"]' in runtime
    assert 'actions   = ["kms:GetPublicKey", "kms:Verify"]' in runtime
    assert 'actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]' in runtime
    assert 'resources = ["${aws_cloudwatch_log_group.runtime.arn}:*"]' in runtime
    assert not re.search(r'actions\s*=\s*\[[^]]*"s3:', runtime)
    persistence = terraform("modules/persistence/main.tf")
    assert 'resources = ["${aws_s3_bucket.snapshots.arn}/sandboxes/*"]' in persistence
    assert 'actions   = ["s3:ListBucket"]' in persistence
    assert 'variable = "s3:prefix"' in persistence


def test_persisted_data_defaults_to_protected_and_outlives_session_expiry() -> None:
    variables = terraform("variables.tf")
    assert 'variable "retain_persisted_data"' in variables
    assert "default     = true" in variables
    assert "checkpoint_retention_days >= 15" in variables
    persistence = terraform("modules/persistence/main.tf")
    assert "force_destroy = !var.retain_data" in persistence
    assert "deletion_protection_enabled = var.retain_data" in persistence
    assert "enable_key_rotation     = true" in persistence
    assert 'versioning_configuration { status = "Enabled" }' in persistence


def test_outputs_and_browser_config_are_public_metadata_only() -> None:
    outputs = terraform("outputs.tf").lower()
    forbidden = ("access_token", "refresh_token", "client_secret", "private_key", "binding_token")
    assert all(term not in outputs for term in forbidden)
    root = terraform("main.tf")
    assert "window.__KIROCREW_AGENTCORE_CONFIG__" not in root
    frontend = terraform("modules/frontend/main.tf")
    assert "window.__KIROCREW_AGENTCORE_CONFIG__" in frontend
    assert "jsonencode(var.public_config)" in frontend


def test_microvm_runtime_uses_official_agentcore_types_and_container_disk() -> None:
    runtime = terraform("modules/runtime-microvm/main.tf")
    variables = terraform("variables.tf")
    assert '"Authorization",' in variables
    assert 'type_name = "AWS::BedrockAgentCore::Runtime"' in runtime
    assert 'type_name = "AWS::BedrockAgentCore::RuntimeEndpoint"' in runtime
    assert 'Type = "AWS::BedrockAgentCore::Runtime"' in runtime
    assert 'Type = "AWS::BedrockAgentCore::RuntimeEndpoint"' in runtime
    # The workspace lives on the container disk; managed session storage
    # (1GB quota, 14-day retention) must stay out - durability is S3-only.
    assert "FilesystemConfigurations" not in runtime
    assert "SessionStorage" not in runtime
    assert 'NetworkMode = "PUBLIC"' in runtime
    assert 'ProtocolConfiguration = "HTTP"' in runtime
    assert "CustomJWTAuthorizer" in runtime
    assert "RequestHeaderAllowlist" in runtime
    assert "IdleRuntimeSessionTimeout" in runtime
    assert "MaxLifetime" in runtime
    assert 'regex("@sha256:[0-9a-f]{64}$"' in runtime


def test_microvm_profile_has_no_customer_capacity_or_shared_filesystem() -> None:
    runtime = terraform("modules/runtime-microvm/main.tf")
    forbidden = (
        "AWS::BedrockAgentCore::CapacityProvider",
        "aws_instance",
        "aws_launch_template",
        "aws_autoscaling_group",
        "aws_efs_",
        "EfsAccessPoint",
        "S3FilesAccessPoint",
    )
    assert all(value not in runtime for value in forbidden)
    root = terraform("main.tf")
    assert 'count  = var.deployment_mode == "microvm" ? 1 : 0' in root
    assert "module.runtime_microvm[0].runtime_arn" in root


def test_microvm_is_default_and_commands_are_profile_and_state_aware() -> None:
    versions = terraform("versions.tf")
    assert 'default     = "microvm"' in versions
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "image-publish:" in makefile
    assert "infra-deploy:" in makefile
    assert "EXPECTED_AWS_ACCOUNT_ID is required" in makefile
    assert "AWS_PROFILE ?= default" in makefile
    assert "TF_STATE ?= $(CURDIR)/infrastructure/terraform.tfstate" in makefile
    image_script = (ROOT / "tools/image.sh").read_text(encoding="utf-8")
    assert "DEPLOYMENT_MODE=${DEPLOYMENT_MODE:-microvm}" in image_script
    assert "ECR_REPOSITORY_URI is required for publish" in image_script
    assert "docker buildx imagetools inspect" in image_script


def test_lambda_archives_exclude_generated_python_caches() -> None:
    control = terraform("modules/control-api/main.tf")
    persistence = terraform("modules/persistence/main.tf")
    assert 'excludes    = ["kirocrew_agentcore_control/__pycache__"]' in control
    assert 'excludes    = ["kirocrew_agentcore_persistence/__pycache__"]' in persistence


def test_runtime_and_persistence_can_use_encrypted_table_only_through_dynamodb() -> None:
    runtime = terraform("modules/runtime-common/main.tf")
    persistence = terraform("modules/persistence/main.tf")
    root = terraform("main.tf")
    actions = 'actions   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]'
    via_dynamodb = (
        'values   = ["dynamodb.${data.aws_region.current.region}.'
        '${data.aws_partition.current.dns_suffix}"]'
    )
    assert 'variable "sandbox_table_key_arn" { type = string }' in runtime
    assert actions in runtime
    assert "resources = [var.sandbox_table_key_arn]" in runtime
    assert via_dynamodb in runtime
    assert "sandbox_table_key_arn = module.persistence.snapshot_key_arn" in root
    assert actions in persistence
    assert "resources = [aws_kms_key.snapshots.arn]" in persistence
    assert via_dynamodb in persistence


def test_panel_persisted_paths_mirror_the_persistence_policy() -> None:
    from kirocrew_agentcore_persistence.manifest import PersistencePolicy

    variables = terraform("variables.tf")
    # The panel shows exactly what the checkpoint engine persists: every
    # policy root appears in the terraform default, and nothing else does.
    for root in PersistencePolicy._roots:
        assert f'"/mnt/workspace/{root.as_posix()}",' in variables
    declared = re.findall(r'"(/mnt/workspace/[^"]+)",', variables)
    assert len(declared) == len(PersistencePolicy._roots)
