"""A stdio MCP server that signs with AWS SigV4 and forwards to a remote MCP endpoint.

Kiro CLI speaks two MCP transports: a local ``command`` over stdio, and a remote
``url`` authenticated with OAuth or a bearer token. AWS-native MCP endpoints such as
AWS Agent Registry authenticate with SigV4 request signing instead, so Kiro cannot
reach them directly. This bridge runs next to KiroCrew inside the sandbox and signs
with the AgentCore Runtime execution role, so no user credential is copied into the
runtime.

It reads newline-delimited JSON-RPC from stdin, signs each request with SigV4, POSTs
it to the configured endpoint, and writes the response to stdout. It is deliberately
a dumb pipe: it does not interpret methods, cache tool lists, or rewrite payloads.

Credentials are resolved per request rather than frozen at startup, because the
runtime role's credentials rotate while a session stays open.

Usage::

    python -m kirocrew_agentcore_runtime.sigv4_mcp_proxy \
        --endpoint <mcp-url> --service agent-registry

The region is taken from the endpoint host when not given.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

import boto3  # type: ignore[import-untyped]
from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]

#: A region label anywhere in the host, matched on its own dot-delimited segment.
#:
#: The obvious form is ``service.region.api.aws`` / ``service.region.amazonaws.com``,
#: where the region is the second label. But an AgentCore Gateway is
#: ``<gateway-id>.gateway.bedrock-agentcore.<region>.amazonaws.com`` -- fourth
#: label -- so anchoring to the second position silently fails on a real endpoint.
#: This scans every segment instead. Matching the whole segment (rather than a
#: substring) is what keeps a name like ``eu-west-1-backup`` from being read as a
#: region, and requiring a known partition prefix keeps an arbitrary
#: ``word-word-digit`` subdomain out.
#:
#: If more than one segment looks like a region the endpoint is ambiguous and no
#: derivation is attempted: a guess that picks the wrong one produces a 403 that
#: reads as a permissions problem, which is the most expensive failure here.
_REGION_SEGMENT = re.compile(r"^(?:us|eu|ap|sa|ca|me|af|il|mx|cn)-[a-z]+-\d+$")

#: JSON-RPC reserved codes. -32603 is "internal error", which is the honest code for
#: "the proxy could not complete the call" -- the failure is in the transport the
#: client cannot see, not in the request the client sent.
_INTERNAL_ERROR = -32603


def region_from_endpoint(endpoint: str) -> str:
    """The AWS region in an endpoint host, or "" when it cannot be read unambiguously.

    Returns "" for both "no region-shaped segment" and "more than one", because the
    caller's response to either is the same: pass --region rather than let the proxy
    pick. A wrong signing region is indistinguishable from a permissions failure at
    the other end, so declining to guess is the cheaper outcome.
    """
    host = urlsplit(endpoint).hostname or ""
    found = {segment for segment in host.split(".") if _REGION_SEGMENT.match(segment)}
    return found.pop() if len(found) == 1 else ""


def log(message: str) -> None:
    """Diagnostics go to stderr.

    stdout carries the JSON-RPC stream, so a stray print there corrupts the protocol
    and the client reports a malformed message rather than the problem being logged.
    """
    print(f"[sigv4-mcp-proxy] {message}", file=sys.stderr, flush=True)


class SigningForwarder:
    def __init__(self, endpoint: str, service: str, region: str, profile: str = "") -> None:
        self._endpoint = endpoint
        self._service = service
        self._region = region
        self._session = boto3.Session(profile_name=profile) if profile else boto3.Session()

    def forward(self, payload: bytes) -> bytes:
        credentials = self._session.get_credentials()
        if credentials is None:
            raise RuntimeError(
                "No AWS credentials found. The proxy signs with the ambient chain "
                "(environment, shared config/profile, SSO, container or instance role); "
                "none of those resolved."
            )
        request = AWSRequest(
            method="POST",
            url=self._endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        # Freeze immediately before signing: a frozen credential taken earlier may
        # have expired by now, and the signature would be valid over a stale key.
        SigV4Auth(credentials.get_frozen_credentials(), self._service, self._region).add_auth(
            request
        )
        prepared = request.prepare()
        http = urllib.request.Request(  # noqa: S310 - caller-configured https endpoint
            prepared.url,
            data=prepared.body,
            headers=dict(prepared.headers),
            method="POST",
        )
        try:
            with urllib.request.urlopen(http, timeout=120) as response:  # noqa: S310
                return bytes(response.read())
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", "replace")[:400]
            # 403 is reported distinctly because the two causes need different fixes:
            # a rejected signature means --service or --region is wrong, while an
            # authorization denial means the identity lacks the IAM permission.
            hint = (
                " (a 403 here is either a bad --service/--region for signing, or an "
                "IAM denial for this identity -- the response body distinguishes them)"
                if error.code == 403
                else ""
            )
            raise RuntimeError(f"HTTP {error.code} from {self._endpoint}{hint}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Cannot reach {self._endpoint}: {error.reason}") from error


def error_response(request_id: Any, message: str) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": _INTERNAL_ERROR, "message": message},
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign MCP JSON-RPC with SigV4 and forward it to a remote endpoint."
    )
    parser.add_argument("--endpoint", required=True, help="Remote MCP endpoint URL.")
    parser.add_argument(
        "--service",
        required=True,
        help="SigV4 signing service name, e.g. agent-registry. Required rather than "
        "guessed: the signing name is not always the first label of the hostname.",
    )
    parser.add_argument("--region", default="", help="Signing region. Default: from the endpoint.")
    parser.add_argument("--profile", default="", help="AWS profile. Default: ambient chain.")
    args = parser.parse_args()

    region = args.region or region_from_endpoint(args.endpoint)
    if not region:
        log(f"Could not determine a signing region from {args.endpoint!r}. Pass --region.")
        return 2

    forwarder = SigningForwarder(args.endpoint, args.service, region, args.profile)
    log(f"signing as {args.service} in {region} → {args.endpoint}")

    # One JSON-RPC message per line, which is what the stdio transport specifies.
    # Lines are forwarded verbatim; the proxy never parses the method, so a protocol
    # version or method it has never heard of passes through unchanged.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = None
        try:
            request_id = json.loads(line).get("id")
        except json.JSONDecodeError:
            # Malformed input is still forwarded: the remote server owns protocol
            # validation, and a proxy that rejected it first would hide a real
            # server-side error message behind its own.
            pass
        try:
            reply = forwarder.forward(line.encode())
            sys.stdout.write(reply.decode("utf-8", "replace").rstrip("\n") + "\n")
        except Exception as error:  # every failure must answer the client
            # A JSON-RPC notification has no id and expects no reply; answering one
            # would desynchronise the stream. Log and move on.
            message = str(error)
            log(message)
            if request_id is not None:
                sys.stdout.write(error_response(request_id, message) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
