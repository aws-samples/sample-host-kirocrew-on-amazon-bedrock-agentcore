from __future__ import annotations

import http.client
import os
import platform
from pathlib import Path
from typing import ClassVar

import pytest
from kirocrew_agentcore_persistence.checkpoint import SystemFlusher
from kirocrew_agentcore_runtime.image_runtime import ImageMetadata, smoke_gateway
from kirocrew_agentcore_runtime.supervisor import GatewayReady, KiroCrewSupervisor

DIGEST = "329b4b2e271d1253eb9b2115f19950485181934f2ba7ded1a993edfbf48c6f90"
BASE_DIGEST = "sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"


def environment() -> dict[str, str]:
    return {
        "KIROCREW_VERSION": "0.2.0",
        "KIROCREW_ARTIFACT_SHA256": DIGEST,
        "KIRO_CLI_VERSION": "2.18.1",
        "KIROCREW_AGENTCORE_PROTOCOL": "kirocrew-agentcore.v1",
        "KIROCREW_SOURCE_REVISION": "abc123",
        "KIROCREW_BASE_IMAGE_DIGEST": BASE_DIGEST,
        "KIROCREW_DEPENDENCY_LOCK_SHA256": DIGEST,
    }


def test_image_metadata_validation_and_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    for missing in environment():
        values = environment()
        values.pop(missing)
        with pytest.raises(ValueError, match=missing):
            ImageMetadata.from_environment(values)
    invalid = environment()
    invalid["KIROCREW_ARTIFACT_SHA256"] = "bad"
    with pytest.raises(ValueError, match="KiroCrew artifact"):
        ImageMetadata.from_environment(invalid)
    invalid = environment()
    invalid["KIROCREW_BASE_IMAGE_DIGEST"] = "bad"
    with pytest.raises(ValueError, match="Base image"):
        ImageMetadata.from_environment(invalid)
    invalid = environment()
    invalid["KIROCREW_DEPENDENCY_LOCK_SHA256"] = "bad"
    with pytest.raises(ValueError, match="Dependency lock"):
        ImageMetadata.from_environment(invalid)
    invalid = environment()
    invalid["KIROCREW_AGENTCORE_PROTOCOL"] = "v2"
    with pytest.raises(ValueError, match="protocol"):
        ImageMetadata.from_environment(invalid)

    metadata = ImageMetadata.from_environment(environment())
    monkeypatch.setattr(platform, "machine", lambda: "test-architecture")
    assert metadata.diagnostics() == {
        "architecture": "test-architecture",
        "baseImageDigest": BASE_DIGEST,
        "dependencyLockSha256": DIGEST,
        "kiroCliVersion": "2.18.1",
        "kiroCrewArtifactSha256": DIGEST,
        "kiroCrewVersion": "0.2.0",
        "protocolVersion": "kirocrew-agentcore.v1",
        "sourceRevision": "abc123",
    }
    runtime = metadata.runtime_metadata()
    assert runtime.kirocrew_version == "0.2.0"
    assert runtime.kirocrew_artifact_sha256 == DIGEST


class FakeSupervisor(KiroCrewSupervisor):
    ready_value = True
    port = 5476
    response_value = "nonempty"
    stops = True
    instance: FakeSupervisor | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.terminated = False
        self.paused = False
        self.resumed = False
        FakeSupervisor.instance = self

    @property
    def ready(self) -> bool:
        return self.ready_value and (not self.terminated or not self.stops)

    def start(self, *, timeout_seconds: float = 60.0) -> GatewayReady:
        assert timeout_seconds == 90
        return GatewayReady(1234, self.port, Path("/workspace/home/.kiro/crew"))

    def token(self, *, renewal_window_seconds: float = 300.0) -> str:
        assert renewal_window_seconds == 300.0
        return self.response_value

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        assert self.paused
        self.resumed = True

    def terminate(self, *, grace_seconds: float = 10.0) -> None:
        assert grace_seconds == 10
        self.terminated = True


class FakeHttpResponse:
    status = 200

    def read(self) -> bytes:
        return b'{"healthy":true}'


class FakeHttpConnection:
    requests: ClassVar[list[tuple[str, str, dict[str, str]]]] = []
    closed = False

    def __init__(self, host: str, port: int, *, timeout: int) -> None:
        assert (host, port, timeout) == ("127.0.0.1", 5476, 10)

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.requests.append((method, path, headers))

    def getresponse(self) -> FakeHttpResponse:
        return FakeHttpResponse()

    def close(self) -> None:
        type(self).closed = True


def test_image_smoke_success_and_failure_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = ImageMetadata.from_environment(environment())
    monkeypatch.setattr(os, "geteuid", lambda: 10001)
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(http.client, "HTTPConnection", FakeHttpConnection)
    monkeypatch.setattr(SystemFlusher, "flush", lambda _self, _path: None)
    result = smoke_gateway(metadata, tmp_path / "workspace", supervisor_type=FakeSupervisor)
    assert result["healthy"] is True
    assert result["gatewayPort"] == 5476
    assert result["nonRoot"] is True
    assert result["restoreOutcome"] == "restored"
    assert result["invocationStatus"] == 200
    assert result["checkpointGeneration"] == 2
    assert result["gracefulStop"] is True
    assert FakeHttpConnection.requests == [
        ("GET", "/api/health", {"Authorization": "Bearer nonempty"})
    ]
    assert FakeHttpConnection.closed
    restored = tmp_path / "workspace/.agentcore/image-smoke/restored"
    assert (restored / "projects/default/restore-marker.txt").is_file()
    assert (restored / "projects/default/checkpoint-marker.txt").is_file()
    assert FakeSupervisor.instance is not None
    assert FakeSupervisor.instance.paused
    assert FakeSupervisor.instance.resumed
    assert FakeSupervisor.instance.terminated

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="non-root"):
        smoke_gateway(metadata, tmp_path / "root", supervisor_type=FakeSupervisor)
    monkeypatch.setattr(os, "geteuid", lambda: 10001)

    original_read_text = Path.read_text

    def mismatched_restore_marker(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path.name == "restore-marker.txt" and "restored" in path.parts:
            return "corrupted\n"
        return original_read_text(path, encoding=encoding, errors=errors)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "read_text", mismatched_restore_marker)
        with pytest.raises(RuntimeError, match="did not restore correctly"):
            smoke_gateway(
                metadata,
                tmp_path / "corrupt-restore",
                supervisor_type=FakeSupervisor,
            )
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)
    with pytest.raises(RuntimeError, match="not writable"):
        smoke_gateway(metadata, tmp_path / "read-only", supervisor_type=FakeSupervisor)
    monkeypatch.setattr(os, "access", lambda _path, _mode: True)

    FakeSupervisor.ready_value = False
    with pytest.raises(RuntimeError, match="did not become ready"):
        smoke_gateway(metadata, tmp_path / "unhealthy", supervisor_type=FakeSupervisor)
    assert FakeSupervisor.instance is not None and FakeSupervisor.instance.terminated
    FakeSupervisor.ready_value = True
    FakeSupervisor.port = 5477
    with pytest.raises(RuntimeError, match="did not become ready"):
        smoke_gateway(metadata, tmp_path / "wrong-port", supervisor_type=FakeSupervisor)
    FakeSupervisor.port = 5476
    FakeSupervisor.response_value = ""
    with pytest.raises(RuntimeError, match="token"):
        smoke_gateway(metadata, tmp_path / "no-token", supervisor_type=FakeSupervisor)
    FakeSupervisor.response_value = "nonempty"

    FakeHttpResponse.status = 503
    with pytest.raises(RuntimeError, match="health invocation"):
        smoke_gateway(metadata, tmp_path / "failed-invocation", supervisor_type=FakeSupervisor)
    FakeHttpResponse.status = 200

    FakeSupervisor.stops = False
    with pytest.raises(RuntimeError, match="stop gracefully"):
        smoke_gateway(metadata, tmp_path / "failed-stop", supervisor_type=FakeSupervisor)
    FakeSupervisor.stops = True
