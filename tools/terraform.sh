#!/usr/bin/env bash
set -euo pipefail

readonly TERRAFORM_VERSION="1.12.2"
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if command -v terraform >/dev/null 2>&1 && terraform version -json 2>/dev/null | grep -q "\"terraform_version\":\"${TERRAFORM_VERSION}\""; then
  exec terraform "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
  printf 'Terraform %s or Docker is required.\n' "${TERRAFORM_VERSION}" >&2
  exit 127
fi

exec docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --volume "${ROOT}:/workspace" \
  --workdir /workspace \
  "hashicorp/terraform:${TERRAFORM_VERSION}" "$@"
