#!/usr/bin/env python3
"""Move the pinned KiroCrew release to a new version: the mechanical part.

    uv run python tools/upgrade_kirocrew.py 0.13.1

An upstream release is pinned in about a dozen places: the artifact record,
the wheel URL, the Dockerfile arguments, the adapter's route-policy version,
the Terraform defaults, two versioned contract files, the tests that pin
them, the docs that name the contract file, and the lock digest. This tool
rewrites all of them, relocks, re-extracts the compiled SPA and records the
new asset set. It does not decide anything a person must review:

- New or removed `/api/*` literals in the compiled bundle are printed. The
  contract's literal digest is only written with ``--accept-routes``, after
  each new route was checked against the deny list (`loopback.py`).
- Behaviour changes in the release (config keys the supervisor pins, new
  host-native features) still need reading the upstream release notes.

Run it on a clean branch; it refuses to run over local changes. Then run
`make verify`, build the image, and deploy the runtime, `module.frontend`
and `module.control_api` together (docs/operations.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess  # nosec B404 - runs uv and git with fixed argv
import sys
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parents[1]
ARTIFACT: Final = Path("runtime/kirocrew-artifact.json")
WHEEL_URL: Final = "https://download.crew.kiro.dev/cli/stable/{v}/kirocrew-{v}-py3-none-any.whl"
RELEASE_URL: Final = "https://github.com/kirodotdev/KiroCrew/releases/tag/v{v}"
_VERSION: Final = re.compile(r"\d+\.\d+\.\d+")
_API_LITERAL: Final = re.compile(rb"/api/[A-Za-z0-9_.?=&/$-]+")

#: Files where the old version string and the old wheel digest are replaced
#: wholesale. Each is a pin, a test of a pin, or prose naming a pinned file.
VERSIONED_FILES: Final = (
    "README.md",
    "README.zh-CN.md",
    "docs/security.md",
    "docs/security.zh-CN.md",
    "adapter/src/kirocrew_agentcore_adapter/loopback.py",
    "infrastructure/variables.tf",
    "runtime/Dockerfile",
    "runtime/kirocrew-artifact.json",
    "runtime/pyproject.toml",
    "tests/contract/test_frontend_spa_contract.py",
    "tests/contract/test_kirocrew_loopback_contract.py",
    "tests/unit/test_adapter_loopback.py",
)


@dataclass(frozen=True, slots=True)
class Pin:
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RouteChange:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    digest: str
    count: int


def current_pin(root: Path) -> Pin:
    value = json.loads((root / ARTIFACT).read_text(encoding="utf-8"))
    return Pin(str(value["version"]), str(value["sha256"]))


def rewrite_pins(root: Path, old: Pin, new: Pin) -> list[str]:
    """Replace the old version and digest in every versioned file; return those changed.

    Version strings are replaced only where they stand alone (not inside a
    longer version such as ``10.13.0``), so unrelated numbers survive.
    """
    pattern = re.compile(rf"(?<![0-9.]){re.escape(old.version)}(?![0-9])")
    changed: list[str] = []
    for name in VERSIONED_FILES:
        path = root / name
        text = path.read_text(encoding="utf-8")
        updated = pattern.sub(new.version, text.replace(old.sha256, new.sha256))
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.append(name)
    return changed


def move_contracts(root: Path, old: Pin, new: Pin, move: Callable[[Path, Path], None]) -> None:
    """Carry both versioned contracts forward under the new version's name."""
    allowlist_old = root / "contracts/kirocrew" / f"{old.version}-route-allowlist.json"
    allowlist_new = allowlist_old.with_name(f"{new.version}-route-allowlist.json")
    move(allowlist_old, allowlist_new)
    allowlist = json.loads(allowlist_new.read_text(encoding="utf-8"))
    allowlist["kirocrewVersion"] = new.version
    _write_json(allowlist_new, allowlist)

    spa_old = root / "frontend-shell/upstream-contracts" / f"{old.version}.json"
    spa_new = spa_old.with_name(f"{new.version}.json")
    move(spa_old, spa_new)
    spa = json.loads(spa_new.read_text(encoding="utf-8"))
    spa["kiroCrew"] = {"version": new.version, "artifactSha256": new.sha256}
    spa["assetIntegrity"]["manifest"] = f"/upstream/{new.version}/asset-manifest.json"
    _write_json(spa_new, spa)


def record_lock_digest(root: Path) -> str:
    """Write uv.lock's sha256 into the Dockerfile's two DEPENDENCY_LOCK_SHA256 args."""
    digest = hashlib.sha256((root / "uv.lock").read_bytes()).hexdigest()
    dockerfile = root / "runtime/Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    dockerfile.write_text(
        re.sub(r"(ARG DEPENDENCY_LOCK_SHA256=)[0-9a-f]{64}", rf"\g<1>{digest}", text),
        encoding="utf-8",
    )
    return digest


def api_literals(asset_root: Path) -> set[str]:
    """The `/api/*` literals the compiled bundle calls (same scan as the SPA contract test)."""
    found: set[str] = set()
    if not asset_root.is_dir():
        return found
    for path in asset_root.rglob("*"):
        if path.suffix in {".html", ".js", ".mjs"}:
            found.update(match.decode() for match in _API_LITERAL.findall(path.read_bytes()))
    return found


def compare_routes(old_root: Path, new_root: Path) -> RouteChange:
    old = api_literals(old_root)
    new = sorted(api_literals(new_root))
    digest = hashlib.sha256(("\n".join(new) + "\n").encode()).hexdigest()
    added = tuple(route for route in new if route not in old)
    removed = tuple(sorted(old - set(new)))
    return RouteChange(added, removed, digest, len(new))


def record_assets(root: Path, new: Pin, routes: RouteChange, *, accept_routes: bool) -> None:
    """Copy the new asset-set digest, and the route digest when accepted, into the SPA contract."""
    manifest = json.loads(
        (root / "frontend-shell/upstream" / new.version / "asset-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    path = root / "frontend-shell/upstream-contracts" / f"{new.version}.json"
    contract = json.loads(path.read_text(encoding="utf-8"))
    contract["assetIntegrity"]["assetSetSha256"] = manifest["assetSetSha256"]
    if accept_routes:
        contract["compiledNetworkContract"]["observedApiLiteralsSha256"] = routes.digest
        contract["compiledNetworkContract"]["observedApiLiteralsCount"] = routes.count
    _write_json(path, contract)


def wheel_sha256(version: str, opener: Callable[[str], bytes] | None = None) -> str:
    fetch = opener or _download
    return hashlib.sha256(fetch(WHEEL_URL.format(v=version))).hexdigest()


def _download(url: str) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing a non-HTTPS download: {url}")
    with urllib.request.urlopen(url, timeout=300) as response:  # noqa: S310  # nosec B310
        data: bytes = response.read()
    return data


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _run(argv: Sequence[str]) -> str:
    completed = subprocess.run(  # noqa: S603  # nosec B603 - fixed argv
        list(argv), cwd=ROOT, check=True, capture_output=True, text=True
    )
    return completed.stdout


def _git_move(source: Path, target: Path) -> None:
    _run(["git", "mv", str(source.relative_to(ROOT)), str(target.relative_to(ROOT))])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("version", help="the KiroCrew release to pin, e.g. 0.13.1")
    parser.add_argument(
        "--accept-routes",
        action="store_true",
        help="record the new /api/* literal digest after reviewing the printed changes",
    )
    args = parser.parse_args(argv)
    if not _VERSION.fullmatch(args.version):
        parser.error("version must look like 0.13.1")
    old = current_pin(ROOT)
    if old.version == args.version:
        if args.accept_routes:
            routes = compare_routes(
                Path("/nonexistent"), ROOT / "frontend-shell/upstream" / old.version
            )
            record_assets(ROOT, old, routes, accept_routes=True)
            print(f"Recorded {routes.count} /api/* literals for {old.version}.")
            return 0
        parser.error(f"{old.version} is already pinned")
    if _run(["git", "status", "--porcelain"]).strip():
        parser.error("the working tree has local changes; commit or stash them first")

    print(f"Downloading kirocrew {args.version} ...")
    new = Pin(args.version, wheel_sha256(args.version))
    print(f"  sha256 {new.sha256}")
    changed = rewrite_pins(ROOT, old, new)
    move_contracts(ROOT, old, new, _git_move)
    print("Relocking ...")
    _run(["uv", "lock", "--upgrade-package", "kirocrew"])
    lock = record_lock_digest(ROOT)
    _run(["uv", "sync", "--all-packages", "--all-groups", "--frozen"])
    print("Extracting the compiled SPA ...")
    _run(["uv", "run", "python", "tools/extract_kirocrew_spa.py"])
    routes = compare_routes(
        ROOT / "frontend-shell/upstream" / old.version,
        ROOT / "frontend-shell/upstream" / new.version,
    )
    record_assets(ROOT, new, routes, accept_routes=args.accept_routes)

    print(f"\nPinned {old.version} -> {new.version}; lock digest {lock}.")
    print("Rewrote: " + ", ".join(changed))
    print(
        f"\n/api/* literals: {routes.count} ({len(routes.added)} new, {len(routes.removed)} gone)"
    )
    for route in routes.added:
        print(f"  + {route}")
    for route in routes.removed:
        print(f"  - {route}")
    print(
        "\nStill yours to do:\n"
        "  1. Check each new route against the deny list in loopback.py, then re-run\n"
        f"     with --accept-routes ({sys.argv[0]} {new.version} --accept-routes).\n"
        f"  2. Read {RELEASE_URL.format(v=new.version)} for config or engine changes\n"
        "     the supervisor pins (supervisor.py) and for new host-native features.\n"
        "  3. make verify and build the image, then refresh the fork server's module\n"
        "     list from it (make image-forkserver-modules IMAGE_TAG=<image>) and rebuild.\n"
        "  4. Deploy runtime + frontend + control_api together."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
