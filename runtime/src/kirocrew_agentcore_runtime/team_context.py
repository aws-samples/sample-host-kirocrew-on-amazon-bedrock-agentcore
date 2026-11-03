"""Team context: one Git repository of shared steering and knowledge, mirrored into every sandbox.

Kiro's own model for team steering is a central repository whose files land in
each user's ``~/.kiro/steering`` (https://kiro.dev/docs/steering/). A sandbox has
no MDM or login script to do that, so this module does it: it keeps a read-only
mirror of the configured repository under ``~/.kiro/team-context/repo`` and
installs the repository's ``steering/*.md`` as global steering files named
``team--<name>.md``.

Rules this module keeps:

* Only ``https://`` remotes. Git's ``ext::`` and ``file::`` transports can run
  commands or read the local disk, and a repository URL is configuration an
  operator types, not something to trust blindly.
* It never touches a steering file it did not install. Ownership is tracked in
  a manifest; a pre-existing ``team--*.md`` it does not own is left alone.
* Failure is never fatal. A sandbox with an unreachable repository keeps the
  last installed copy (``~/.kiro`` is a persisted path) and works as before.
* The mirror is a mirror. Local edits to it are discarded on the next sync;
  changes go through a pull request on the source repository, which is where
  review and history come from.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess  # nosec B404
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

TEAM_PREFIX = "team--"
_MANIFEST_NAME = "installed.json"
_MAX_FILES = 200
_MAX_FILE_BYTES = 256 * 1024
_GIT_TIMEOUT_SECONDS = 60.0
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_SUBDIR_PATTERN = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.md$")
DEFAULT_REFRESH_SECONDS = 1800

Runner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class TeamContextConfig:
    repo_url: str
    ref: str = "main"
    subdir: str = ""

    def __post_init__(self) -> None:
        if not self.repo_url.startswith("https://") or any(
            character.isspace() for character in self.repo_url
        ):
            raise ValueError("Team context repository must be an https:// URL.")
        if not _REF_PATTERN.fullmatch(self.ref) or ".." in self.ref:
            raise ValueError("Team context ref is not a valid branch or tag name.")
        if self.subdir and (
            not _SUBDIR_PATTERN.fullmatch(self.subdir) or ".." in self.subdir.split("/")
        ):
            raise ValueError(
                "Team context subdirectory must be a relative path inside the repository."
            )


def config_from_environment(environment: Mapping[str, str]) -> TeamContextConfig | None:
    """Read the team-context settings, or ``None`` when the feature is off or misconfigured."""
    url = environment.get("TEAM_CONTEXT_REPO_URL", "").strip()
    if not url:
        return None
    try:
        return TeamContextConfig(
            repo_url=url,
            ref=environment.get("TEAM_CONTEXT_REF", "").strip() or "main",
            subdir=environment.get("TEAM_CONTEXT_SUBDIR", "").strip().strip("/"),
        )
    except ValueError as error:
        _LOGGER.warning("Team context is disabled: %s", error)
        return None


def refresh_seconds(environment: Mapping[str, str]) -> int:
    raw = environment.get("TEAM_CONTEXT_REFRESH_SECONDS", "").strip()
    if not raw:
        return DEFAULT_REFRESH_SECONDS
    try:
        return max(300, int(raw))
    except ValueError:
        return DEFAULT_REFRESH_SECONDS


@dataclass(slots=True)
class SyncResult:
    fetched: bool = False
    revision: str = ""
    installed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    error: str = ""


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    }
    environment.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    return environment


def _git(
    runner: Runner,
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    allowed_protocol: str = "https",
) -> subprocess.CompletedProcess[str]:
    command = [
        "git",
        "-c",
        "protocol.allow=never",
        "-c",
        f"protocol.{allowed_protocol}.allow=always",
        "-c",
        "core.hooksPath=/dev/null",
        *arguments,
    ]
    return runner(  # nosec B603 - fixed argv, https-only remote, no shell
        command,
        cwd=str(cwd) if cwd is not None else None,
        env=_git_environment(),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=True,
    )


class TeamContextSync:
    """Mirror the team repository and install its steering files under one home directory."""

    def __init__(
        self,
        home: Path,
        config: TeamContextConfig,
        *,
        runner: Runner = subprocess.run,
        allowed_protocol: str = "https",
    ) -> None:
        self._home = home
        self._config = config
        self._runner = runner
        self._protocol = allowed_protocol

    @property
    def root(self) -> Path:
        return self._home / ".kiro" / "team-context"

    @property
    def checkout(self) -> Path:
        return self.root / "repo"

    @property
    def content_root(self) -> Path:
        return self.checkout / self._config.subdir if self._config.subdir else self.checkout

    @property
    def knowledge_directory(self) -> Path:
        return self.content_root / "knowledge"

    @property
    def steering_directory(self) -> Path:
        return self._home / ".kiro" / "steering"

    def sync(self) -> SyncResult:
        result = SyncResult()
        try:
            result.revision = self._fetch()
            result.fetched = True
        except (subprocess.SubprocessError, OSError) as error:
            # Keep whatever was installed last time; say why without echoing
            # git's stderr, which can carry the remote URL.
            result.error = f"fetch failed ({type(error).__name__})"
            _LOGGER.warning(
                "Team context fetch failed (%s); keeping the last installed copy.",
                type(error).__name__,
            )
            return result
        try:
            self._install(result)
        except OSError as error:
            result.error = f"install failed ({type(error).__name__})"
            _LOGGER.warning("Team context install failed (%s).", type(error).__name__)
            return result
        _LOGGER.info(
            "Team context synced revision=%s installed=%d removed=%d skipped=%d",
            result.revision[:12],
            len(result.installed),
            len(result.removed),
            len(result.skipped),
        )
        return result

    def _fetch(self) -> str:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (self.checkout / ".git").is_dir():
            if self.checkout.exists():
                _remove_tree(self.checkout)
            _git(
                self._runner,
                [
                    "clone",
                    "--depth",
                    "1",
                    "--single-branch",
                    "--branch",
                    self._config.ref,
                    "--",
                    self._config.repo_url,
                    str(self.checkout),
                ],
                allowed_protocol=self._protocol,
            )
        else:
            _git(
                self._runner,
                ["remote", "set-url", "origin", self._config.repo_url],
                cwd=self.checkout,
                allowed_protocol=self._protocol,
            )
            _git(
                self._runner,
                ["fetch", "--depth", "1", "origin", self._config.ref],
                cwd=self.checkout,
                allowed_protocol=self._protocol,
            )
            _git(
                self._runner,
                ["reset", "--hard", "FETCH_HEAD"],
                cwd=self.checkout,
                allowed_protocol=self._protocol,
            )
            _git(
                self._runner, ["clean", "-fdx"], cwd=self.checkout, allowed_protocol=self._protocol
            )
        completed = _git(
            self._runner, ["rev-parse", "HEAD"], cwd=self.checkout, allowed_protocol=self._protocol
        )
        return completed.stdout.strip()

    def _install(self, result: SyncResult) -> None:
        source = self.content_root / "steering"
        wanted: dict[str, bytes] = {}
        if source.is_dir() and not source.is_symlink():
            for path in sorted(source.iterdir()):
                if len(wanted) >= _MAX_FILES:
                    result.skipped.append(path.name)
                    continue
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not _NAME_PATTERN.fullmatch(path.name)
                    or path.stat().st_size > _MAX_FILE_BYTES
                ):
                    result.skipped.append(path.name)
                    continue
                wanted[f"{TEAM_PREFIX}{path.name}"] = path.read_bytes()

        self.steering_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        owned = self._read_manifest()
        for name, content in wanted.items():
            target = self.steering_directory / name
            if (target.exists() or target.is_symlink()) and name not in owned:
                # Someone else's file under our prefix: never overwrite it.
                result.skipped.append(name)
                continue
            if target.is_symlink():
                target.unlink()
            if not target.exists() or target.read_bytes() != content:
                temporary = target.with_name(f".{name}.tmp")
                temporary.write_bytes(content)
                temporary.chmod(0o600)
                temporary.replace(target)
            result.installed.append(name)
        for name in sorted(owned - set(result.installed)):
            target = self.steering_directory / name
            if target.is_file() and not target.is_symlink():
                target.unlink()
            result.removed.append(name)
        self._write_manifest(set(result.installed), result.revision)

    def _read_manifest(self) -> set[str]:
        try:
            data = json.loads((self.root / _MANIFEST_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        names = data.get("installed", []) if isinstance(data, dict) else []
        return {
            name
            for name in names
            if isinstance(name, str) and name.startswith(TEAM_PREFIX) and "/" not in name
        }

    def _write_manifest(self, names: set[str], revision: str) -> None:
        path = self.root / _MANIFEST_NAME
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"installed": sorted(names), "revision": revision}, sort_keys=True),
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        temporary.replace(path)


def _remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    for child in path.iterdir():
        _remove_tree(child)
    path.rmdir()
