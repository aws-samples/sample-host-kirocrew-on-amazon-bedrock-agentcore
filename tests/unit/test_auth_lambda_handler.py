from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]
from kirocrew_agentcore_auth import lambda_handler as auth

POOL = "us-east-2_example"
CLIENT = "client-1234567890"


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "operation")


class FakeCognito:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, object]] = {}
        self.created: list[dict[str, Any]] = []
        self.passwords: list[dict[str, Any]] = []
        self.auth_requests: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.create_error: ClientError | None = None
        self.password_error: ClientError | None = None
        self.auth_error: ClientError | None = None
        self.auth_response: dict[str, Any] = {
            "AuthenticationResult": {
                "AccessToken": "access",
                "IdToken": "identity",
                "RefreshToken": "refresh",
                "ExpiresIn": 3600,
            }
        }

    def admin_create_user(self, **request: Any) -> dict[str, object]:
        self.created.append(request)
        if self.create_error is not None:
            raise self.create_error
        return {}

    def admin_set_user_password(self, **request: Any) -> dict[str, object]:
        self.passwords.append(request)
        if self.password_error is not None:
            raise self.password_error
        return {}

    def admin_delete_user(self, **request: Any) -> dict[str, object]:
        self.deleted.append(str(request["Username"]))
        return {}

    def admin_initiate_auth(self, **request: Any) -> dict[str, Any]:
        self.auth_requests.append(request)
        if self.auth_error is not None:
            raise self.auth_error
        return self.auth_response


def service(cognito: FakeCognito | None = None) -> tuple[auth.AuthService, FakeCognito]:
    fake = cognito or FakeCognito()
    policy = auth.EmailPolicy.from_environment("amazon.com, Example.ORG")
    return auth.AuthService(fake, POOL, CLIENT, policy), fake


def test_email_policy_normalizes_validates_and_enforces_domains() -> None:
    policy = auth.EmailPolicy.from_environment("amazon.com")
    assert policy.validate("  User@AMAZON.com ") == "user@amazon.com"
    for invalid in (None, 7, "", "not-an-email", "a@b", "user@" + "x" * 250 + ".com"):
        with pytest.raises(auth.AuthRequestError) as error:
            policy.validate(invalid)
        assert error.value.code == "INVALID_EMAIL"
    with pytest.raises(auth.AuthRequestError) as denied:
        policy.validate("user@other.com")
    assert denied.value.code == "DOMAIN_NOT_ALLOWED"
    assert denied.value.status == 403
    # A subdomain is a different domain.
    with pytest.raises(auth.AuthRequestError):
        policy.validate("user@evil.amazon.com.attacker.net")
    with pytest.raises(auth.AuthRequestError):
        policy.validate("user@sub.amazon.com")
    with pytest.raises(ValueError, match="allowed email domain"):
        auth.EmailPolicy.from_environment(" , ")


def test_registration_creates_a_confirmed_user_with_permanent_password() -> None:
    subject, fake = service()
    subject.register("Dev@Amazon.com", "CorrectHorse#42")
    assert fake.created[0]["Username"] == "dev@amazon.com"
    assert fake.created[0]["MessageAction"] == "SUPPRESS"
    assert {"Name": "email_verified", "Value": "true"} in fake.created[0]["UserAttributes"]
    assert fake.passwords[0]["Permanent"] is True
    assert fake.deleted == []

    with pytest.raises(auth.AuthRequestError) as bad_password:
        subject.register("dev@amazon.com", "short")
    assert bad_password.value.code == "INVALID_PASSWORD"

    fake.create_error = client_error("UsernameExistsException")
    with pytest.raises(auth.AuthRequestError) as exists:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert exists.value.status == 409

    fake.create_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert unavailable.value.status == 502


def test_registration_rolls_back_the_half_created_user_on_password_rejection() -> None:
    subject, fake = service()
    fake.password_error = client_error("InvalidPasswordException")
    with pytest.raises(auth.AuthRequestError) as error:
        subject.register("dev@amazon.com", "not-compliant-but-long")
    assert error.value.code == "INVALID_PASSWORD"
    assert fake.deleted == ["dev@amazon.com"]


def test_login_returns_tokens_and_never_reveals_account_existence() -> None:
    subject, fake = service()
    tokens = subject.login("dev@amazon.com", "CorrectHorse#42")
    assert tokens.payload() == {
        "accessToken": "access",
        "expiresIn": 3600,
        "idToken": "identity",
        "refreshToken": "refresh",
        "tokenType": "Bearer",
    }
    assert fake.auth_requests[0]["AuthFlow"] == "ADMIN_USER_PASSWORD_AUTH"

    messages = set()
    for code in ("NotAuthorizedException", "UserNotFoundException"):
        fake.auth_error = client_error(code)
        with pytest.raises(auth.AuthRequestError) as error:
            subject.login("dev@amazon.com", "CorrectHorse#42")
        assert error.value.status == 401
        messages.add(str(error.value))
    assert len(messages) == 1

    fake.auth_error = client_error("PasswordResetRequiredException")
    with pytest.raises(auth.AuthRequestError) as reset:
        subject.login("dev@amazon.com", "CorrectHorse#42")
    assert reset.value.code == "PASSWORD_RESET_REQUIRED"

    fake.auth_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.login("dev@amazon.com", "CorrectHorse#42")
    assert unavailable.value.status == 502

    with pytest.raises(auth.AuthRequestError) as denied:
        subject.login("dev@other.com", "CorrectHorse#42")
    assert denied.value.code == "DOMAIN_NOT_ALLOWED"


def test_login_rejects_challenges_and_malformed_authentication_results() -> None:
    subject, fake = service()
    fake.auth_response = {"ChallengeName": "SOFTWARE_TOKEN_MFA"}
    with pytest.raises(auth.AuthRequestError) as challenge:
        subject.login("dev@amazon.com", "CorrectHorse#42")
    assert challenge.value.code == "CHALLENGE_NOT_SUPPORTED"

    for malformed in (
        {},
        {"AuthenticationResult": "nope"},
        {"AuthenticationResult": {"AccessToken": "a", "IdToken": "i", "ExpiresIn": 0}},
        {"AuthenticationResult": {"AccessToken": "a", "IdToken": "i", "ExpiresIn": 3600}},
        {
            "AuthenticationResult": {
                "AccessToken": "a",
                "IdToken": "i",
                "RefreshToken": 5,
                "ExpiresIn": 3600,
            }
        },
    ):
        fake.auth_response = malformed
        with pytest.raises(auth.AuthRequestError) as error:
            subject.login("dev@amazon.com", "CorrectHorse#42")
        assert error.value.status in {501, 502}


def test_refresh_uses_the_refresh_flow_and_tolerates_absent_rotation() -> None:
    subject, fake = service()
    fake.auth_response = {
        "AuthenticationResult": {"AccessToken": "a", "IdToken": "i", "ExpiresIn": 3600}
    }
    tokens = subject.refresh("refresh-token")
    assert "refreshToken" not in tokens.payload()
    assert fake.auth_requests[0]["AuthFlow"] == "REFRESH_TOKEN_AUTH"

    for invalid in (None, "", 4):
        with pytest.raises(auth.AuthRequestError):
            subject.refresh(invalid)

    fake.auth_error = client_error("NotAuthorizedException")
    with pytest.raises(auth.AuthRequestError) as expired:
        subject.refresh("refresh-token")
    assert expired.value.status == 401


def test_service_requires_identifiers() -> None:
    with pytest.raises(ValueError, match="identifiers"):
        auth.AuthService(FakeCognito(), "", CLIENT, auth.EmailPolicy(("amazon.com",)))


def test_handler_routes_requests_and_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCognito()
    for name, value in {
        "REGION": "us-east-2",
        "USER_POOL_ID": POOL,
        "APP_CLIENT_ID": CLIENT,
        "ALLOWED_EMAIL_DOMAINS": "amazon.com",
    }.items():
        monkeypatch.setenv(name, value)

    class Session:
        def __init__(self, *, region_name: str) -> None:
            assert region_name == "us-east-2"

        def client(self, name: str) -> FakeCognito:
            assert name == "cognito-idp"
            return fake

    monkeypatch.setattr(auth, "_SERVICE", None)
    monkeypatch.setattr("kirocrew_agentcore_auth.lambda_handler.boto3.Session", Session)

    def event(route: str, body: object) -> dict[str, object]:
        return {"routeKey": route, "body": json.dumps(body) if body is not None else None}

    created = auth.handler(
        event("POST /auth/v1/register", {"email": "dev@amazon.com", "password": "Horse#4242"}),
        object(),
    )
    assert created["statusCode"] == 201
    assert created["headers"]["cache-control"] == "no-store"  # type: ignore[index]

    logged_in = auth.handler(
        event("POST /auth/v1/login", {"email": "dev@amazon.com", "password": "Horse#4242"}),
        object(),
    )
    assert logged_in["statusCode"] == 200
    assert json.loads(cast_str(logged_in["body"]))["accessToken"] == "access"

    refreshed = auth.handler(event("POST /auth/v1/refresh", {"refreshToken": "refresh"}), object())
    assert refreshed["statusCode"] == 200

    unknown = auth.handler(event("GET /auth/v1/unknown", {}), object())
    assert unknown["statusCode"] == 404

    denied = auth.handler(
        event("POST /auth/v1/login", {"email": "dev@other.com", "password": "Horse#4242"}),
        object(),
    )
    assert denied["statusCode"] == 403
    assert json.loads(cast_str(denied["body"]))["code"] == "DOMAIN_NOT_ALLOWED"

    for body in (None, "{", json.dumps([1]), "x" * 5000):
        raw = {"routeKey": "POST /auth/v1/login", "body": body}
        assert auth.handler(raw, object())["statusCode"] == 400

    # The built service is cached across invocations.
    assert (
        auth.handler(event("POST /auth/v1/refresh", {"refreshToken": "r"}), object())["statusCode"]
        == 200
    )


def test_environment_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth, "_SERVICE", None)
    monkeypatch.delenv("REGION", raising=False)
    with pytest.raises(ValueError, match="REGION"):
        auth.handler({"routeKey": "POST /auth/v1/login", "body": "{}"}, object())


def cast_str(value: object) -> str:
    assert isinstance(value, str)
    return value
