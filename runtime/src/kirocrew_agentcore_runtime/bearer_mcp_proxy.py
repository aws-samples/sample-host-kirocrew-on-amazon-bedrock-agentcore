"""A stdio MCP server that forwards to the tool Gateway with the owner's bearer token.

The tool Gateway authenticates with a Cognito access token that names the sandbox
owner. The runtime obtains one from the persistence broker and keeps a fresh copy
in a file; this bridge, started by KiroCrew as a local ``command`` MCP server,
reads that file for every request so a token refreshed mid-session is picked up
without restarting anything.

Like the SigV4 bridge it is a dumb pipe: newline-delimited JSON-RPC in on stdin,
one HTTPS POST per message, the response body out on stdout. It keeps the
``Mcp-Session-Id`` the Gateway hands back and sends it on later requests.

Usage::

    python -m kirocrew_agentcore_runtime.bearer_mcp_proxy \\
        --endpoint <gateway-mcp-url> --token-file <path>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kirocrew_agentcore_runtime.sigv4_mcp_proxy import error_response

_SESSION_HEADER = "Mcp-Session-Id"
_MAX_TOKEN_BYTES = 16384


def log(message: str) -> None:
    """Diagnostics go to stderr; stdout carries the JSON-RPC stream."""
    print(f"[tool-gateway-mcp] {message}", file=sys.stderr, flush=True)


class BearerForwarder:
    def __init__(self, endpoint: str, token_file: Path) -> None:
        parts = urlsplit(endpoint)
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError(f"Endpoint must be an https URL with a host: {endpoint!r}")
        self._endpoint = endpoint
        self._token_file = token_file
        self._session_id: str | None = None

    def _token(self) -> str:
        try:
            raw = self._token_file.read_bytes()[:_MAX_TOKEN_BYTES]
        except OSError as error:
            raise RuntimeError(
                "The tool token is not available yet; the sandbox fetches it after start."
            ) from error
        token = raw.decode("ascii", "replace").strip()
        if not token or any(character.isspace() for character in token):
            raise RuntimeError("The tool token file is empty or malformed.")
        return token

    def forward(self, payload: bytes) -> bytes:
        headers = {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers[_SESSION_HEADER] = self._session_id
        request = urllib.request.Request(  # noqa: S310 - https enforced in __init__
            self._endpoint, data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310  # nosec B310 - https enforced in __init__.
                session = response.headers.get(_SESSION_HEADER)
                if session:
                    self._session_id = session
                return bytes(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:400]
            hint = (
                " (401 means the tool token was rejected; it is refreshed every few minutes)"
                if error.code == 401
                else ""
            )
            raise RuntimeError(f"HTTP {error.code} from the tool Gateway{hint}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cannot reach the tool Gateway: {error.reason}") from error


def serve(forwarder: BearerForwarder, stdin: Any, stdout: Any) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request_id = json.loads(line).get("id")
        except (json.JSONDecodeError, AttributeError):
            pass
        try:
            reply = forwarder.forward(line.encode()).decode("utf-8", "replace").strip()
            # A notification is answered with 202 and no body; writing an empty
            # line would hand the client a message that is not JSON.
            if reply:
                stdout.write(reply + "\n")
        except Exception as error:  # every request must be answered
            message = str(error)
            log(message)
            if request_id is not None:
                stdout.write(error_response(request_id, message) + "\n")
        stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Forward MCP JSON-RPC to the tool Gateway.")
    parser.add_argument("--endpoint", required=True, help="Tool Gateway MCP URL.")
    parser.add_argument("--token-file", required=True, help="File holding the bearer token.")
    args = parser.parse_args(argv)
    serve(BearerForwarder(args.endpoint, Path(args.token_file)), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
