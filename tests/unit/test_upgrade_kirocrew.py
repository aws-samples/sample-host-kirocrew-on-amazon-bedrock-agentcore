from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
OLD_SHA = "a" * 64
NEW_SHA = "b" * 64


def tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "upgrade_kirocrew", ROOT / "tools" / "upgrade_kirocrew.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses look their module up in sys.modules while being defined.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def fake_repo(root: Path, module: ModuleType) -> None:
    for name in module.VERSIONED_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"pin 0.13.0 {OLD_SHA}\nnode >=10.13.0 keeps 0.13.01\n", encoding="utf-8")
    (root / "runtime/kirocrew-artifact.json").write_text(
        json.dumps({"version": "0.13.0", "sha256": OLD_SHA}), encoding="utf-8"
    )
    (root / "runtime/Dockerfile").write_text(
        f"ARG DEPENDENCY_LOCK_SHA256={'c' * 64}\nARG KIROCREW_VERSION=0.13.0\n"
        f"ARG DEPENDENCY_LOCK_SHA256={'c' * 64}\n",
        encoding="utf-8",
    )
    (root / "contracts/kirocrew").mkdir(parents=True)
    (root / "contracts/kirocrew/0.13.0-route-allowlist.json").write_text(
        json.dumps({"kirocrewVersion": "0.13.0", "deniedPrefixes": ["/api/token"]}),
        encoding="utf-8",
    )
    (root / "frontend-shell/upstream-contracts").mkdir(parents=True)
    (root / "frontend-shell/upstream-contracts/0.13.0.json").write_text(
        json.dumps(
            {
                "kiroCrew": {"version": "0.13.0", "artifactSha256": OLD_SHA},
                "assetIntegrity": {"manifest": "/upstream/0.13.0/asset-manifest.json"},
                "compiledNetworkContract": {"observedApiLiteralsSha256": "old"},
            }
        ),
        encoding="utf-8",
    )
    (root / "uv.lock").write_text("lock\n", encoding="utf-8")


def test_rewrites_pins_moves_contracts_and_records_digests(tmp_path: Path) -> None:
    module = tool()
    fake_repo(tmp_path, module)
    old = module.current_pin(tmp_path)
    assert old == module.Pin("0.13.0", OLD_SHA)
    new = module.Pin("0.13.1", NEW_SHA)

    changed = module.rewrite_pins(tmp_path, old, new)
    assert set(changed) == set(module.VERSIONED_FILES)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    # The pin moves; a longer version that merely contains it does not.
    assert readme == f"pin 0.13.1 {NEW_SHA}\nnode >=10.13.0 keeps 0.13.01\n"
    assert module.rewrite_pins(tmp_path, old, new) == []

    module.move_contracts(tmp_path, old, new, lambda source, target: source.rename(target))
    allowlist = json.loads(
        (tmp_path / "contracts/kirocrew/0.13.1-route-allowlist.json").read_text(encoding="utf-8")
    )
    assert allowlist == {"kirocrewVersion": "0.13.1", "deniedPrefixes": ["/api/token"]}
    assert not (tmp_path / "contracts/kirocrew/0.13.0-route-allowlist.json").exists()
    spa_path = tmp_path / "frontend-shell/upstream-contracts/0.13.1.json"
    spa = json.loads(spa_path.read_text(encoding="utf-8"))
    assert spa["kiroCrew"] == {"version": "0.13.1", "artifactSha256": NEW_SHA}
    assert spa["assetIntegrity"]["manifest"] == "/upstream/0.13.1/asset-manifest.json"

    digest = module.record_lock_digest(tmp_path)
    assert digest == hashlib.sha256(b"lock\n").hexdigest()
    assert (tmp_path / "runtime/Dockerfile").read_text(encoding="utf-8").count(digest) == 2

    old_assets = tmp_path / "frontend-shell/upstream/0.13.0"
    new_assets = tmp_path / "frontend-shell/upstream/0.13.1"
    old_assets.mkdir(parents=True)
    new_assets.mkdir(parents=True)
    (old_assets / "a.js").write_bytes(b'fetch("/api/kept");fetch("/api/gone")')
    (new_assets / "a.js").write_bytes(b'fetch("/api/kept");fetch("/api/new")')
    (new_assets / "style.css").write_bytes(b"/api/not-scanned")
    (new_assets / "asset-manifest.json").write_text(
        json.dumps({"assetSetSha256": "set"}), encoding="utf-8"
    )
    routes = module.compare_routes(old_assets, new_assets)
    assert routes.added == ("/api/new",)
    assert routes.removed == ("/api/gone",)
    assert routes.count == 2
    assert routes.digest == hashlib.sha256(b"/api/kept\n/api/new\n").hexdigest()
    assert module.api_literals(tmp_path / "missing") == set()

    # Without review the route digest stays as it was; the asset set always moves.
    module.record_assets(tmp_path, new, routes, accept_routes=False)
    spa = json.loads(spa_path.read_text(encoding="utf-8"))
    assert spa["assetIntegrity"]["assetSetSha256"] == "set"
    assert spa["compiledNetworkContract"]["observedApiLiteralsSha256"] == "old"
    module.record_assets(tmp_path, new, routes, accept_routes=True)
    spa = json.loads(spa_path.read_text(encoding="utf-8"))
    assert spa["compiledNetworkContract"] == {
        "observedApiLiteralsSha256": routes.digest,
        "observedApiLiteralsCount": 2,
    }


def test_wheel_digest_and_download_guard() -> None:
    module = tool()
    urls: list[str] = []

    def opener(url: str) -> bytes:
        urls.append(url)
        return b"wheel"

    assert module.wheel_sha256("0.13.1", opener) == hashlib.sha256(b"wheel").hexdigest()
    assert urls == [
        "https://download.crew.kiro.dev/cli/stable/0.13.1/kirocrew-0.13.1-py3-none-any.whl"
    ]
    with pytest.raises(ValueError, match="non-HTTPS"):
        module._download("http://example.com/x.whl")


def test_cli_refuses_bad_or_current_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    module = tool()
    with pytest.raises(SystemExit):
        module.main(["latest"])
    current = module.current_pin(ROOT).version
    with pytest.raises(SystemExit):
        module.main([current])
    monkeypatch.setattr(module, "_run", lambda _argv: " M README.md\n")
    with pytest.raises(SystemExit):
        module.main(["99.0.0"])
