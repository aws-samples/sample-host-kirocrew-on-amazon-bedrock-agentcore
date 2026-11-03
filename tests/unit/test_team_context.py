from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any, ClassVar

import pytest
from kirocrew_agentcore_adapter.transport import BindingClaims
from kirocrew_agentcore_runtime import aws_runtime, team_context
from kirocrew_agentcore_runtime import supervisor as supervisor_module
from kirocrew_agentcore_runtime.team_context import (
    DEFAULT_REFRESH_SECONDS,
    TEAM_PREFIX,
    TeamContextConfig,
    TeamContextSync,
    config_from_environment,
    refresh_seconds,
)
from test_aws_runtime import SANDBOX, StubAwsSessionInitializer

_REMOTE = "https://git.example.test/team/context.git"


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    (repo / "team" / "steering").mkdir(parents=True)
    (repo / "team" / "knowledge").mkdir()
    (repo / "team" / "steering" / "coding.md").write_text("# Coding\nUse integers for money.\n")
    (repo / "team" / "steering" / "review.md").write_text("# Review\nTwo approvals.\n")
    (repo / "team" / "knowledge" / "runbook.md").write_text("# Runbook\n")
    _run("init", "-q", "-b", "main", cwd=repo)
    _run("-c", "user.email=t@example.test", "-c", "user.name=t", "add", ".", cwd=repo)
    _run(
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        "init",
        cwd=repo,
    )
    return repo


def _commit(repo: Path, message: str) -> None:
    _run("-c", "user.email=t@example.test", "-c", "user.name=t", "add", "-A", ".", cwd=repo)
    _run(
        "-c",
        "user.email=t@example.test",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "-m",
        message,
        cwd=repo,
    )


def _runner_for(upstream: Path) -> Any:
    """Run real git, pointing the configured https remote at the local upstream."""

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        rewritten = [f"file://{upstream}" if part == _REMOTE else part for part in command]
        return subprocess.run(rewritten, **kwargs)

    return runner


def _sync(home: Path, upstream: Path, **config: str) -> TeamContextSync:
    return TeamContextSync(
        home,
        TeamContextConfig(repo_url=_REMOTE, **config),
        runner=_runner_for(upstream),
        allowed_protocol="file",
    )


def test_config_is_off_without_a_repository() -> None:
    assert config_from_environment({}) is None
    assert config_from_environment({"TEAM_CONTEXT_REPO_URL": "  "}) is None


@pytest.mark.parametrize(
    "environment",
    [
        {"TEAM_CONTEXT_REPO_URL": "ext::sh -c touch% /tmp/pwned"},
        {"TEAM_CONTEXT_REPO_URL": "file:///etc"},
        {"TEAM_CONTEXT_REPO_URL": "git@github.com:org/repo.git"},
        {"TEAM_CONTEXT_REPO_URL": "https://x.test/r.git", "TEAM_CONTEXT_REF": "--upload-pack=sh"},
        {"TEAM_CONTEXT_REPO_URL": "https://x.test/r.git", "TEAM_CONTEXT_REF": "a/../b"},
        {"TEAM_CONTEXT_REPO_URL": "https://x.test/r.git", "TEAM_CONTEXT_SUBDIR": "../../etc"},
        {"TEAM_CONTEXT_REPO_URL": "https://x.test/r .git"},
    ],
)
def test_unsafe_configuration_disables_the_feature(environment: dict[str, str]) -> None:
    assert config_from_environment(environment) is None


def test_config_reads_ref_and_subdir() -> None:
    config = config_from_environment(
        {
            "TEAM_CONTEXT_REPO_URL": "https://x.test/r.git",
            "TEAM_CONTEXT_REF": "release/v1",
            "TEAM_CONTEXT_SUBDIR": "/examples/team-context/",
        }
    )
    assert config == TeamContextConfig(
        "https://x.test/r.git", "release/v1", "examples/team-context"
    )


def test_refresh_interval_has_a_floor_and_a_default() -> None:
    assert refresh_seconds({}) == DEFAULT_REFRESH_SECONDS
    assert refresh_seconds({"TEAM_CONTEXT_REFRESH_SECONDS": "60"}) == 300
    assert refresh_seconds({"TEAM_CONTEXT_REFRESH_SECONDS": "900"}) == 900
    assert refresh_seconds({"TEAM_CONTEXT_REFRESH_SECONDS": "soon"}) == DEFAULT_REFRESH_SECONDS


def test_sync_installs_team_steering_as_global_steering(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    result = sync.sync()

    assert result.fetched and not result.error and len(result.revision) == 40
    assert sorted(result.installed) == [f"{TEAM_PREFIX}coding.md", f"{TEAM_PREFIX}review.md"]
    installed = home / ".kiro" / "steering" / f"{TEAM_PREFIX}coding.md"
    assert installed.read_text() == "# Coding\nUse integers for money.\n"
    assert installed.stat().st_mode & 0o777 == 0o600
    assert (sync.knowledge_directory / "runbook.md").is_file()


def test_resync_follows_upstream_and_discards_local_mirror_edits(
    tmp_path: Path, upstream: Path
) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    sync.sync()
    (sync.checkout / "team" / "steering" / "coding.md").write_text("tampered locally\n")
    (upstream / "team" / "steering" / "coding.md").write_text("# Coding v2\n")
    (upstream / "team" / "steering" / "review.md").unlink()
    _commit(upstream, "v2")

    result = sync.sync()

    steering = home / ".kiro" / "steering"
    assert (steering / f"{TEAM_PREFIX}coding.md").read_text() == "# Coding v2\n"
    assert not (steering / f"{TEAM_PREFIX}review.md").exists()
    assert result.removed == [f"{TEAM_PREFIX}review.md"]


def test_sync_never_touches_files_it_does_not_own(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    steering = home / ".kiro" / "steering"
    steering.mkdir(parents=True)
    (steering / "personal.md").write_text("mine\n")
    (steering / f"{TEAM_PREFIX}coding.md").write_text("hand made\n")

    result = _sync(home, upstream, subdir="team").sync()

    assert (steering / "personal.md").read_text() == "mine\n"
    assert (steering / f"{TEAM_PREFIX}coding.md").read_text() == "hand made\n"
    assert f"{TEAM_PREFIX}coding.md" in result.skipped
    assert result.installed == [f"{TEAM_PREFIX}review.md"]


def test_sync_skips_symlinks_odd_names_and_oversized_files(tmp_path: Path, upstream: Path) -> None:
    source = upstream / "team" / "steering"
    (source / "escape.md").symlink_to("/etc/hosts")
    (source / "notes.txt").write_text("not markdown\n")
    (source / "big.md").write_bytes(b"x" * (256 * 1024 + 1))
    _commit(upstream, "odd files")

    result = _sync(tmp_path / "home", upstream, subdir="team").sync()

    assert sorted(result.installed) == [f"{TEAM_PREFIX}coding.md", f"{TEAM_PREFIX}review.md"]
    assert {"escape.md", "notes.txt", "big.md"} <= set(result.skipped)


def test_unreachable_repository_keeps_the_last_installed_copy(
    tmp_path: Path, upstream: Path
) -> None:
    home = tmp_path / "home"
    _sync(home, upstream, subdir="team").sync()

    def failing(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(128, command, stderr="fatal: https://secret@host")

    result = TeamContextSync(
        home, TeamContextConfig(repo_url=_REMOTE, subdir="team"), runner=failing
    ).sync()

    assert not result.fetched
    assert result.error == "fetch failed (CalledProcessError)"
    assert "secret" not in result.error
    assert (home / ".kiro" / "steering" / f"{TEAM_PREFIX}coding.md").is_file()


def test_repository_without_steering_installs_nothing(tmp_path: Path, upstream: Path) -> None:
    result = _sync(tmp_path / "home", upstream).sync()
    assert result.fetched and result.installed == [] and not result.error


class _RecordingSync:
    calls: ClassVar[list[tuple[Path, TeamContextConfig]]] = []

    def __init__(self, home: Path, config: TeamContextConfig) -> None:
        self._entry = (home, config)

    def sync(self) -> str:
        _RecordingSync.calls.append(self._entry)
        return "synced"


def test_supervisor_syncs_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    supervisor = object.__new__(supervisor_module.KiroCrewSupervisor)
    supervisor._layout = supervisor_module.WorkspaceLayout(tmp_path)
    monkeypatch.setattr(supervisor_module, "TeamContextSync", _RecordingSync)
    _RecordingSync.calls = []

    monkeypatch.delenv("TEAM_CONTEXT_REPO_URL", raising=False)
    assert supervisor.sync_team_context() is None
    assert _RecordingSync.calls == []

    monkeypatch.setenv("TEAM_CONTEXT_REPO_URL", "https://x.test/r.git")
    assert supervisor.sync_team_context() == "synced"  # type: ignore[comparison-overlap]
    assert _RecordingSync.calls == [(tmp_path / "home", TeamContextConfig("https://x.test/r.git"))]


def test_supervisor_swallows_unexpected_sync_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Exploding:
        def __init__(self, *_args: object) -> None:
            raise RuntimeError("boom")

    supervisor = object.__new__(supervisor_module.KiroCrewSupervisor)
    supervisor._layout = supervisor_module.WorkspaceLayout(tmp_path)
    monkeypatch.setattr(supervisor_module, "TeamContextSync", Exploding)
    monkeypatch.setenv("TEAM_CONTEXT_REPO_URL", "https://x.test/r.git")
    assert supervisor.sync_team_context() is None


def test_unchanged_resync_rewrites_nothing(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    sync.sync()
    installed = home / ".kiro" / "steering" / f"{TEAM_PREFIX}coding.md"
    before = installed.stat().st_mtime_ns
    result = sync.sync()
    assert installed.stat().st_mtime_ns == before
    assert result.removed == []


def test_owned_file_replaced_by_a_symlink_is_restored(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    sync.sync()
    installed = home / ".kiro" / "steering" / f"{TEAM_PREFIX}coding.md"
    installed.unlink()
    installed.symlink_to("/etc/hosts")
    sync.sync()
    assert not installed.is_symlink()
    assert installed.read_text() == "# Coding\nUse integers for money.\n"


def test_removal_tolerates_a_file_the_user_already_deleted(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    sync.sync()
    (home / ".kiro" / "steering" / f"{TEAM_PREFIX}review.md").unlink()
    (upstream / "team" / "steering" / "review.md").unlink()
    _commit(upstream, "drop review")
    assert sync.sync().removed == [f"{TEAM_PREFIX}review.md"]


def test_a_broken_checkout_is_replaced(tmp_path: Path, upstream: Path) -> None:
    sync = _sync(tmp_path / "home", upstream, subdir="team")
    (sync.checkout / "nested").mkdir(parents=True)
    (sync.checkout / "nested" / "leftover.txt").write_text("x")
    (sync.checkout / "stray.txt").write_text("x")
    result = sync.sync()
    assert result.fetched and (sync.checkout / ".git").is_dir()
    assert not (sync.checkout / "stray.txt").exists()


def test_a_checkout_path_that_is_a_file_is_replaced(tmp_path: Path, upstream: Path) -> None:
    sync = _sync(tmp_path / "home", upstream, subdir="team")
    sync.root.mkdir(parents=True)
    sync.checkout.write_text("not a directory")
    assert sync.sync().fetched


def test_file_count_is_capped(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(team_context, "_MAX_FILES", 1)
    result = _sync(tmp_path / "home", upstream, subdir="team").sync()
    assert result.installed == [f"{TEAM_PREFIX}coding.md"]
    assert "review.md" in result.skipped


def test_install_failure_is_reported_not_raised(
    tmp_path: Path, upstream: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_self: TeamContextSync, _result: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(TeamContextSync, "_install", broken)
    result = _sync(tmp_path / "home", upstream, subdir="team").sync()
    assert result.fetched and result.error == "install failed (PermissionError)"


def test_corrupt_manifest_is_treated_as_empty(tmp_path: Path, upstream: Path) -> None:
    home = tmp_path / "home"
    sync = _sync(home, upstream, subdir="team")
    sync.sync()
    (sync.root / "installed.json").write_text("[not json")
    result = sync.sync()
    # Without ownership records the existing files count as someone else's.
    assert result.installed == []
    assert (home / ".kiro" / "steering" / f"{TEAM_PREFIX}coding.md").is_file()


class _StopLoopError(Exception):
    pass


def test_runtime_refresh_loop_resyncs_on_its_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    synced: list[bool] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) > 1:
            raise _StopLoopError

    class _Supervisor:
        def sync_team_context(self) -> None:
            synced.append(True)

    initializer = object.__new__(aws_runtime.AwsSessionInitializer)
    initializer._supervisor = _Supervisor()  # type: ignore[assignment]
    monkeypatch.setenv("TEAM_CONTEXT_REFRESH_SECONDS", "900")
    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(_StopLoopError):
        asyncio.run(initializer._team_context_refresh())
    assert delays == [900, 900]
    assert synced == [True]


def test_initializer_starts_the_refresher_only_with_a_team_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario(url: str | None) -> object:
        if url is None:
            monkeypatch.delenv("TEAM_CONTEXT_REPO_URL", raising=False)
        else:
            monkeypatch.setenv("TEAM_CONTEXT_REPO_URL", url)
        initializer = StubAwsSessionInitializer()
        claims = BindingClaims(SANDBOX, "session-1", 2_000_000_000)
        await initializer.initialize("subject", "binding-1", claims)
        task = initializer._team_context_task
        for background in (task, initializer._lease_heartbeat_task, initializer._tool_token_task):
            if background is not None:
                background.cancel()
        return task

    assert asyncio.run(scenario("https://x.test/r.git")) is not None
    assert asyncio.run(scenario(None)) is None
