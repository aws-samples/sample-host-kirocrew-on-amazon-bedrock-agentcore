#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
IMAGE_TAG=${IMAGE_TAG:-kirocrew-agentcore:workspace}
SOURCE_REVISION=${SOURCE_REVISION:-$(git -C "$ROOT" rev-parse --verify HEAD 2>/dev/null || printf uncommitted)}
DEPENDENCY_LOCK_SHA256=$(sha256sum "$ROOT/uv.lock" | awk '{print $1}')
DEPLOYMENT_MODE=${DEPLOYMENT_MODE:-microvm}
PLATFORMS=${PLATFORMS:-linux/amd64,linux/arm64}
BUILDER=${BUILDER:-}
ECR_REPOSITORY_URI=${ECR_REPOSITORY_URI:-}
IMAGE_RELEASE_TAG=${IMAGE_RELEASE_TAG:-0.2.0-${DEPLOYMENT_MODE}}
OUTPUT=${OUTPUT:-$ROOT/runtime/reports/kirocrew-agentcore.oci.tar}
REPORT_DIR=${REPORT_DIR:-$ROOT/runtime/reports}
TRIVY_IMAGE=${TRIVY_IMAGE:-aquasec/trivy:0.63.0@sha256:6fb0646988fcd2fdf7bf123f7174945ebc2c9c72d1fa1567c8d7daeeb70f8037}

case "$DEPLOYMENT_MODE" in
  microvm|instances) ;;
  *) echo "DEPLOYMENT_MODE must be microvm or instances." >&2; exit 2 ;;
esac

builder_args=()
if [[ -n "$BUILDER" ]]; then
  builder_args=(--builder "$BUILDER")
fi

run_trivy() {
  mkdir -p "$REPORT_DIR/.trivy-cache"
  docker run --rm --user "$(id -u):$(id -g)" \
    --group-add "$(stat -c %g /var/run/docker.sock)" \
    --env HOME=/tmp \
    --volume /var/run/docker.sock:/var/run/docker.sock \
    --volume "$REPORT_DIR/.trivy-cache:/cache" \
    --volume "$REPORT_DIR:/reports" \
    "$TRIVY_IMAGE" --cache-dir /cache "$@"
}

build_args=(
  --file "$ROOT/runtime/Dockerfile"
  --build-arg "SOURCE_REVISION=$SOURCE_REVISION"
  --build-arg "DEPENDENCY_LOCK_SHA256=$DEPENDENCY_LOCK_SHA256"
  "$ROOT"
)

case "${1:-}" in
  build)
    docker buildx build --pull --platform "${PLATFORM:-linux/amd64}" --load --tag "$IMAGE_TAG" "${build_args[@]}"
    ;;
  multiarch)
    mkdir -p "$(dirname "$OUTPUT")"
    docker buildx build --pull --platform "$PLATFORMS" \
      --sbom=true --provenance=mode=max \
      --tag "$IMAGE_TAG" --output "type=oci,dest=$OUTPUT" "${build_args[@]}"
    ;;
  publish)
    if [[ -z "$ECR_REPOSITORY_URI" ]]; then
      echo "ECR_REPOSITORY_URI is required for publish." >&2
      exit 2
    fi
    remote_tag="$ECR_REPOSITORY_URI:$IMAGE_RELEASE_TAG"
    docker buildx build "${builder_args[@]}" --pull --platform "$PLATFORMS" \
      --sbom=true --provenance=mode=max --push --tag "$remote_tag" "${build_args[@]}"
    digest=$(docker buildx imagetools inspect "$remote_tag" --format '{{json .Manifest}}' | python -c 'import json,sys; print(json.load(sys.stdin)["digest"])')
    printf '%s@%s\n' "$ECR_REPOSITORY_URI" "$digest"
    ;;
  inspect)
    docker image inspect "$IMAGE_TAG" --format '{{json .Config}}'
    test "$(docker image inspect "$IMAGE_TAG" --format '{{.Config.User}}')" = "10001:10001"
    test "$(docker image inspect "$IMAGE_TAG" --format '{{index .Config.Labels "dev.kirocrew.agentcore.protocol"}}')" = "kirocrew-agentcore.v1"
    ;;
  smoke)
    docker run --rm --read-only \
      --tmpfs /tmp:rw,noexec,nosuid,size=256m \
      --mount type=volume,destination=/mnt/workspace \
      "$IMAGE_TAG" smoke
    ;;
  audit)
    listing=$(mktemp)
    container=$(docker create "$IMAGE_TAG")
    trap 'docker rm -f "$container" >/dev/null 2>&1 || true; rm -f "$listing"' EXIT
    docker export "$container" | tar -tf - >"$listing"
    if grep -E '^(root/(\.aws|\.ssh|\.kiro)(/|$)|home/.+|build/|wheels/|tmp/kiro|mnt/workspace/(home/)?(\.aws|\.ssh)(/|$)|mnt/workspace/home/\.kiro(/|$))' "$listing"; then
      echo "Prohibited local state is present in the runtime image." >&2
      exit 1
    fi
    test "$(docker run --rm --entrypoint /bin/sh "$IMAGE_TAG" -c \
      "awk -F: '\$3 >= 1000 && \$3 != 65534 {print \$1 \":\" \$3 \":\" \$6}' /etc/passwd")" = \
      "app:10001:/mnt/workspace/home"
    ;;
  sbom)
    mkdir -p "$REPORT_DIR"
    run_trivy image --format cyclonedx \
      --output /reports/runtime.cdx.json "$IMAGE_TAG"
    ;;
  vulnerability)
    mkdir -p "$REPORT_DIR"
    run_trivy image --scanners vuln,secret --ignore-unfixed \
      --severity HIGH,CRITICAL --format json \
      --output /reports/runtime-vulnerabilities.json --exit-code 1 "$IMAGE_TAG"
    ;;
  *)
    echo "usage: $0 {build|multiarch|publish|inspect|smoke|audit|sbom|vulnerability}" >&2
    exit 2
    ;;
esac
