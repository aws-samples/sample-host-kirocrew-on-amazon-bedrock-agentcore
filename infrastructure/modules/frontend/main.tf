variable "prefix" { type = string }
variable "global_suffix" { type = string }
variable "asset_directory" { type = string }
variable "bootstrap_path" { type = string }
variable "api_endpoint" { type = string }
variable "cognito_domain" { type = string }
variable "region" { type = string }
variable "dns_suffix" { type = string }
variable "public_config" { type = any }
variable "tags" { type = map(string) }

locals {
  asset_files = fileset(var.asset_directory, "**")
  content_types = {
    css   = "text/css; charset=utf-8"
    gif   = "image/gif"
    html  = "text/html; charset=utf-8"
    ico   = "image/x-icon"
    jpeg  = "image/jpeg"
    jpg   = "image/jpeg"
    js    = "text/javascript; charset=utf-8"
    json  = "application/json; charset=utf-8"
    mjs   = "text/javascript; charset=utf-8"
    mp3   = "audio/mpeg"
    ogg   = "audio/ogg"
    png   = "image/png"
    svg   = "image/svg+xml"
    wasm  = "application/wasm"
    webp  = "image/webp"
    woff  = "font/woff"
    woff2 = "font/woff2"
  }
  short_cache_files = toset(["index.html", "manifest.json", "sw.js", "asset-manifest.json"])
  api_domain        = trimprefix(var.api_endpoint, "https://")
  bootstrap_content = "window.__KIROCREW_AGENTCORE_CONFIG__=${jsonencode(var.public_config)};\n${file(var.bootstrap_path)}"
  csp = join("; ", [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self' ${var.cognito_domain}",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "connect-src 'self' ${var.cognito_domain} https://bedrock-agentcore.${var.region}.${var.dns_suffix} wss://bedrock-agentcore.${var.region}.${var.dns_suffix} https://s3.${var.region}.${var.dns_suffix}",
    "upgrade-insecure-requests",
  ])
}

resource "aws_s3_bucket" "frontend" {
  bucket        = "${var.prefix}-frontend-${var.global_suffix}"
  force_destroy = true
  tags          = var.tags
}

resource "aws_s3_bucket_public_access_block" "frontend" {
  bucket                  = aws_s3_bucket.frontend.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_cloudfront_origin_access_control" "frontend" {
  name                              = "${var.prefix}-frontend"
  description                       = "Private KiroCrew frontend access"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_response_headers_policy" "security" {
  name = "${var.prefix}-security"

  security_headers_config {
    content_security_policy {
      content_security_policy = local.csp
      override                = true
    }
    content_type_options { override = true }
    frame_options {
      frame_option = "DENY"
      override     = true
    }
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    strict_transport_security {
      access_control_max_age_sec = 63072000
      include_subdomains         = true
      preload                    = true
      override                   = true
    }
    xss_protection {
      protection = true
      mode_block = true
      override   = true
    }
  }

  custom_headers_config {
    items {
      header   = "Permissions-Policy"
      value    = "camera=(), geolocation=(), microphone=()"
      override = true
    }
    items {
      header   = "Cross-Origin-Opener-Policy"
      value    = "same-origin"
      override = true
    }
    items {
      header   = "Cross-Origin-Resource-Policy"
      value    = "same-origin"
      override = true
    }
  }
}

resource "aws_cloudfront_cache_policy" "entry" {
  name        = "${var.prefix}-entry"
  default_ttl = 0
  max_ttl     = 60
  min_ttl     = 0
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = true
    enable_accept_encoding_gzip   = true
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

resource "aws_cloudfront_cache_policy" "control" {
  name        = "${var.prefix}-control-disabled"
  default_ttl = 0
  max_ttl     = 0
  min_ttl     = 0
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_brotli = false
    enable_accept_encoding_gzip   = false
    cookies_config { cookie_behavior = "none" }
    headers_config { header_behavior = "none" }
    query_strings_config { query_string_behavior = "none" }
  }
}

resource "aws_cloudfront_origin_request_policy" "control" {
  name = "${var.prefix}-control"
  cookies_config { cookie_behavior = "none" }
  query_strings_config { query_string_behavior = "all" }
  headers_config {
    header_behavior = "whitelist"
    headers {
      items = ["Authorization", "Content-Type", "Idempotency-Key", "Origin", "X-Correlation-Id"]
    }
  }
}

resource "aws_cloudfront_distribution" "frontend" {
  enabled             = true
  is_ipv6_enabled     = true
  default_root_object = "index.html"
  price_class         = "PriceClass_100"
  http_version        = "http2and3"
  wait_for_deployment = true
  tags                = var.tags

  origin {
    domain_name              = aws_s3_bucket.frontend.bucket_regional_domain_name
    origin_id                = "frontend-s3"
    origin_access_control_id = aws_cloudfront_origin_access_control.frontend.id
  }

  origin {
    domain_name = local.api_domain
    origin_id   = "control-api"
    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    target_origin_id           = "frontend-s3"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS"]
    cached_methods             = ["GET", "HEAD", "OPTIONS"]
    compress                   = true
    cache_policy_id            = aws_cloudfront_cache_policy.entry.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  ordered_cache_behavior {
    path_pattern               = "/control/*"
    target_origin_id           = "control-api"
    viewer_protocol_policy     = "https-only"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD", "OPTIONS"]
    compress                   = false
    cache_policy_id            = aws_cloudfront_cache_policy.control.id
    origin_request_policy_id   = aws_cloudfront_origin_request_policy.control.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
  }

  custom_error_response {
    error_code            = 403
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }
  custom_error_response {
    error_code            = 404
    response_code         = 200
    response_page_path    = "/index.html"
    error_caching_min_ttl = 0
  }

  restrictions {
    geo_restriction { restriction_type = "none" }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1.2_2021"
  }

  lifecycle {
    # CloudFront normalizes this field for its managed default certificate.
    ignore_changes = [viewer_certificate[0].minimum_protocol_version]
  }
}

data "aws_iam_policy_document" "frontend" {
  statement {
    sid       = "AllowCloudFrontReadOnly"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.frontend.arn]
    }
  }
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.frontend.arn, "${aws_s3_bucket.frontend.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "frontend" {
  bucket = aws_s3_bucket.frontend.id
  policy = data.aws_iam_policy_document.frontend.json
}

resource "aws_s3_object" "assets" {
  for_each      = local.asset_files
  bucket        = aws_s3_bucket.frontend.id
  key           = each.value
  source        = "${var.asset_directory}/${each.value}"
  source_hash   = filemd5("${var.asset_directory}/${each.value}")
  content_type  = lookup(local.content_types, lower(element(reverse(split(".", each.value)), 0)), "application/octet-stream")
  cache_control = contains(local.short_cache_files, each.value) ? "public,max-age=0,must-revalidate" : "public,max-age=31536000,immutable"
}

resource "aws_s3_object" "bootstrap" {
  bucket        = aws_s3_bucket.frontend.id
  key           = "bootstrap.js"
  content_type  = "text/javascript; charset=utf-8"
  cache_control = "public,max-age=0,must-revalidate"
  content       = local.bootstrap_content
  etag          = md5(local.bootstrap_content)
}

output "bucket_name" { value = aws_s3_bucket.frontend.id }
output "distribution_id" { value = aws_cloudfront_distribution.frontend.id }
output "distribution_arn" { value = aws_cloudfront_distribution.frontend.arn }
output "domain_name" { value = aws_cloudfront_distribution.frontend.domain_name }
output "url" { value = "https://${aws_cloudfront_distribution.frontend.domain_name}" }
