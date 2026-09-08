"""Gated Cognito authentication for the browser shell.

The user pool only permits administrator-created accounts and its app client
only permits administrator-driven password auth, so this Lambda is the sole
path to both registration and sign-in. It enforces the deployment's allowed
email domains on every operation, which is what keeps an open CloudFront
endpoint from becoming an open registration endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, cast

import boto3  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

_LOGGER = logging.getLogger(__name__)

_EMAIL_PATTERN: Final = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")
_MAX_BODY_BYTES: Final = 4096
_SECURITY_HEADERS: Final = {
    "cache-control": "no-store",
    "content-type": "application/json",
    "x-content-type-options": "nosniff",
}


class AuthRequestError(Exception):
    """A client-visible authentication failure with a stable error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True, slots=True)
class EmailPolicy:
    """Case-insensitive full-domain matching against the deployment allowlist.

    Individual addresses outside the allowed domains can be admitted through
    ``allowed_patterns``: full-address regular expressions that are matched
    case-insensitively against the whole normalized email.
    """

    allowed_domains: tuple[str, ...]
    allowed_patterns: tuple[re.Pattern[str], ...] = ()

    def __post_init__(self) -> None:
        if not self.allowed_domains or any(not domain for domain in self.allowed_domains):
            raise ValueError("At least one allowed email domain is required.")

    @classmethod
    def from_environment(cls, domains: str, patterns: str = "[]") -> EmailPolicy:
        parsed = tuple(domain.strip().casefold() for domain in domains.split(",") if domain.strip())
        raw = cast(object, json.loads(patterns))
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise ValueError("ALLOWED_EMAIL_PATTERNS must be a JSON list of strings.")
        compiled = tuple(
            re.compile(item, re.IGNORECASE) for item in cast(list[str], raw) if item.strip()
        )
        return cls(parsed, compiled)

    def validate(self, email: object) -> str:
        if not isinstance(email, str) or len(email) > 254:
            raise AuthRequestError(400, "INVALID_EMAIL", "A valid email address is required.")
        candidate = email.strip().casefold()
        match = _EMAIL_PATTERN.fullmatch(candidate)
        if match is None:
            raise AuthRequestError(400, "INVALID_EMAIL", "A valid email address is required.")
        if match.group(1) in self.allowed_domains:
            return candidate
        if any(pattern.fullmatch(candidate) for pattern in self.allowed_patterns):
            return candidate
        allowed = ", ".join(self.allowed_domains)
        # 422, not 403: the CloudFront distribution rewrites 403/404
        # responses into the SPA fallback page for deep links, which
        # would swallow this error body on its way to the sign-in form.
        raise AuthRequestError(
            422,
            "DOMAIN_NOT_ALLOWED",
            f"Registration and sign-in are limited to: {allowed}.",
        )


# The one sentence every password rejection from the pool answers with. It
# states the requirement instead of naming "complexity requirements", and it
# mirrors the pool's password_policy in infrastructure/modules/identity.
_STRENGTH_REQUIREMENT = (
    "Passwords need at least 8 characters, including a lowercase letter and a number."
)


def _password(value: object) -> str:
    if not isinstance(value, str) or not (8 <= len(value) <= 256):
        raise AuthRequestError(
            400, "INVALID_PASSWORD", "A password of 8-256 characters is required."
        )
    return value


@dataclass(frozen=True, slots=True)
class TokenSet:
    access_token: str
    id_token: str
    refresh_token: str | None
    expires_in: int

    def payload(self) -> dict[str, object]:
        value: dict[str, object] = {
            "accessToken": self.access_token,
            "expiresIn": self.expires_in,
            "idToken": self.id_token,
            "tokenType": "Bearer",
        }
        if self.refresh_token is not None:
            value["refreshToken"] = self.refresh_token
        return value


class AuthService:
    def __init__(
        self,
        cognito: Any,
        user_pool_id: str,
        app_client_id: str,
        policy: EmailPolicy,
    ) -> None:
        if not user_pool_id or not app_client_id:
            raise ValueError("User pool and app client identifiers are required.")
        self._cognito = cognito
        self._user_pool_id = user_pool_id
        self._app_client_id = app_client_id
        self._policy = policy

    def register(self, email: object, password: object) -> None:
        """Create the account unverified and email a confirmation code."""
        address = self._policy.validate(email)
        secret = _password(password)
        try:
            self._create_user(address)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code != "UsernameExistsException":
                raise self._invalid_password(error) from error
            if self._is_verified(address):
                raise AuthRequestError(
                    409, "USER_EXISTS", "An account with this email already exists."
                ) from error
            # An unverified account is an unproven claim on the address. The
            # real owner must be able to register over it, so it yields.
            self._cognito.admin_delete_user(UserPoolId=self._user_pool_id, Username=address)
            try:
                self._create_user(address)
            except ClientError as retry_error:
                raise self._invalid_password(retry_error) from retry_error
        try:
            self._cognito.admin_set_user_password(
                UserPoolId=self._user_pool_id,
                Username=address,
                Password=secret,
                Permanent=True,
            )
        except ClientError as error:
            # The half-created account must not linger: it would block a retry
            # with a compliant password behind USER_EXISTS forever.
            self._cognito.admin_delete_user(UserPoolId=self._user_pool_id, Username=address)
            raise self._invalid_password(error) from error
        self._send_confirmation_code(address, secret)

    def _create_user(self, address: str) -> None:
        self._cognito.admin_create_user(
            UserPoolId=self._user_pool_id,
            Username=address,
            UserAttributes=[
                {"Name": "email", "Value": address},
                {"Name": "email_verified", "Value": "false"},
            ],
            MessageAction="SUPPRESS",
        )

    def _is_verified(self, address: str) -> bool:
        try:
            user = self._cognito.admin_get_user(UserPoolId=self._user_pool_id, Username=address)
        except ClientError as error:
            raise self._reset_failed(error.response.get("Error", {}).get("Code")) from error
        attributes = cast(list[Mapping[str, str]], user.get("UserAttributes", []))
        return any(
            item.get("Name") == "email_verified" and item.get("Value") == "true"
            for item in attributes
        )

    def _authenticate(self, address: str, secret: str) -> Mapping[str, Any]:
        try:
            return cast(
                Mapping[str, Any],
                self._cognito.admin_initiate_auth(
                    UserPoolId=self._user_pool_id,
                    ClientId=self._app_client_id,
                    AuthFlow="ADMIN_USER_PASSWORD_AUTH",
                    AuthParameters={"USERNAME": address, "PASSWORD": secret},
                ),
            )
        except ClientError as error:
            raise self._sign_in_failed(error) from error

    def _send_confirmation_code(self, address: str, secret: str) -> None:
        """Email a verification code, acting with the user's own session."""
        tokens = self._tokens(self._authenticate(address, secret), refresh_required=True)
        try:
            self._cognito.get_user_attribute_verification_code(
                AccessToken=tokens.access_token, AttributeName="email"
            )
        except ClientError as error:
            raise self._reset_failed(error.response.get("Error", {}).get("Code")) from error

    def confirm(self, email: object, password: object, code: object) -> TokenSet:
        """Verify the emailed code, then sign the user in."""
        address = self._policy.validate(email)
        secret = _password(password)
        if not isinstance(code, str) or not (1 <= len(code.strip()) <= 64):
            raise AuthRequestError(400, "INVALID_CODE", "The code is incorrect or has expired.")
        tokens = self._tokens(self._authenticate(address, secret), refresh_required=True)
        try:
            self._cognito.verify_user_attribute(
                AccessToken=tokens.access_token, AttributeName="email", Code=code.strip()
            )
        except ClientError as error:
            name = error.response.get("Error", {}).get("Code")
            if name in {"CodeMismatchException", "ExpiredCodeException"}:
                raise AuthRequestError(
                    400, "INVALID_CODE", "The code is incorrect or has expired."
                ) from error
            raise self._reset_failed(name) from error
        return tokens

    def resend(self, email: object, password: object) -> None:
        address = self._policy.validate(email)
        secret = _password(password)
        self._send_confirmation_code(address, secret)

    def login(self, email: object, password: object) -> TokenSet:
        address = self._policy.validate(email)
        secret = _password(password)
        tokens = self._tokens(self._authenticate(address, secret), refresh_required=True)
        if not self._is_verified(address):
            raise AuthRequestError(409, "EMAIL_NOT_VERIFIED", "Confirm your email address first.")
        return tokens

    def refresh(self, refresh_token: object) -> TokenSet:
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AuthRequestError(400, "INVALID_REQUEST", "A refresh token is required.")
        try:
            response = self._cognito.admin_initiate_auth(
                UserPoolId=self._user_pool_id,
                ClientId=self._app_client_id,
                AuthFlow="REFRESH_TOKEN_AUTH",
                AuthParameters={"REFRESH_TOKEN": refresh_token},
            )
        except ClientError as error:
            raise self._sign_in_failed(error) from error
        return self._tokens(response, refresh_required=False)

    def forgot_password(self, email: object) -> None:
        """Start a Cognito reset; the code is emailed to the verified address."""
        address = self._policy.validate(email)
        try:
            self._cognito.forgot_password(ClientId=self._app_client_id, Username=address)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"UserNotFoundException", "NotAuthorizedException"}:
                # Answer exactly like success: account existence and state
                # must not be probeable through this endpoint.
                _LOGGER.info("Password reset requested for an ineligible account.")
                return
            raise self._reset_failed(code) from error

    def reset_password(self, email: object, code: object, password: object) -> None:
        address = self._policy.validate(email)
        secret = _password(password)
        if not isinstance(code, str) or not (1 <= len(code.strip()) <= 64):
            raise AuthRequestError(400, "INVALID_CODE", "The code is incorrect or has expired.")
        try:
            self._cognito.confirm_forgot_password(
                ClientId=self._app_client_id,
                Username=address,
                ConfirmationCode=code.strip(),
                Password=secret,
            )
        except ClientError as error:
            name = error.response.get("Error", {}).get("Code")
            if name in {"CodeMismatchException", "ExpiredCodeException", "UserNotFoundException"}:
                # One answer for a wrong code and an unknown account: account
                # existence must not be probeable.
                raise AuthRequestError(
                    400, "INVALID_CODE", "The code is incorrect or has expired."
                ) from error
            if name == "InvalidPasswordException":
                raise AuthRequestError(400, "INVALID_PASSWORD", _STRENGTH_REQUIREMENT) from error
            raise self._reset_failed(name) from error

    @staticmethod
    def _reset_failed(code: object) -> AuthRequestError:
        if code in {
            "LimitExceededException",
            "TooManyRequestsException",
            "TooManyFailedAttemptsException",
        }:
            return AuthRequestError(429, "TOO_MANY_ATTEMPTS", "Too many attempts. Try again later.")
        _LOGGER.error("Cognito password reset failed with %s.", code)
        return AuthRequestError(502, "RESET_UNAVAILABLE", "Password reset is unavailable.")

    @staticmethod
    def _invalid_password(error: ClientError) -> AuthRequestError:
        code = error.response.get("Error", {}).get("Code")
        if code == "InvalidPasswordException":
            return AuthRequestError(400, "INVALID_PASSWORD", _STRENGTH_REQUIREMENT)
        _LOGGER.error("Cognito registration failed with %s.", code)
        return AuthRequestError(502, "REGISTRATION_FAILED", "Registration is unavailable.")

    @staticmethod
    def _sign_in_failed(error: ClientError) -> AuthRequestError:
        code = error.response.get("Error", {}).get("Code")
        if code in {"NotAuthorizedException", "UserNotFoundException"}:
            # One message for both: account existence must not be probeable.
            return AuthRequestError(401, "SIGN_IN_FAILED", "Incorrect email or password.")
        if code == "PasswordResetRequiredException":
            return AuthRequestError(
                409, "PASSWORD_RESET_REQUIRED", "An administrator reset this password."
            )
        _LOGGER.error("Cognito sign-in failed with %s.", code)
        return AuthRequestError(502, "SIGN_IN_UNAVAILABLE", "Sign-in is unavailable.")

    def _tokens(self, response: Mapping[str, Any], *, refresh_required: bool) -> TokenSet:
        challenge = response.get("ChallengeName")
        if challenge:
            # MFA and forced-rotation challenges are not part of this UI.
            _LOGGER.error("Unsupported Cognito challenge %s.", challenge)
            raise AuthRequestError(
                501,
                "CHALLENGE_NOT_SUPPORTED",
                "This account requires an interactive challenge that is not supported.",
            )
        result = response.get("AuthenticationResult")
        if not isinstance(result, Mapping):
            raise AuthRequestError(502, "SIGN_IN_UNAVAILABLE", "Sign-in is unavailable.")
        access_token = result.get("AccessToken")
        id_token = result.get("IdToken")
        refresh_token = result.get("RefreshToken")
        expires_in = result.get("ExpiresIn")
        if (
            not isinstance(access_token, str)
            or not isinstance(id_token, str)
            or type(expires_in) is not int
            or expires_in <= 0
            or (refresh_required and not isinstance(refresh_token, str))
            or (refresh_token is not None and not isinstance(refresh_token, str))
        ):
            raise AuthRequestError(502, "SIGN_IN_UNAVAILABLE", "Sign-in is unavailable.")
        return TokenSet(access_token, id_token, refresh_token, expires_in)


def _response(status: int, body: Mapping[str, object]) -> dict[str, object]:
    return {
        "statusCode": status,
        "headers": dict(_SECURITY_HEADERS),
        "body": json.dumps(body, sort_keys=True, separators=(",", ":")),
    }


def _request_body(event: Mapping[str, object]) -> Mapping[str, object]:
    raw = event.get("body")
    if not isinstance(raw, str) or len(raw.encode()) > _MAX_BODY_BYTES:
        raise AuthRequestError(400, "INVALID_REQUEST", "A JSON request body is required.")
    try:
        value = cast(object, json.loads(raw))
    except json.JSONDecodeError as error:
        raise AuthRequestError(
            400, "INVALID_REQUEST", "A JSON request body is required."
        ) from error
    if not isinstance(value, dict):
        raise AuthRequestError(400, "INVALID_REQUEST", "A JSON request body is required.")
    return cast(Mapping[str, object], value)


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required.")
    return value


def _build_service() -> AuthService:
    session = boto3.Session(region_name=_required("REGION"))
    return AuthService(
        session.client("cognito-idp"),
        _required("USER_POOL_ID"),
        _required("APP_CLIENT_ID"),
        EmailPolicy.from_environment(
            _required("ALLOWED_EMAIL_DOMAINS"),
            os.environ.get("ALLOWED_EMAIL_PATTERNS", "[]"),
        ),
    )


_SERVICE: AuthService | None = None


def handler(event: Mapping[str, object], _context: object) -> dict[str, object]:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service()
    service = _SERVICE
    route = event.get("routeKey")
    try:
        body = _request_body(event)
        if route == "POST /auth/v1/register":
            service.register(body.get("email"), body.get("password"))
            return _response(201, {"confirmationRequired": True, "registered": True})
        if route == "POST /auth/v1/confirm":
            return _response(
                200,
                service.confirm(
                    body.get("email"), body.get("password"), body.get("code")
                ).payload(),
            )
        if route == "POST /auth/v1/resend":
            service.resend(body.get("email"), body.get("password"))
            return _response(200, {"sent": True})
        if route == "POST /auth/v1/login":
            return _response(200, service.login(body.get("email"), body.get("password")).payload())
        if route == "POST /auth/v1/refresh":
            return _response(200, service.refresh(body.get("refreshToken")).payload())
        if route == "POST /auth/v1/forgot":
            service.forgot_password(body.get("email"))
            return _response(200, {"sent": True})
        if route == "POST /auth/v1/reset":
            service.reset_password(body.get("email"), body.get("code"), body.get("password"))
            return _response(200, {"reset": True})
        return _response(404, {"code": "NOT_FOUND", "message": "Unknown route."})
    except AuthRequestError as error:
        return _response(error.status, {"code": error.code, "message": str(error)})
