#!/usr/bin/env python3
"""Extract the pinned compiled KiroCrew SPA without vendoring upstream source."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
_ARTIFACT_CONFIG: Final = _PROJECT_ROOT / "runtime" / "kirocrew-artifact.json"
_BOOTSTRAP_TAG: Final = '<script type="module" src="/bootstrap.js"></script>'
_COMPILED_SUFFIXES: Final = frozenset(
    {
        ".css",
        ".html",
        ".ico",
        ".jpeg",
        ".jpg",
        ".js",
        ".json",
        ".mjs",
        ".png",
        ".svg",
        # A bundled font's licence text ships beside it: the SIL Open Font
        # License requires the copy to travel with the font (0.5.0 adds
        # fonts/opendyslexic/OFL.txt).
        ".txt",
        ".ttf",
        ".wasm",
        ".webmanifest",
        ".woff",
        ".woff2",
    }
)
_SOURCE_SUFFIXES: Final = frozenset({".jsx", ".map", ".ts", ".tsx"})
# Precompressed duplicates of compiled assets (0.3.0 ships Brotli variants).
# CloudFront compresses at the edge, so serving the originals is enough.
_PRECOMPRESSED_SUFFIXES: Final = frozenset({".br", ".gz"})


@dataclass(frozen=True, slots=True)
class ArtifactPin:
    distribution: str
    version: str
    wheel: str
    sha256: str
    source: str


@dataclass(frozen=True, slots=True)
class AssetRecord:
    path: str
    bytes: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        return {"path": self.path, "bytes": self.bytes, "sha256": self.sha256}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_pin(path: Path = _ARTIFACT_CONFIG) -> ArtifactPin:
    value: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("KiroCrew artifact metadata is invalid.")
    required = ("distribution", "version", "wheel", "sha256", "source")
    if any(not isinstance(value.get(key), str) or not value[key] for key in required):
        raise ValueError("KiroCrew artifact metadata is incomplete.")
    digest = value["sha256"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("KiroCrew artifact digest is invalid.")
    return ArtifactPin(*(value[key] for key in required))


def installed_dist() -> Path:
    spec = importlib.util.find_spec("kiro_crew")
    if spec is None or spec.submodule_search_locations is None:
        raise FileNotFoundError("The pinned KiroCrew distribution is not installed.")
    package_root = Path(next(iter(spec.submodule_search_locations))).resolve()
    return package_root / "static" / "dist"


def _compiled_files(source: Path) -> tuple[Path, ...]:
    if not source.is_dir():
        raise FileNotFoundError("The upstream compiled SPA directory does not exist.")
    files: list[Path] = []
    for candidate in source.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"Upstream SPA contains a symbolic link: {candidate}")
        if not candidate.is_file():
            continue
        if candidate.suffix in _PRECOMPRESSED_SUFFIXES:
            continue
        if candidate.suffix in _SOURCE_SUFFIXES:
            raise ValueError(f"Upstream SPA contains a source artifact: {candidate}")
        if candidate.suffix not in _COMPILED_SUFFIXES:
            raise ValueError(f"Upstream SPA contains an unsupported artifact: {candidate}")
        files.append(candidate)

    def relative(path: Path) -> str:
        return path.relative_to(source).as_posix()

    ordered = tuple(sorted(files, key=relative))
    if not ordered or relative(ordered[0]) == "":
        raise ValueError("The upstream compiled SPA is empty.")
    if not (source / "index.html").is_file():
        raise ValueError("The upstream compiled SPA has no index.html.")
    if not any(path.suffix == ".js" for path in ordered):
        raise ValueError("The upstream compiled SPA has no JavaScript bundle.")
    if not any(path.suffix == ".css" for path in ordered):
        raise ValueError("The upstream compiled SPA has no CSS bundle.")
    return ordered


def _render_index(content: bytes) -> bytes:
    text = content.decode("utf-8")
    if _BOOTSTRAP_TAG in text:
        raise ValueError("The upstream index already contains the project bootstrap.")
    marker = '<script type="module"'
    position = text.find(marker)
    if position < 0:
        raise ValueError("The upstream index has no module entry point.")
    rendered = f"{text[:position]}{_BOOTSTRAP_TAG}\n  {text[position:]}"
    if rendered.index(_BOOTSTRAP_TAG) > rendered.index(marker, position):
        raise AssertionError("Bootstrap ordering is invalid.")
    return rendered.encode("utf-8")


def _asset_set_digest(records: Iterable[AssetRecord]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(record.path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(record.bytes).encode("ascii"))
        digest.update(b"\0")
        digest.update(record.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def extract(source: Path, output_root: Path, pin: ArtifactPin) -> dict[str, object]:
    source = source.resolve()
    files = _compiled_files(source)
    output = output_root / pin.version
    output_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{pin.version}-", dir=output_root))
    records: list[AssetRecord] = []
    original_index_sha256 = ""
    try:
        for source_file in files:
            relative = source_file.relative_to(source)
            content = source_file.read_bytes()
            if relative.as_posix() == "index.html":
                original_index_sha256 = _sha256(content)
                content = _render_index(content)
            destination = staging / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
            records.append(AssetRecord(relative.as_posix(), len(content), _sha256(content)))
        manifest: dict[str, object] = {
            "schemaVersion": 1,
            "upstream": {
                "distribution": pin.distribution,
                "version": pin.version,
                "wheel": pin.wheel,
                "artifactSha256": pin.sha256,
                "source": pin.source,
            },
            "bootstrap": {
                "path": "/bootstrap.js",
                "loadsBeforeUpstreamBundle": True,
            },
            "originalIndexSha256": original_index_sha256,
            "assetSetSha256": _asset_set_digest(records),
            "assets": [record.as_dict() for record in records],
        }
        (staging / "asset-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if output.exists():
            shutil.rmtree(output)
        staging.replace(output)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=_PROJECT_ROOT / "frontend-shell" / "upstream",
    )
    parser.add_argument("--check", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    pin = load_pin()
    source = arguments.source or installed_dist()
    if arguments.check:
        with tempfile.TemporaryDirectory() as temporary:
            expected = extract(source, Path(temporary), pin)
            manifest_path = arguments.output_root / pin.version / "asset-manifest.json"
            if not manifest_path.is_file():
                raise SystemExit("Extracted KiroCrew SPA manifest is missing.")
            actual = json.loads(manifest_path.read_text(encoding="utf-8"))
            if actual != expected:
                raise SystemExit("Extracted KiroCrew SPA assets are stale.")
        return 0
    manifest = extract(source, arguments.output_root, pin)
    print(json.dumps({"assetSetSha256": manifest["assetSetSha256"], "version": pin.version}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
