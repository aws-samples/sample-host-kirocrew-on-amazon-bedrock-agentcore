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
        self.create_errors: list[ClientError] = []
        self.password_error: ClientError | None = None
        self.auth_error: ClientError | None = None
        self.email_verified = True
        self.get_user_error: ClientError | None = None
        self.code_requests: list[dict[str, Any]] = []
        self.code_error: ClientError | None = None
        self.verify_requests: list[dict[str, Any]] = []
        self.verify_error: ClientError | None = None
        self.forgot_requests: list[dict[str, Any]] = []
        self.forgot_error: ClientError | None = None
        self.confirm_requests: list[dict[str, Any]] = []
        self.confirm_error: ClientError | None = None
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
        if self.create_errors:
            raise self.create_errors.pop(0)
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

    def admin_get_user(self, **request: Any) -> dict[str, object]:
        if self.get_user_error is not None:
            raise self.get_user_error
        value = "true" if self.email_verified else "false"
        return {"UserAttributes": [{"Name": "email_verified", "Value": value}]}

    def get_user_attribute_verification_code(self, **request: Any) -> dict[str, object]:
        self.code_requests.append(request)
        if self.code_error is not None:
            raise self.code_error
        return {}

    def verify_user_attribute(self, **request: Any) -> dict[str, object]:
        self.verify_requests.append(request)
        if self.verify_error is not None:
            raise self.verify_error
        return {}

    def forgot_password(self, **request: Any) -> dict[str, object]:
        self.forgot_requests.append(request)
        if self.forgot_error is not None:
            raise self.forgot_error
        return {}

    def confirm_forgot_password(self, **request: Any) -> dict[str, object]:
        self.confirm_requests.append(request)
        if self.confirm_error is not None:
            raise self.confirm_error
        return {}


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
    assert denied.value.status == 422
    # A subdomain is a different domain.
    with pytest.raises(auth.AuthRequestError):
        policy.validate("user@evil.amazon.com.attacker.net")
    with pytest.raises(auth.AuthRequestError):
        policy.validate("user@sub.amazon.com")
    with pytest.raises(ValueError, match="allowed email domain"):
        auth.EmailPolicy.from_environment(" , ")


def test_registration_creates_an_unverified_user_and_emails_a_code() -> None:
    subject, fake = service()
    subject.register("Dev@Amazon.com", "CorrectHorse#42")
    assert fake.created[0]["Username"] == "dev@amazon.com"
    assert fake.created[0]["MessageAction"] == "SUPPRESS"
    assert {"Name": "email_verified", "Value": "false"} in fake.created[0]["UserAttributes"]
    assert fake.passwords[0]["Permanent"] is True
    assert fake.deleted == []
    # The confirmation code is requested with the user's own fresh session.
    assert fake.code_requests[0] == {"AccessToken": "access", "AttributeName": "email"}

    with pytest.raises(auth.AuthRequestError) as bad_password:
        subject.register("dev@amazon.com", "short")
    assert bad_password.value.code == "INVALID_PASSWORD"

    fake.create_errors = [client_error("UsernameExistsException")]
    with pytest.raises(auth.AuthRequestError) as exists:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert exists.value.status == 409

    fake.create_errors = [client_error("InternalErrorException")]
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert unavailable.value.status == 502


def test_registration_replaces_an_unverified_squatter() -> None:
    subject, fake = service()
    fake.email_verified = False
    fake.create_errors = [client_error("UsernameExistsException")]
    subject.register("dev@amazon.com", "CorrectHorse#42")
    # The unproven claim on the address yields to the new registration.
    assert fake.deleted == ["dev@amazon.com"]
    assert len(fake.created) == 2
    assert fake.code_requests

    fake.create_errors = [
        client_error("UsernameExistsException"),
        client_error("InternalErrorException"),
    ]
    with pytest.raises(auth.AuthRequestError) as retry_failed:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert retry_failed.value.status == 502

    fake.create_errors = [client_error("UsernameExistsException")]
    fake.get_user_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as lookup_failed:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert lookup_failed.value.status == 502


def test_registration_maps_code_delivery_failures() -> None:
    subject, fake = service()
    fake.code_error = client_error("LimitExceededException")
    with pytest.raises(auth.AuthRequestError) as throttled:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert throttled.value.status == 429

    fake.code_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.register("dev@amazon.com", "CorrectHorse#42")
    assert unavailable.value.code == "RESET_UNAVAILABLE"


def test_confirm_verifies_the_code_and_signs_in() -> None:
    subject, fake = service()
    tokens = subject.confirm("Dev@Amazon.com", "CorrectHorse#42", " 123456 ")
    assert tokens.payload()["accessToken"] == "access"
    assert fake.verify_requests[0] == {
        "AccessToken": "access",
        "AttributeName": "email",
        "Code": "123456",
    }

    for invalid in (None, "", "x" * 65, 9):
        with pytest.raises(auth.AuthRequestError) as rejected:
            subject.confirm("dev@amazon.com", "CorrectHorse#42", invalid)
        assert rejected.value.code == "INVALID_CODE"

    for name in ("CodeMismatchException", "ExpiredCodeException"):
        fake.verify_error = client_error(name)
        with pytest.raises(auth.AuthRequestError) as mismatch:
            subject.confirm("dev@amazon.com", "CorrectHorse#42", "123456")
        assert mismatch.value.code == "INVALID_CODE"

    fake.verify_error = client_error("LimitExceededException")
    with pytest.raises(auth.AuthRequestError) as throttled:
        subject.confirm("dev@amazon.com", "CorrectHorse#42", "123456")
    assert throttled.value.status == 429

    fake.verify_error = None
    fake.auth_error = client_error("NotAuthorizedException")
    with pytest.raises(auth.AuthRequestError) as wrong:
        subject.confirm("dev@amazon.com", "CorrectHorse#42", "123456")
    assert wrong.value.status == 401


def test_resend_reissues_the_confirmation_code() -> None:
    subject, fake = service()
    subject.resend("Dev@Amazon.com", "CorrectHorse#42")
    assert fake.code_requests[0]["AttributeName"] == "email"

    fake.auth_error = client_error("NotAuthorizedException")
    with pytest.raises(auth.AuthRequestError) as wrong:
        subject.resend("dev@amazon.com", "CorrectHorse#42")
    assert wrong.value.status == 401


def test_login_requires_a_verified_email() -> None:
    subject, fake = service()
    fake.email_verified = False
    with pytest.raises(auth.AuthRequestError) as unverified:
        subject.login("dev@amazon.com", "CorrectHorse#42")
    assert unverified.value.status == 409
    assert unverified.value.code == "EMAIL_NOT_VERIFIED"


def test_registration_rolls_back_the_half_created_user_on_password_rejection() -> None:
    subject, fake = service()
    fake.password_error = client_error("InvalidPasswordException")
    with pytest.raises(auth.AuthRequestError) as error:
        subject.register("dev@amazon.com", "not-compliant-but-long")
    assert error.value.code == "INVALID_PASSWORD"
    assert fake.deleted == ["dev@amazon.com"]
    assert fake.code_requests == []  # no code for an account that failed


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


def test_email_patterns_admit_individual_exceptions() -> None:
    policy = auth.EmailPolicy.from_environment("amazon.com", json.dumps(["^cosintfs@qq\\.com$"]))
    assert policy.validate("Cosintfs@QQ.com") == "cosintfs@qq.com"
    assert policy.validate("dev@amazon.com") == "dev@amazon.com"
    # The pattern is anchored to the full address: neighbours stay excluded.
    for excluded in ("other@qq.com", "cosintfs@qq.com.evil.net", "xcosintfs@qq.com"):
        with pytest.raises(auth.AuthRequestError) as denied:
            policy.validate(excluded)
        assert denied.value.code == "DOMAIN_NOT_ALLOWED"
    # Blank patterns are ignored; malformed configuration fails loudly.
    empty = auth.EmailPolicy.from_environment("amazon.com", json.dumps([" "]))
    assert empty.allowed_patterns == ()
    for malformed in ('"nope"', "[1]"):
        with pytest.raises(ValueError, match="JSON list"):
            auth.EmailPolicy.from_environment("amazon.com", malformed)


def test_forgot_password_is_silent_about_account_existence() -> None:
    subject, fake = service()
    subject.forgot_password("Dev@Amazon.com")
    assert fake.forgot_requests[0] == {"ClientId": CLIENT, "Username": "dev@amazon.com"}

    for hidden in ("UserNotFoundException", "NotAuthorizedException"):
        fake.forgot_error = client_error(hidden)
        subject.forgot_password("dev@amazon.com")  # answers like success

    fake.forgot_error = client_error("LimitExceededException")
    with pytest.raises(auth.AuthRequestError) as throttled:
        subject.forgot_password("dev@amazon.com")
    assert throttled.value.status == 429

    fake.forgot_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.forgot_password("dev@amazon.com")
    assert unavailable.value.code == "RESET_UNAVAILABLE"

    with pytest.raises(auth.AuthRequestError) as denied:
        subject.forgot_password("dev@other.com")
    assert denied.value.code == "DOMAIN_NOT_ALLOWED"


def test_reset_password_confirms_the_code_and_maps_failures() -> None:
    subject, fake = service()
    subject.reset_password("Dev@Amazon.com", " 123456 ", "CorrectHorse#42")
    assert fake.confirm_requests[0] == {
        "ClientId": CLIENT,
        "Username": "dev@amazon.com",
        "ConfirmationCode": "123456",
        "Password": "CorrectHorse#42",
    }

    for invalid_code in (None, "", "x" * 65, 7):
        with pytest.raises(auth.AuthRequestError) as rejected:
            subject.reset_password("dev@amazon.com", invalid_code, "CorrectHorse#42")
        assert rejected.value.code == "INVALID_CODE"

    messages = set()
    for hidden in ("CodeMismatchException", "ExpiredCodeException", "UserNotFoundException"):
        fake.confirm_error = client_error(hidden)
        with pytest.raises(auth.AuthRequestError) as mismatch:
            subject.reset_password("dev@amazon.com", "123456", "CorrectHorse#42")
        assert mismatch.value.code == "INVALID_CODE"
        messages.add(str(mismatch.value))
    assert len(messages) == 1  # unknown accounts are indistinguishable

    fake.confirm_error = client_error("InvalidPasswordException")
    with pytest.raises(auth.AuthRequestError) as weak:
        subject.reset_password("dev@amazon.com", "123456", "not-compliant-but-long")
    assert weak.value.code == "INVALID_PASSWORD"

    fake.confirm_error = client_error("TooManyFailedAttemptsException")
    with pytest.raises(auth.AuthRequestError) as throttled:
        subject.reset_password("dev@amazon.com", "123456", "CorrectHorse#42")
    assert throttled.value.status == 429

    fake.confirm_error = client_error("InternalErrorException")
    with pytest.raises(auth.AuthRequestError) as unavailable:
        subject.reset_password("dev@amazon.com", "123456", "CorrectHorse#42")
    assert unavailable.value.status == 502

    with pytest.raises(auth.AuthRequestError):
        subject.reset_password("dev@amazon.com", "123456", "short")


def test_handler_routes_requests_and_maps_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeCognito()
    for name, value in {
        "REGION": "us-east-2",
        "USER_POOL_ID": POOL,
        "APP_CLIENT_ID": CLIENT,
        "ALLOWED_EMAIL_DOMAINS": "amazon.com",
        "ALLOWED_EMAIL_PATTERNS": '["^cosintfs@qq\\\\.com$"]',
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
    assert json.loads(cast_str(created["body"]))["confirmationRequired"] is True
    assert created["headers"]["cache-control"] == "no-store"  # type: ignore[index]

    confirmed = auth.handler(
        event(
            "POST /auth/v1/confirm",
            {"email": "dev@amazon.com", "password": "Horse#4242", "code": "123456"},
        ),
        object(),
    )
    assert confirmed["statusCode"] == 200
    assert json.loads(cast_str(confirmed["body"]))["accessToken"] == "access"

    resent = auth.handler(
        event("POST /auth/v1/resend", {"email": "dev@amazon.com", "password": "Horse#4242"}),
        object(),
    )
    assert resent["statusCode"] == 200

    logged_in = auth.handler(
        event("POST /auth/v1/login", {"email": "dev@amazon.com", "password": "Horse#4242"}),
        object(),
    )
    assert logged_in["statusCode"] == 200
    assert json.loads(cast_str(logged_in["body"]))["accessToken"] == "access"

    refreshed = auth.handler(event("POST /auth/v1/refresh", {"refreshToken": "refresh"}), object())
    assert refreshed["statusCode"] == 200

    sent = auth.handler(event("POST /auth/v1/forgot", {"email": "dev@amazon.com"}), object())
    assert sent["statusCode"] == 200
    assert json.loads(cast_str(sent["body"])) == {"sent": True}

    reset = auth.handler(
        event(
            "POST /auth/v1/reset",
            {"email": "dev@amazon.com", "code": "123456", "password": "Horse#4242"},
        ),
        object(),
    )
    assert reset["statusCode"] == 200
    assert json.loads(cast_str(reset["body"])) == {"reset": True}

    # The exception pattern from ALLOWED_EMAIL_PATTERNS admits the address.
    exception = auth.handler(event("POST /auth/v1/forgot", {"email": "cosintfs@qq.com"}), object())
    assert exception["statusCode"] == 200

    unknown = auth.handler(event("GET /auth/v1/unknown", {}), object())
    assert unknown["statusCode"] == 404

    denied = auth.handler(
        event("POST /auth/v1/login", {"email": "dev@other.com", "password": "Horse#4242"}),
        object(),
    )
    assert denied["statusCode"] == 422
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
