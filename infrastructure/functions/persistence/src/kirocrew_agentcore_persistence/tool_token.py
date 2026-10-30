"""Tool-Gateway tokens issued to a sandbox on behalf of its owner.

The sandbox cannot obtain a user token on its own: an interactive browser token
expires, and a scheduled wake has none. The broker already knows which sandbox
is calling (it verified the binding token), so it can name that sandbox's owner
to Cognito with a client-credentials request. The user pool's pre token
generation trigger then checks the owner is active and adds their current
groups, which the tool Gateway's Cedar policies judge.

The owner comes from the record the control plane wrote while the owner was
signed in (the KMS-sealed ``wakeSubject``), checked against the record's
``ownerHash``. Nothing the sandbox sends selects the owner.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Final, cast

from botocore.exceptions import ClientError  # type: ignore[import-untyped]

_WAKE_SUBJECT_PURPOSE: Final = "kirocrew-wake-subject"
_OWNER_HASH_PREFIX: Final = "kirocrew-owner-v1"
_TOKEN_TIMEOUT_SECONDS: Final = 10
_CONFIG_KEYS: Final = ("clientId", "clientSecret", "tokenEndpoint", "scope")

_config_cache: dict[str, str] | None = None

Opener = Callable[..., Any]


class ToolTokenUnavailableError(PermissionError):
    """This sandbox cannot be given a tool token."""


def enabled() -> bool:
    return bool(os.environ.get("TOOL_TOKEN_SECRET_ARN"))


def _config(secrets: Any) -> dict[str, str]:
    global _config_cache
    if _config_cache is None:
        value = secrets.get_secret_value(SecretId=os.environ["TOOL_TOKEN_SECRET_ARN"])
        loaded = cast(object, json.loads(value["SecretString"]))
        if not isinstance(loaded, dict) or not all(
            isinstance(loaded.get(key), str) and loaded.get(key) for key in _CONFIG_KEYS
        ):
            raise RuntimeError("Tool token configuration is incomplete.")
        if not str(loaded["tokenEndpoint"]).startswith("https://"):
            raise RuntimeError("Tool token endpoint must use HTTPS.")
        _config_cache = {key: str(loaded[key]) for key in _CONFIG_KEYS}
    return _config_cache


def owner_subject(kms: Any, record: Mapping[str, Mapping[str, str]], sandbox_id: str) -> str:
    """Unseal the owner's Cognito subject and prove it belongs to this record."""
    sealed = record.get("wakeSubject", {}).get("S", "")
    owner_hash = record.get("ownerHash", {}).get("S", "")
    if not sealed or not owner_hash:
        raise ToolTokenUnavailableError("Sandbox owner is not enrolled for tool tokens.")
    try:
        plaintext = kms.decrypt(
            CiphertextBlob=base64.b64decode(sealed, validate=True),
            KeyId=os.environ["WAKE_SUBJECT_KEY_ARN"],
            EncryptionContext={"purpose": _WAKE_SUBJECT_PURPOSE, "sandboxId": sandbox_id},
        ).get("Plaintext")
    except (binascii.Error, ClientError) as error:
        raise ToolTokenUnavailableError("Sandbox owner record does not open.") from error
    if not isinstance(plaintext, bytes) or not plaintext:
        raise ToolTokenUnavailableError("Sandbox owner record is empty.")
    subject = plaintext.decode("utf-8")
    derived = hashlib.sha256(f"{_OWNER_HASH_PREFIX}\x00{subject}".encode()).hexdigest()
    if not hmac.compare_digest(derived, owner_hash):
        raise ToolTokenUnavailableError("Sandbox owner record does not match its owner.")
    return subject


def issue(
    kms: Any,
    dynamodb: Any,
    secrets: Any,
    sandbox_id: str,
    *,
    opener: Opener = urllib.request.urlopen,
) -> dict[str, object]:
    if not enabled():
        raise ToolTokenUnavailableError("Tool tokens are not enabled.")
    item = cast(
        Mapping[str, Mapping[str, str]],
        dynamodb.get_item(
            TableName=os.environ["SANDBOX_TABLE"],
            Key={"pk": {"S": f"SANDBOX#{sandbox_id}"}, "sk": {"S": "METADATA"}},
            ConsistentRead=True,
            ProjectionExpression="wakeSubject, ownerHash",
        ).get("Item", {}),
    )
    subject = owner_subject(kms, item, sandbox_id)
    config = _config(secrets)

    credentials = base64.b64encode(
        f"{config['clientId']}:{config['clientSecret']}".encode()
    ).decode()
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "scope": config["scope"],
            "aws_client_metadata": json.dumps({"owner_sub": subject}, separators=(",", ":")),
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 - https enforced in _config
        config["tokenEndpoint"],
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        with opener(request, timeout=_TOKEN_TIMEOUT_SECONDS) as response:
            payload = cast(object, json.loads(response.read()))
    except (OSError, ValueError) as error:
        # An HTTPError (a refused owner) is an OSError; its body can name the
        # owner and is not returned to the sandbox.
        raise ToolTokenUnavailableError("Tool token was refused.") from error
    if not isinstance(payload, dict):
        raise ToolTokenUnavailableError("Tool token response is invalid.")
    token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(token, str) or not token or type(expires_in) is not int:
        raise ToolTokenUnavailableError("Tool token response is invalid.")
    return {"accessToken": token, "expiresIn": expires_in, "tokenType": "Bearer"}
