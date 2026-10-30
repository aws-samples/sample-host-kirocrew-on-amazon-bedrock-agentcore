from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SOURCE = Path(__file__).parents[2] / "infrastructure/modules/tool-authorization/src/token_claims.py"
SCOPE = "kirocrew.tools/invoke"


def _load(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TOOL_SCOPE", SCOPE)
    spec = importlib.util.spec_from_file_location("token_claims", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NotFoundError(Exception):
    pass


class _Exceptions:
    UserNotFoundException = _NotFoundError


class _FakeCognito:
    exceptions = _Exceptions

    def __init__(self, user: dict[str, Any] | None, groups: list[str]) -> None:
        self.user = user
        self.groups = groups
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def admin_get_user(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_get_user", kwargs))
        if self.user is None:
            raise _NotFoundError()
        return self.user

    def admin_list_groups_for_user(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("admin_list_groups_for_user", kwargs))
        return {"Groups": [{"GroupName": name} for name in self.groups]}


def _event(source: str = "TokenGeneration_ClientCredentials", **request: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"scopes": [SCOPE], "clientMetadata": {"owner_sub": "sub-1"}}
    base.update(request)
    return {"triggerSource": source, "userPoolId": "pool-1", "request": base, "response": {}}


ACTIVE = {"Enabled": True, "UserStatus": "CONFIRMED"}


def test_tool_token_carries_owner_and_current_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load(monkeypatch)
    fake = _FakeCognito(ACTIVE, ["team-b", "team-a"])
    monkeypatch.setattr(module, "_cognito", fake)

    out = module.handler(_event(), None)

    claims = out["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]
    assert claims["claimsToAddOrOverride"] == {
        "kirocrew_owner": "sub-1",
        "kirocrew_groups": ["team-a", "team-b"],
    }
    assert fake.calls[0] == ("admin_get_user", {"UserPoolId": "pool-1", "Username": "sub-1"})


@pytest.mark.parametrize(
    "event",
    [
        _event(source="TokenGeneration_Authentication"),
        _event(source="TokenGeneration_RefreshTokens"),
        _event(scopes=["other/scope"]),
    ],
)
def test_other_token_requests_pass_through_untouched(
    monkeypatch: pytest.MonkeyPatch, event: dict[str, Any]
) -> None:
    module = _load(monkeypatch)
    fake = _FakeCognito(ACTIVE, ["team-a"])
    monkeypatch.setattr(module, "_cognito", fake)

    assert module.handler(event, None)["response"] == {}
    assert fake.calls == []


@pytest.mark.parametrize(
    "metadata", [{}, {"owner_sub": ""}, {"owner_sub": 7}, {"owner_sub": "x" * 129}]
)
def test_tool_token_without_a_valid_owner_is_refused(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, Any]
) -> None:
    module = _load(monkeypatch)
    monkeypatch.setattr(module, "_cognito", _FakeCognito(ACTIVE, []))
    with pytest.raises(module.ToolTokenRefusedError):
        module.handler(_event(clientMetadata=metadata), None)


@pytest.mark.parametrize(
    "user",
    [
        None,
        {"Enabled": False, "UserStatus": "CONFIRMED"},
        {"Enabled": True, "UserStatus": "FORCE_CHANGE_PASSWORD"},
    ],
)
def test_missing_disabled_or_unconfirmed_owner_gets_no_token(
    monkeypatch: pytest.MonkeyPatch, user: dict[str, Any] | None
) -> None:
    module = _load(monkeypatch)
    fake = _FakeCognito(user, ["team-a"])
    monkeypatch.setattr(module, "_cognito", fake)
    with pytest.raises(module.ToolTokenRefusedError):
        module.handler(_event(), None)
    assert all(name != "admin_list_groups_for_user" for name, _ in fake.calls)
