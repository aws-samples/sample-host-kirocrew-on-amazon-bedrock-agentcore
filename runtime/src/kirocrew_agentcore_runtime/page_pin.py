"""Pin the files a session start reads into memory before the V2 snapshot.

AgentCore Runtime V2 snapshots a microVM once its port answers, after dropping
the page cache (``drop_caches=3``). Every later session restores from that
snapshot and reads its files again from a lazily fetched remote disk; KiroCrew's
start reads about 5,000 files (Python packages, node, kiro-cli, the KAS engine),
so on V2 the gateway missed its readiness deadline. Pages a live process holds
mapped are not dropped, so mapping those files read-only before the port opens
carries their content into the snapshot. Quick's mount fix uses the same
technique; it does not cover directory metadata, which is still read cold.

The manifest lists one absolute path or glob per line. ``{machine}`` stands for
``platform.machine()`` (``x86_64`` / ``aarch64``) and ``{node_arch}`` for the
Node distribution's name for it (``x64`` / ``arm64``), so one manifest serves
both image architectures. Missing files are skipped: the manifest is a hint,
never a requirement for start.
"""

from __future__ import annotations

import glob
import logging
import mmap
import platform
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

PAGE_PIN_ENV: Final = "KIROCREW_PAGE_PIN"
MANIFEST_ENV: Final = "KIROCREW_PAGE_PIN_MANIFEST"
DEFAULT_MANIFEST: Final = Path("/opt/app/pin-manifest.txt")
#: Pinned pages are resident memory in every session restored from the snapshot.
DEFAULT_BUDGET_BYTES: Final = 1024 * 1024 * 1024
_NODE_ARCH: Final = {"x86_64": "x64", "aarch64": "arm64"}

# Held for the life of the process: dropping a mapping releases its pages.
_PINNED: list[mmap.mmap] = []


@dataclass(frozen=True, slots=True)
class PinReport:
    files: int
    bytes: int
    skipped: int
    seconds: float


def expand(lines: Iterable[str], machine: str | None = None) -> list[Path]:
    """Resolve manifest lines to existing regular files, in order, without repeats."""
    machine = machine or platform.machine()
    node_arch = _NODE_ARCH.get(machine, machine)
    seen: set[str] = set()
    paths: list[Path] = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern = line.replace("{machine}", machine).replace("{node_arch}", node_arch)
        matches = (
            sorted(glob.glob(pattern, recursive=True)) if glob.has_magic(pattern) else [pattern]  # noqa: PTH207 - absolute patterns
        )
        for match in matches:
            if match in seen or not Path(match).is_file():
                continue
            seen.add(match)
            paths.append(Path(match))
    return paths


def pin(paths: Iterable[Path], budget_bytes: int = DEFAULT_BUDGET_BYTES) -> PinReport:
    """Map each file read-only and populated; stop at *budget_bytes*."""
    started = time.monotonic()
    pinned = total = skipped = 0
    for path in paths:
        try:
            size = path.stat().st_size
            if size == 0:
                continue
            if total + size > budget_bytes:
                skipped += 1
                continue
            with path.open("rb") as handle:
                mapping = mmap.mmap(
                    handle.fileno(),
                    0,
                    flags=mmap.MAP_SHARED | getattr(mmap, "MAP_POPULATE", 0),
                    prot=mmap.PROT_READ,
                )
        except (OSError, ValueError):
            skipped += 1
            continue
        _PINNED.append(mapping)
        pinned += 1
        total += size
    return PinReport(pinned, total, skipped, time.monotonic() - started)


def pin_from_environment(environment: Mapping[str, str]) -> PinReport | None:
    """Pin the manifest when ``KIROCREW_PAGE_PIN=1``; never raises."""
    if environment.get(PAGE_PIN_ENV, "").strip() != "1":
        return None
    manifest = Path(environment.get(MANIFEST_ENV, "") or DEFAULT_MANIFEST)
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        _LOGGER.warning("Page pin manifest %s is unreadable; nothing pinned.", manifest)
        return None
    report = pin(expand(lines))
    _LOGGER.info(
        "Pinned %d files (%.0f MB) for the snapshot in %.1f s; %d skipped.",
        report.files,
        report.bytes / 1e6,
        report.seconds,
        report.skipped,
    )
    return report


def pinned_count() -> int:
    return len(_PINNED)
