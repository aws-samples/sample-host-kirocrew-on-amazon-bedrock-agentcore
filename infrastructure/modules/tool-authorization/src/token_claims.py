"""Cognito pre token generation trigger (event version V3_0) for tool tokens.

Phase 2 of the tool-authorization design: the persistence broker asks Cognito
for a client-credentials token on behalf of a sandbox owner, naming the owner's
Cognito subject in ``aws_client_metadata``. This trigger turns that request
into a token the tool Gateway's Cedar policies can judge: it adds the owner and
the owner's CURRENT groups as claims.

Every other token request passes through untouched, so ordinary sign-ins are
unaffected. A tool-token request for an owner that is missing, disabled or not
confirmed raises, which makes Cognito refuse the token instead of issuing one
without groups.
"""

from __future__ import annotations

import os
from typing import Any

import boto3  # type: ignore[import-untyped]

CLIENT_CREDENTIALS = "TokenGeneration_ClientCredentials"
OWNER_KEY = "owner_sub"
OWNER_CLAIM = "kirocrew_owner"
GROUPS_CLAIM = "kirocrew_groups"
_MAX_GROUPS = 50

_cognito: Any = None


def _client() -> Any:
    global _cognito
    if _cognito is None:
        _cognito = boto3.client("cognito-idp")
    return _cognito


class ToolTokenRefusedError(Exception):
    """The named owner may not receive a tool token."""


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if event.get("triggerSource") != CLIENT_CREDENTIALS:
        return event
    request = event.get("request") or {}
    scope = os.environ["TOOL_SCOPE"]
    if scope not in (request.get("scopes") or []):
        return event

    owner = (request.get("clientMetadata") or {}).get(OWNER_KEY)
    if not isinstance(owner, str) or not owner or len(owner) > 128:
        raise ToolTokenRefusedError("a tool token must name its owner")

    pool = event["userPoolId"]
    cognito = _client()
    try:
        user = cognito.admin_get_user(UserPoolId=pool, Username=owner)
    except cognito.exceptions.UserNotFoundException as exc:
        raise ToolTokenRefusedError("unknown owner") from exc
    if not user.get("Enabled", False) or user.get("UserStatus") != "CONFIRMED":
        raise ToolTokenRefusedError("owner is not active")

    groups = cognito.admin_list_groups_for_user(UserPoolId=pool, Username=owner, Limit=_MAX_GROUPS)
    names = sorted(group["GroupName"] for group in groups.get("Groups", []))

    event["response"] = {
        "claimsAndScopeOverrideDetails": {
            "accessTokenGeneration": {
                "claimsToAddOrOverride": {OWNER_CLAIM: owner, GROUPS_CLAIM: names},
            }
        }
    }
    return event
