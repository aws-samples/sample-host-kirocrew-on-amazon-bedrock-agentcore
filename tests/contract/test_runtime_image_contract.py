from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.contract
def test_runtime_image_contract_is_pinned_multiarch_nonroot_and_secret_free() -> None:
    dockerfile = (ROOT / "runtime" / "Dockerfile").read_text()
    artifact = json.loads((ROOT / "runtime" / "kiro-cli-artifact.json").read_text())
    ignore = (ROOT / ".dockerignore").read_text().splitlines()

    assert artifact["version"] == "2.18.1"
    assert set(artifact["artifacts"]) == {"amd64", "arm64"}
    assert all(len(value["sha256"]) == 64 for value in artifact["artifacts"].values())
    assert (
        dockerfile.count(
            "python:3.12.11-slim-bookworm@sha256:"
            "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
        )
        == 3
    )
    lock_digest = hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()
    assert f"ARG DEPENDENCY_LOCK_SHA256={lock_digest}" in dockerfile
    assert "dev.kirocrew.agentcore.dependency-lock.sha256" in dockerfile
    assert "KIROCREW_DEPENDENCY_LOCK_SHA256" in dockerfile
    assert "python -m pip install --no-index --no-deps /wheels/*.whl" in dockerfile
    assert "apt-get upgrade --yes --no-install-recommends" in dockerfile
    assert "tini=0.19.0-1+b3" in dockerfile
    assert "ghcr.io/astral-sh/uv:0.12.3@sha256:" in dockerfile
    assert 'echo "${DEPENDENCY_LOCK_SHA256}  uv.lock" | sha256sum --check' in dockerfile
    assert "uv export --frozen --no-dev --no-emit-workspace" in dockerfile
    assert "python -m pip wheel --require-hashes" in dockerfile
    assert 'case "$TARGETARCH"' in dockerfile
    assert "KIRO_CLI_AMD64_SHA256=" + artifact["artifacts"]["amd64"]["sha256"] in dockerfile
    assert "KIRO_CLI_ARM64_SHA256=" + artifact["artifacts"]["arm64"]["sha256"] in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "mkdir -p /opt/app /mnt/workspace/home /mnt/workspace/projects/default" in dockerfile
    assert "chown -R 10001:10001 /mnt/workspace" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini"' in dockerfile
    assert "KIROCREW_HOST=127.0.0.1" in dockerfile
    assert "EXPOSE 8080" in dockerfile
    assert 'VOLUME ["/mnt/workspace"]' in dockerfile
    assert "KIROCREW_ARTIFACT_SHA256" in dockerfile
    assert "KIROCREW_BASE_IMAGE_DIGEST" in dockerfile
    assert "COPY ." not in dockerfile

    for prohibited in ("**/.aws", "**/.ssh", "**/.kiro", ".venv", "**/.env*"):
        assert prohibited in ignore


@pytest.mark.contract
def test_image_workflow_exposes_build_smoke_sbom_and_multiarch_commands() -> None:
    script = ROOT / "tools" / "image.sh"
    makefile = (ROOT / "Makefile").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert script.stat().st_mode & 0o111
    script_text = script.read_text()
    assert "--read-only" in script_text
    assert "--sbom=true" in script_text
    assert "linux/amd64,linux/arm64" in script_text
    assert "aquasec/trivy:0.63.0@sha256:" in script_text
    assert "vulnerability)" in script_text
    assert "audit)" in script_text
    for target in (
        "image:",
        "image-multiarch:",
        "image-inspect:",
        "image-smoke:",
        "image-audit:",
        "image-sbom:",
        "image-vulnerability:",
    ):
        assert target in makefile
    # Actions are pinned to commit SHAs with the tag kept as a trailing comment,
    # so assert on the action and its tag independently of the digest.
    assert "docker/setup-qemu-action@" in workflow
    assert "# v3.6.0" in workflow
    for line in workflow.splitlines():
        if line.strip().startswith("- uses:"):
            reference = line.split("uses:", 1)[1].split("#", 1)[0].strip()
            digest = reference.split("@", 1)[1]
            assert len(digest) == 40 and all(c in "0123456789abcdef" for c in digest), (
                f"Action {reference} must be pinned to a 40-character commit SHA."
            )
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "sbom: true" in workflow
