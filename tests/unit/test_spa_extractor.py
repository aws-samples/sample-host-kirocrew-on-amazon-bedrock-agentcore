from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]


def load_extractor() -> ModuleType:
    path = ROOT / "tools" / "extract_kirocrew_spa.py"
    spec = importlib.util.spec_from_file_location("extract_kirocrew_spa", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def compiled_fixture(root: Path) -> Path:
    source = root / "dist"
    (source / "assets").mkdir(parents=True)
    (source / "index.html").write_text(
        '<html><head><script type="module" src="/assets/main.js"></script>'
        '<link rel="stylesheet" href="/assets/main.css"></head></html>',
        encoding="utf-8",
    )
    (source / "assets" / "main.js").write_text("export const ready=true;\n")
    (source / "assets" / "main.css").write_text("body{color:black}\n")
    (source / "assets" / "font.woff2").write_bytes(b"font")
    (source / "icon.png").write_bytes(b"icon")
    return source


def pin(module: ModuleType) -> Any:
    return module.ArtifactPin("kirocrew", "0.2.0", "kirocrew.whl", "a" * 64, "release")


def test_extractor_is_stable_versioned_and_bootstrap_first(tmp_path: Path) -> None:
    module = load_extractor()
    source = compiled_fixture(tmp_path)
    output = tmp_path / "upstream"

    first = module.extract(source, output, pin(module))
    first_tree = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }
    second = module.extract(source, output, pin(module))
    second_tree = {
        path.relative_to(output).as_posix(): path.read_bytes()
        for path in output.rglob("*")
        if path.is_file()
    }

    assert first == second
    assert first_tree == second_tree
    assert first["assetSetSha256"] == second["assetSetSha256"]
    manifest = json.loads((output / "0.2.0" / "asset-manifest.json").read_text())
    paths = [asset["path"] for asset in manifest["assets"]]
    assert paths == sorted(paths)
    index = (output / "0.2.0" / "index.html").read_text()
    assert index.index("/bootstrap.js") < index.index("/assets/main.js")
    assert manifest["upstream"]["artifactSha256"] == "a" * 64
    assert all(len(asset["sha256"]) == 64 for asset in manifest["assets"])


def test_extractor_rejects_sources_symlinks_and_incomplete_dist(tmp_path: Path) -> None:
    module = load_extractor()
    source = compiled_fixture(tmp_path)
    (source / "source.ts").write_text("source")
    with pytest.raises(ValueError, match="source artifact"):
        module.extract(source, tmp_path / "output", pin(module))
    (source / "source.ts").unlink()
    (source / "link.js").symlink_to(source / "assets" / "main.js")
    with pytest.raises(ValueError, match="symbolic link"):
        module.extract(source, tmp_path / "output", pin(module))
    (source / "link.js").unlink()
    (source / "index.html").write_text("<html></html>")
    with pytest.raises(ValueError, match="module entry point"):
        module.extract(source, tmp_path / "output", pin(module))


def test_pin_and_installed_distribution_validation(tmp_path: Path) -> None:
    module = load_extractor()
    invalid = tmp_path / "artifact.json"
    invalid.write_text('{"schemaVersion":2}')
    with pytest.raises(ValueError, match="invalid"):
        module.load_pin(invalid)
    invalid.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "distribution": "kirocrew",
                "version": "0.2.0",
                "wheel": "wheel",
                "sha256": "bad",
                "source": "release",
            }
        )
    )
    with pytest.raises(ValueError, match="digest"):
        module.load_pin(invalid)
    installed = module.installed_dist()
    assert installed.name == "dist"
    assert (installed / "index.html").is_file()
