variable "prefix" { type = string }
variable "domain_prefix" { type = string }
variable "callback_urls" { type = list(string) }
variable "logout_urls" { type = list(string) }
variable "retain_data" { type = bool }
variable "tags" { type = map(string) }

resource "aws_cognito_user_pool" "this" {
  name                = "${var.prefix}-users"
  deletion_protection = var.retain_data ? "ACTIVE" : "INACTIVE"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  auto_verified_attributes = ["email"]
  username_attributes      = ["email"]
  mfa_configuration        = "OPTIONAL"

  software_token_mfa_configuration {
    enabled = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    require_uppercase                = true
    temporary_password_validity_days = 7
  }

  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  user_pool_add_ons {
    advanced_security_mode = "ENFORCED"
  }

  tags = var.tags
}

resource "aws_cognito_resource_server" "control" {
  identifier   = "kirocrew.control"
  name         = "KiroCrew sandbox control"
  user_pool_id = aws_cognito_user_pool.this.id

  scope {
    scope_name        = "invoke"
    scope_description = "Invoke the authenticated sandbox control plane"
  }
}

resource "aws_cognito_user_pool_client" "browser" {
  name         = "${var.prefix}-browser"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "profile", "email", "kirocrew.control/invoke"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls
  supported_identity_providers         = ["COGNITO"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  access_token_validity                = 60
  id_token_validity                    = 60
  refresh_token_validity               = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  depends_on = [aws_cognito_resource_server.control]
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_user_group" "administrators" {
  name         = "sandbox-admins"
  description  = "Administrators allowed to perform retained sandbox deletion"
  user_pool_id = aws_cognito_user_pool.this.id
  precedence   = 10
}

output "user_pool_id" { value = aws_cognito_user_pool.this.id }
output "app_client_id" { value = aws_cognito_user_pool_client.browser.id }
output "issuer" { value = "https://${aws_cognito_user_pool.this.endpoint}" }
output "domain" { value = aws_cognito_user_pool_domain.this.domain }
