from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
from kirocrew_agentcore_adapter.loopback import KiroCrewRoutePolicy, RouteDisposition

ROOT = Path(__file__).parents[2]
VERSION = "0.5.0"
ASSET_ROOT = ROOT / "frontend-shell" / "upstream" / VERSION
CONTRACT_PATH = ROOT / "frontend-shell" / "upstream-contracts" / f"{VERSION}.json"
API_LITERAL = re.compile(rb"/api/[A-Za-z0-9_.?=&/$-]+")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def blocked_prefixes(contract: dict[str, Any]) -> tuple[str, ...]:
    return tuple(contract["unavailableRouteFamilies"] + contract["deniedRouteFamilies"])


@pytest.mark.contract
def test_real_upstream_spa_assets_are_pinned_complete_and_bootstrap_first() -> None:
    artifact = load_json(ROOT / "runtime" / "kirocrew-artifact.json")
    contract = load_json(CONTRACT_PATH)
    manifest = load_json(ASSET_ROOT / "asset-manifest.json")

    assert contract["kiroCrew"] == {
        "version": artifact["version"],
        "artifactSha256": artifact["sha256"],
    }
    assert manifest["upstream"]["version"] == VERSION
    assert manifest["upstream"]["artifactSha256"] == artifact["sha256"]
    assert manifest["assetSetSha256"] == contract["assetIntegrity"]["assetSetSha256"]
    assert manifest["bootstrap"]["loadsBeforeUpstreamBundle"] is True

    records = manifest["assets"]
    assert records
    assert [record["path"] for record in records] == sorted(record["path"] for record in records)
    for record in records:
        path = ASSET_ROOT / record["path"]
        assert path.is_file()
        assert path.stat().st_size == record["bytes"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"]
        assert path.suffix not in {".jsx", ".map", ".ts", ".tsx"}

    suffixes = {path.suffix for path in ASSET_ROOT.rglob("*") if path.is_file()}
    assert {".html", ".js", ".css"} <= suffixes
    assert suffixes & {".woff", ".woff2", ".ttf"}
    assert suffixes & {".ico", ".jpg", ".png", ".svg"}
    index = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
    assert index.index("/bootstrap.js") < index.index('<script type="module" crossorigin src=')


@pytest.mark.contract
def test_setup_extracts_the_upstream_assets_this_suite_reads() -> None:
    # Every test in this file reads ASSET_ROOT, which is gitignored and produced by
    # `make frontend-assets`. The documented bootstrap is `make setup`, so setup must
    # wire that extraction in; otherwise a clean checkout fails `make verify` at
    # frontend-assets-check and this whole suite never executes.
    lines = (ROOT / "Makefile").read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.startswith("setup:"))
    recipe = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    assert any("frontend-assets" in line for line in recipe), recipe


@pytest.mark.contract
def test_upstream_network_assumptions_are_recorded_and_adapter_closed() -> None:
    contract = load_json(CONTRACT_PATH)
    blocked = blocked_prefixes(contract)
    observed: set[str] = set()
    compiled = bytearray()
    for path in ASSET_ROOT.rglob("*"):
        if path.suffix not in {".html", ".js", ".mjs"}:
            continue
        content = path.read_bytes()
        compiled.extend(content)
        observed.update(match.decode("utf-8") for match in API_LITERAL.findall(content))

    observed_routes = sorted(observed)
    observed_digest = hashlib.sha256(("\n".join(observed_routes) + "\n").encode()).hexdigest()
    expected_digest = contract.get("compiledNetworkContract", {}).get("observedApiLiteralsSha256")
    assert expected_digest == observed_digest, (
        "Upstream API literals changed; review every route and record the new digest: "
        f"{observed_digest}"
    )
    # Every route the shipped bundle calls must reach upstream. A route may only
    # fail to tunnel when it is explicitly recorded as denied or unavailable, so
    # a narrowing of the policy shows up here instead of as a 403 in the product.
    policy = KiroCrewRoutePolicy()
    unreachable = [
        route
        for route in observed_routes
        if not route.startswith(blocked)
        and policy.classify("GET", route)
        not in {RouteDisposition.ALLOWED, RouteDisposition.SYNTHETIC}
    ]
    assert unreachable == [], (
        "These upstream routes would not reach KiroCrew; allow them or record why "
        f"they are blocked: {unreachable}"
    )
    assert contract["compiledNetworkContract"]["unlistedRouteDisposition"] == "allowed"
    assert b"new WebTransport(" not in compiled
    assert b"new RTCDataChannel(" not in compiled
    assert contract["supportedStreaming"] == ["fetch-stream", "sse", "websocket"]
    assert contract["browserAuthority"]["browserVisibleKiroCrewToken"] is False
    assert contract["browserAuthority"]["browserKiroCrewTokenStorageKeys"] == []

    for entry in contract["allowedRouteFamilies"]:
        for method in entry["methods"]:
            assert policy.classify(method, entry["path"]) is RouteDisposition.ALLOWED
    for route in contract["syntheticAuthRoutes"]:
        method, path = route.split(" ", 1)
        assert policy.classify(method, path) is RouteDisposition.SYNTHETIC
    for path in contract["unavailableRouteFamilies"]:
        assert policy.classify("GET", path) is RouteDisposition.UNAVAILABLE
    for path in contract["deniedRouteFamilies"]:
        assert policy.classify("GET", path) is RouteDisposition.DENIED
