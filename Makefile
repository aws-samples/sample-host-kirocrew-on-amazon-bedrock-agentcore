SHELL := /usr/bin/env bash
.DEFAULT_GOAL := help

UV := uv
NPM := npm
TERRAFORM := ./tools/terraform.sh
IMAGE_TAG ?= kirocrew-agentcore:workspace
DEPLOYMENT_MODE ?= microvm
AWS_PROFILE ?= default
AWS_REGION ?= us-east-2
EXPECTED_AWS_ACCOUNT_ID ?=
TF_STATE ?= $(CURDIR)/infrastructure/terraform.tfstate
BUILDER ?=
ECR_REPOSITORY_URI ?=
IMAGE_RELEASE_TAG ?= 0.2.0-$(DEPLOYMENT_MODE)

.PHONY: help setup lock-check frontend-assets frontend-assets-check generate generated-check format format-check lint typecheck unit contract terraform-validate verify image image-multiarch image-publish image-inspect image-smoke image-audit image-sbom image-vulnerability infra-plan infra-deploy e2e clean

help:
	@printf '%s\n' 'setup frontend-assets generate format lint typecheck unit contract verify image image-multiarch image-publish image-inspect image-smoke image-audit image-sbom image-vulnerability infra-plan infra-deploy e2e clean'

setup:
	$(UV) sync --all-packages --all-groups --frozen
	$(NPM) ci --ignore-scripts
	$(MAKE) --no-print-directory frontend-assets

lock-check:
	$(UV) lock --check
	$(UV) sync --all-packages --all-groups --frozen
	$(NPM) ci --ignore-scripts

frontend-assets:
	$(UV) run python tools/extract_kirocrew_spa.py

frontend-assets-check:
	$(UV) run python tools/extract_kirocrew_spa.py --check

generate: frontend-assets
	$(UV) run python tools/generate_protocol_models.py

generated-check: frontend-assets-check
	$(UV) run python tools/generate_protocol_models.py --check

format: generate
	$(UV) run ruff format .
	$(UV) run ruff check --fix .
	$(NPM) run format
	$(TERRAFORM) -chdir=infrastructure fmt -recursive

format-check:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(NPM) run format:check
	$(TERRAFORM) -chdir=infrastructure fmt -check -recursive

lint:
	$(UV) run ruff check .
	$(UV) run bandit -q -r adapter runtime infrastructure/functions tools/headless-client -x '*/tests/*'
	$(NPM) run lint
	$(UV) run python tools/check-secrets.py .

typecheck:
	$(UV) run mypy
	$(NPM) run typecheck

unit:
	$(UV) run pytest tests/unit
	$(NPM) test

contract: generated-check
	$(NPM) run typecheck
	$(UV) run pytest --no-cov -m contract tests/contract

terraform-validate:
	$(TERRAFORM) -chdir=infrastructure init -backend=false -input=false
	$(TERRAFORM) -chdir=infrastructure validate

verify: lock-check format-check lint typecheck unit contract terraform-validate

image:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh build

image-multiarch:
	DEPLOYMENT_MODE=$(DEPLOYMENT_MODE) IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh multiarch

image-publish:
	@test -n "$(EXPECTED_AWS_ACCOUNT_ID)" || { echo 'EXPECTED_AWS_ACCOUNT_ID is required' >&2; exit 2; }
	@test -n "$(ECR_REPOSITORY_URI)" || { echo 'ECR_REPOSITORY_URI is required' >&2; exit 2; }
	@unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN; \
	export AWS_PROFILE='$(AWS_PROFILE)' AWS_SDK_LOAD_CONFIG=1; \
	test "$$(aws sts get-caller-identity --profile '$(AWS_PROFILE)' --query Account --output text --no-cli-pager)" = '$(EXPECTED_AWS_ACCOUNT_ID)'; \
	registry=$${ECR_REPOSITORY_URI%%/*}; \
	aws ecr get-login-password --profile '$(AWS_PROFILE)' --region '$(AWS_REGION)' | docker login --username AWS --password-stdin "$$registry" >/dev/null; \
	DEPLOYMENT_MODE='$(DEPLOYMENT_MODE)' BUILDER='$(BUILDER)' ECR_REPOSITORY_URI='$(ECR_REPOSITORY_URI)' IMAGE_RELEASE_TAG='$(IMAGE_RELEASE_TAG)' ./tools/image.sh publish

image-inspect:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh inspect

image-smoke:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh smoke

image-audit:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh audit

image-sbom:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh sbom

image-vulnerability:
	IMAGE_TAG=$(IMAGE_TAG) ./tools/image.sh vulnerability

infra-plan:
	@test -n "$(EXPECTED_AWS_ACCOUNT_ID)" || { echo 'EXPECTED_AWS_ACCOUNT_ID is required' >&2; exit 2; }
	@unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN; \
	export AWS_PROFILE='$(AWS_PROFILE)' AWS_SDK_LOAD_CONFIG=1; \
	test "$$(aws sts get-caller-identity --profile '$(AWS_PROFILE)' --query Account --output text --no-cli-pager)" = '$(EXPECTED_AWS_ACCOUNT_ID)'; \
	terraform -chdir=infrastructure init -backend=false -input=false; \
	terraform -chdir=infrastructure plan -input=false -lock=false -state='$(TF_STATE)' -var='aws_region=$(AWS_REGION)' -var='deployment_mode=$(DEPLOYMENT_MODE)'

infra-deploy:
	@test -n "$(EXPECTED_AWS_ACCOUNT_ID)" || { echo 'EXPECTED_AWS_ACCOUNT_ID is required' >&2; exit 2; }
	@test -s "$(TF_STATE)" || echo "note: $(TF_STATE) does not exist yet; Terraform will create it (first deployment)" >&2
	@unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN; \
	export AWS_PROFILE='$(AWS_PROFILE)' AWS_SDK_LOAD_CONFIG=1; \
	test "$$(aws sts get-caller-identity --profile '$(AWS_PROFILE)' --query Account --output text --no-cli-pager)" = '$(EXPECTED_AWS_ACCOUNT_ID)'; \
	terraform -chdir=infrastructure init -backend=false -input=false; \
	terraform -chdir=infrastructure apply -input=false -auto-approve -state='$(TF_STATE)' -var='aws_region=$(AWS_REGION)' -var='deployment_mode=$(DEPLOYMENT_MODE)'

e2e:
	DEPLOYMENT_MODE=$(DEPLOYMENT_MODE) $(UV) run pytest --no-cov -m e2e tests/e2e

clean:
	rm -rf .coverage .mypy_cache .pytest_cache .ruff_cache .venv frontend-shell/dist frontend-shell/upstream node_modules infrastructure/.terraform
