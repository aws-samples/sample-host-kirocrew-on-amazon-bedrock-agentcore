from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess  # nosec B404
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import IO, Final, Protocol, cast
from urllib.parse import urlsplit

_URL_PATTERN: Final = re.compile(r"https://[^\s<>]+")
_CODE_PATTERN: Final = re.compile(
    r"(?i)(?:code|user[_ -]?code)\s*(?:is|:|=)?\s*([A-Z0-9]{4,}(?:-[A-Z0-9]{4,})+)"
)
_EXPIRY_PATTERN: Final = re.compile(
    r"(?i)(?:expires?|valid)\s+(?:in|after)\s+(\d{1,4})\s*(seconds?|minutes?)"
)
_BEARER_PATTERN: Final = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")
_SECRET_ASSIGNMENT_PATTERN: Final = re.compile(
    r"(?i)\b(KIRO_API_KEY|access[_ -]?token|refresh[_ -]?token)\s*[:=]\s*[^\s]+"
)


class KiroAuthState(StrEnum):
    AUTHENTICATED = "authenticated"
    REQUIRED = "required"
    EXPIRED = "expired"
    FAILED = "failed"


class KiroIdentityError(RuntimeError):
    """Kiro identity could not be established without exposing credential details."""


# `kiro-cli whoami --format json` prints one JSON object, then appends the
# profile block as plain text ("Profile:", the profile name, and for an
# Identity Center identity its codewhisperer ARN). Both halves are read.
_WHOAMI_FIELDS: Final = (
    ("accountType", "account_type"),
    ("email", "email"),
    ("region", "region"),
    ("startUrl", "start_url"),
)
_IDENTITY_VALUE_LIMIT: Final = 200


def _identity_value(value: object) -> str | None:
    """Accept one short, printable single-line string; reject anything else.

    The result is rendered in the browser panel, so a control character or an
    unbounded blob from a future CLI release must not travel there.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > _IDENTITY_VALUE_LIMIT:
        return None
    if any(character < " " or character == "\x7f" for character in text):
        return None
    return text


@dataclass(frozen=True, slots=True)
class KiroIdentity:
    """Who the sandbox's Kiro CLI is signed in as, as the CLI itself reports it."""

    account_type: str | None = None
    email: str | None = None
    region: str | None = None
    start_url: str | None = None
    profile_name: str | None = None
    profile_arn: str | None = None

    def payload(self) -> dict[str, object]:
        values = {
            "accountType": self.account_type,
            "email": self.email,
            "profileArn": self.profile_arn,
            "profileName": self.profile_name,
            "region": self.region,
            "startUrl": self.start_url,
        }
        return {key: value for key, value in values.items() if value is not None}

    @property
    def empty(self) -> bool:
        return not self.payload()

    @classmethod
    def parse(cls, output: str) -> KiroIdentity:
        document: dict[str, object] = {}
        match = re.search(r"\{.*?\}", output, re.DOTALL)
        if match is not None:
            try:
                parsed = cast(object, json.loads(match.group(0)))
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                document = cast(dict[str, object], parsed)
        fields = {
            attribute: _identity_value(document.get(key)) for key, attribute in _WHOAMI_FIELDS
        }
        name, arn = _profile_block(output)
        return cls(profile_name=name, profile_arn=arn, **fields)


def _profile_block(output: str) -> tuple[str | None, str | None]:
    """Read the profile name and ARN the CLI appends after the JSON document."""
    name: str | None = None
    arn: str | None = None
    seen_header = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith("profile:"):
            seen_header = True
            remainder = line[len("profile:") :].strip()
            if remainder:
                name = name or _identity_value(remainder)
            continue
        if line.startswith("arn:"):
            arn = arn or _identity_value(line)
            continue
        if seen_header and name is None and not line.startswith("{"):
            name = _identity_value(line)
    return name, arn


@dataclass(frozen=True, slots=True)
class KiroAuthStatus:
    state: KiroAuthState
    interactive_supported: bool
    identity: KiroIdentity | None = None

    def payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "interactiveSupported": self.interactive_supported,
            "state": self.state.value,
        }
        # Only a signed-in identity carries one, and only when the CLI
        # reported something we recognise: the panel falls back to the plain
        # "Signed in" label otherwise.
        if self.identity is not None and not self.identity.empty:
            payload["identity"] = self.identity.payload()
        return payload


@dataclass(frozen=True, slots=True)
class DeviceAuthorization:
    verification_url: str
    user_code: str
    expires_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "expiresAt": self.expires_at.isoformat().replace("+00:00", "Z"),
            "userCode": self.user_code,
            "verificationUrl": self.verification_url,
        }


@dataclass(frozen=True, slots=True)
class OrganizationLogin:
    """Identity Center (SSO) login parameters supplied per request."""

    start_url: str
    region: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.start_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("An https Identity Center start URL is required.")
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", self.region):
            raise ValueError("A valid AWS region is required for SSO login.")


class DeviceFlowParser:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        default_ttl_seconds: int = 600,
    ) -> None:
        if default_ttl_seconds <= 0:
            raise ValueError("Device authorization TTL must be positive.")
        self._clock = clock
        self._default_ttl = default_ttl_seconds
        self._buffer = ""
        self._authorization: DeviceAuthorization | None = None

    @property
    def authorization(self) -> DeviceAuthorization | None:
        return self._authorization

    def feed(self, text: str) -> DeviceAuthorization | None:
        self._buffer = (self._buffer + "\n" + text)[-8192:]
        code_matches = _CODE_PATTERN.findall(self._buffer)
        if not code_matches:
            return None
        url: str | None = None
        for url_match in _URL_PATTERN.finditer(self._buffer):
            candidate = url_match.group(0).rstrip(".,);]")
            parsed = urlsplit(candidate)
            if parsed.scheme != "https" or not parsed.hostname:
                continue
            if url is None:
                url = candidate
            if "user_code=" in candidate:
                # Organization logins echo the bare start URL in the prompt
                # first; the deep verification link is the one to present.
                url = candidate
                break
        if url is None:
            return None
        code = code_matches[-1].upper()
        current = self._authorization
        if current is not None and current.user_code == code and current.verification_url == url:
            # Nothing changed since the last emission; stay quiet so callers
            # can forward every returned authorization to the client.
            return None
        seconds = self._default_ttl
        expiry_match = _EXPIRY_PATTERN.search(self._buffer)
        if expiry_match is not None:
            amount = int(expiry_match.group(1))
            seconds = amount * (60 if expiry_match.group(2).lower().startswith("minute") else 1)
        self._authorization = DeviceAuthorization(
            url,
            code,
            self._clock() + timedelta(seconds=seconds),
        )
        return self._authorization

    def expired(self) -> bool:
        return self._authorization is not None and self._clock() >= self._authorization.expires_at


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str


class DeviceProcess(Protocol):
    def lines(self) -> AsyncIterator[str]: ...

    async def write(self, data: bytes) -> None: ...

    async def wait(self) -> int: ...

    async def terminate(self) -> None: ...


class KiroCommandRunner(Protocol):
    def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> CommandResult: ...

    def spawn_pty(self, command: list[str], env: Mapping[str, str]) -> DeviceProcess: ...


class SecretProvider(Protocol):
    def get_secret(self) -> str: ...


class SecretsManagerClient(Protocol):
    def get_secret_value(self, *, SecretId: str) -> Mapping[str, object]: ...  # noqa: N803


class SecretsManagerSecretProvider:
    def __init__(self, client: SecretsManagerClient, secret_arn: str) -> None:
        if not secret_arn.startswith("arn:") or ":secretsmanager:" not in secret_arn:
            raise ValueError("A Secrets Manager secret ARN is required.")
        self._client = client
        self._secret_arn = secret_arn

    def get_secret(self) -> str:
        response = self._client.get_secret_value(SecretId=self._secret_arn)
        value = response.get("SecretString")
        if not isinstance(value, str) or not value:
            raise KiroIdentityError("The Kiro API-key secret is unavailable.")
        return value


@dataclass(frozen=True, slots=True)
class KiroIdentityConfig:
    mode: str
    home: Path
    api_key_home: Path
    executable: str = "kiro-cli"
    license: str | None = None
    identity_provider: str | None = None
    region: str | None = None
    trusted_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.mode not in {"device_flow", "api_key"} or not self.executable:
            raise ValueError("Kiro identity configuration is invalid.")
        organization_values = (self.license, self.identity_provider, self.region)
        if any(organization_values) and not all(organization_values):
            raise ValueError("Kiro organization login configuration must be complete.")
        if self.license is not None and self.license != "pro":
            raise ValueError("Organization Device Flow requires a pro license.")
        if any(not tool or "," in tool for tool in self.trusted_tools):
            raise ValueError("Trusted tool names are invalid.")


class KiroIdentityManager:
    def __init__(
        self,
        config: KiroIdentityConfig,
        runner: KiroCommandRunner,
        *,
        secret_provider: SecretProvider | None = None,
        parser: DeviceFlowParser | None = None,
    ) -> None:
        if config.mode == "api_key" and secret_provider is None:
            raise ValueError("API-key mode requires a secret provider.")
        if config.mode == "device_flow" and secret_provider is not None:
            raise ValueError("Device Flow mode must not receive an API-key provider.")
        self._config = config
        self._runner = runner
        self._secret_provider = secret_provider
        self._parser_factory: Callable[[], DeviceFlowParser] = (
            (lambda: parser) if parser is not None else DeviceFlowParser
        )
        self._parser = self._parser_factory()
        self._process: DeviceProcess | None = None

    def status(self) -> KiroAuthStatus:
        if self._config.mode == "api_key":
            secret = cast(SecretProvider, self._secret_provider).get_secret()
            return KiroAuthStatus(
                KiroAuthState.AUTHENTICATED if secret else KiroAuthState.FAILED,
                interactive_supported=False,
            )
        environment = self._device_environment()
        try:
            result = self._runner.run(
                [self._config.executable, "whoami", "--format", "json"],
                environment,
                # Generous: the CLI's settings database can be briefly locked
                # by a concurrent chat process inside the sandbox.
                timeout_seconds=30,
            )
        except subprocess.TimeoutExpired:
            return KiroAuthStatus(KiroAuthState.FAILED, interactive_supported=True)
        if result.returncode != 0:
            return KiroAuthStatus(KiroAuthState.REQUIRED, interactive_supported=True)
        value: object = None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            # Update notices or warnings can surround the JSON document.
            embedded = re.search(r"\{.*\}", result.stdout, re.DOTALL)
            if embedded is not None:
                try:
                    value = json.loads(embedded.group(0))
                except json.JSONDecodeError:
                    value = None
        identity = KiroIdentity.parse(result.stdout)
        if not isinstance(value, dict):
            # The exit code is the authoritative signal: whoami only succeeds
            # for a signed-in identity, however noisy its output.
            return KiroAuthStatus(
                KiroAuthState.AUTHENTICATED, interactive_supported=True, identity=identity
            )
        return KiroAuthStatus(
            KiroAuthState.AUTHENTICATED if value else KiroAuthState.REQUIRED,
            interactive_supported=True,
            identity=identity if value else None,
        )

    async def device_flow_events(
        self,
        organization: OrganizationLogin | None = None,
    ) -> AsyncIterator[tuple[str, Mapping[str, object]]]:
        if self._config.mode != "device_flow":
            raise KiroIdentityError(
                "Interactive Kiro authentication is unavailable in API-key mode."
            )
        if self._process is not None:
            raise KiroIdentityError("A Kiro Device Flow is already active.")
        # Every flow needs a fresh parser: a shared buffer would deduplicate
        # a repeated device code away and starve the new flow of its event.
        self._parser = self._parser_factory()
        command = [self._config.executable, "login", "--use-device-flow"]
        if organization is not None:
            command.extend(
                [
                    "--license",
                    "pro",
                    "--identity-provider",
                    organization.start_url,
                    "--region",
                    organization.region,
                ]
            )
        elif self._config.license is not None:
            command.extend(
                [
                    "--license",
                    self._config.license,
                    "--identity-provider",
                    cast(str, self._config.identity_provider),
                    "--region",
                    cast(str, self._config.region),
                ]
            )
        else:
            # Without an explicit license kiro-cli renders an interactive
            # "Select login method" menu and blocks forever in a PTY.
            command.extend(["--license", "free"])
        process = self._runner.spawn_pty(command, self._device_environment())
        self._process = process
        if organization is not None:
            # kiro-cli pre-fills the Start URL and Region prompts from the
            # flags but still waits for interactive confirmation. A PTY
            # Enter is carriage return: dialoguer accepts "\r" and submits
            # the pre-filled value, whereas "\n" corrupts the edit buffer
            # and the login hangs on an empty start URL forever.
            await process.write(b"\r\r")
        try:
            async for line in process.lines():
                authorization = self._parser.feed(line)
                if authorization is not None:
                    # Re-emitted when the parser upgrades the bare start URL
                    # to the deep verification link; the banner just updates.
                    yield "kiro.auth_required", authorization.payload()
            returncode = await process.wait()
            status = self.status()
            if returncode == 0 and status.state is KiroAuthState.AUTHENTICATED:
                yield "kiro.authenticated", status.payload()
            elif self._parser.expired():
                yield (
                    "kiro.auth_status",
                    KiroAuthStatus(KiroAuthState.EXPIRED, interactive_supported=True).payload(),
                )
            else:
                yield (
                    "kiro.auth_status",
                    KiroAuthStatus(KiroAuthState.FAILED, interactive_supported=True).payload(),
                )
        finally:
            self._process = None

    async def cancel_device_flow(self) -> bool:
        process = self._process
        if process is None:
            return False
        await process.terminate()
        self._process = None
        return True

    async def logout(self) -> KiroAuthStatus:
        if self._config.mode != "device_flow":
            raise KiroIdentityError("Kiro logout is unavailable in API-key mode.")
        await self.cancel_device_flow()
        await asyncio.to_thread(
            self._runner.run,
            [self._config.executable, "logout"],
            self._device_environment(),
            timeout_seconds=30,
        )
        return await asyncio.to_thread(self.status)

    def run_headless(self, prompt: str) -> CommandResult:
        if self._config.mode != "api_key":
            raise KiroIdentityError("Headless API-key execution is disabled in Device Flow mode.")
        if not prompt:
            raise ValueError("A non-empty headless prompt is required.")
        secret = cast(SecretProvider, self._secret_provider).get_secret()
        if not secret:
            raise KiroIdentityError("Kiro API-key authentication is unavailable.")
        self._config.api_key_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._config.api_key_home.chmod(0o700)
        environment = _base_environment()
        environment["HOME"] = str(self._config.api_key_home)
        environment["KIRO_API_KEY"] = secret
        trusted = ",".join(self._config.trusted_tools)
        command = [
            self._config.executable,
            "chat",
            "--no-interactive",
            f"--trust-tools={trusted}",
            "--require-mcp-startup",
            prompt,
        ]
        try:
            return self._runner.run(command, environment, timeout_seconds=300)
        finally:
            environment.pop("KIRO_API_KEY", None)

    def require_interactive_identity(self) -> None:
        status = self.status()
        if not status.interactive_supported:
            raise KiroIdentityError("Interactive ACP requires Device Flow authentication.")
        if status.state is not KiroAuthState.AUTHENTICATED:
            raise KiroIdentityError("Interactive Kiro authentication is required.")

    def _device_environment(self) -> dict[str, str]:
        environment = _base_environment()
        environment["HOME"] = str(self._config.home)
        environment.pop("KIRO_API_KEY", None)
        return environment


class LocalKiroCommandRunner:
    def run(
        self,
        command: list[str],
        env: Mapping[str, str],
        *,
        timeout_seconds: float,
    ) -> CommandResult:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            command,
            env=dict(env),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        return CommandResult(completed.returncode, completed.stdout)

    def spawn_pty(self, command: list[str], env: Mapping[str, str]) -> DeviceProcess:
        return PtyDeviceProcess.start(command, env)


class PtyDeviceProcess:
    def __init__(self, process: subprocess.Popen[bytes], stream: IO[bytes]) -> None:
        self._process = process
        self._stream = stream

    @classmethod
    def start(cls, command: list[str], env: Mapping[str, str]) -> PtyDeviceProcess:
        import pty

        master, slave = pty.openpty()
        try:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                command,
                env=dict(env),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
            )
        finally:
            os.close(slave)
        return cls(process, os.fdopen(master, "rb", buffering=0))

    async def write(self, data: bytes) -> None:
        await asyncio.to_thread(os.write, self._stream.fileno(), data)

    async def lines(self) -> AsyncIterator[str]:
        while True:
            try:
                line = await asyncio.to_thread(self._stream.readline)
            except OSError:
                return
            if not line:
                return
            yield line.decode(errors="replace")

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)

    async def terminate(self) -> None:
        if self._process.poll() is None:
            os.killpg(self._process.pid, signal.SIGTERM)
            try:
                await asyncio.to_thread(self._process.wait, 5)
            except subprocess.TimeoutExpired:
                os.killpg(self._process.pid, signal.SIGKILL)
                await asyncio.to_thread(self._process.wait)
        self._stream.close()


def redact_kiro_output(value: str) -> str:
    redacted = _BEARER_PATTERN.sub("Bearer [REDACTED]", value)
    redacted = _SECRET_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}=[REDACTED]", redacted
    )
    redacted = _CODE_PATTERN.sub(
        lambda match: match.group(0).replace(match.group(1), "[REDACTED]"), redacted
    )
    return redacted


def _base_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "KIRO_API_KEY",
        }
    }
