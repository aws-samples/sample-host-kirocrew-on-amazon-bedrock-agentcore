"""Pin the files a session start reads into memory before the V2 snapshot.

AgentCore Runtime V2 snapshots a microVM once its port answers, after dropping
the page cache (``drop_caches=3``). Every later session restores from that
snapshot and reads its files again from a lazily fetched remote disk; KiroCrew's
start reads about 5,000 files (Python packages, node, shared libraries), so on
V2 the gateway missed its readiness deadline. Pages a live process holds
mapped are not dropped, so mapping those files read-only before the port opens
carries their content into the snapshot. Quick's mount fix uses the same
technique, also from a separate pinner process; it does not cover directory
metadata, which is still read cold.

The manifest lists one absolute path or glob per line. ``{machine}`` stands for
``platform.machine()`` (``x86_64`` / ``aarch64``) and ``{node_arch}`` for the
Node distribution's name for it (``x64`` / ``arm64``), so one manifest serves
both image architectures. Missing files are skipped: the manifest is a hint,
never a requirement for start.
"""

from __future__ import annotations

import glob
import json
import logging
import mmap
import platform
import select
import signal
import subprocess  # nosec B404 - starts this module as the pinner process.
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

_LOGGER = logging.getLogger(__name__)

PAGE_PIN_ENV: Final = "KIROCREW_PAGE_PIN"
MANIFEST_ENV: Final = "KIROCREW_PAGE_PIN_MANIFEST"
DEFAULT_MANIFEST: Final = Path("/opt/app/pin-manifest.txt")
#: Pinned pages are resident memory in every session restored from the snapshot.
DEFAULT_BUDGET_BYTES: Final = 512 * 1024 * 1024
#: The platform health-checks the port while the snapshot is being built, and
#: the image disk is fetched lazily then too: pinning stops at this deadline so
#: the port always opens in time. Files past it are read cold, as without pins.
DEFAULT_DEADLINE_SECONDS: Final = 20.0
#: The serving process waits this long for the pinner's report, then opens the
#: port regardless.
DEFAULT_WAIT_SECONDS: Final = 30.0
_NODE_ARCH: Final = {"x86_64": "x64", "aarch64": "arm64"}

# Held for the life of the process: dropping a mapping releases its pages.
_PINNED: list[mmap.mmap] = []
# The serving process keeps its pinner alive; its mappings hold the pages.
_PINNERS: list[subprocess.Popen[str]] = []


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


def pin(
    paths: Iterable[Path],
    budget_bytes: int = DEFAULT_BUDGET_BYTES,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
) -> PinReport:
    """Map each file read-only and populated; stop at the budget or deadline."""
    started = time.monotonic()
    pinned = total = skipped = 0
    for path in paths:
        if time.monotonic() - started > deadline_seconds:
            skipped += 1
            continue
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


def pin_from_environment(
    environment: Mapping[str, str], wait_seconds: float = DEFAULT_WAIT_SECONDS
) -> PinReport | None:
    """Start the pinner when ``KIROCREW_PAGE_PIN=1`` and wait for its report.

    The mappings live in a separate process, as in Quick's fix: a runtime whose
    serving process itself held file mappings failed the V2 snapshot with "The
    runtime process exited unexpectedly". Never raises; the pinner is a hint.
    """
    if environment.get(PAGE_PIN_ENV, "").strip() != "1":
        return None
    manifest = Path(environment.get(MANIFEST_ENV, "") or DEFAULT_MANIFEST)
    if not manifest.is_file():
        _LOGGER.warning("Page pin manifest %s is unreadable; nothing pinned.", manifest)
        return None
    try:
        process = subprocess.Popen(  # noqa: S603  # nosec B603 - fixed argv, own module.
            [sys.executable, "-m", __name__, str(manifest)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
        )
    except OSError:
        _LOGGER.warning("Page pinner did not start; nothing pinned.", exc_info=True)
        return None
    _PINNERS.append(process)
    report = _read_report(process, wait_seconds)
    if report is None:
        _LOGGER.warning("Page pinner sent no report within %.0f s.", wait_seconds)
        return None
    _LOGGER.info(
        "Pinned %d files (%.0f MB) for the snapshot in %.1f s; %d skipped.",
        report.files,
        report.bytes / 1e6,
        report.seconds,
        report.skipped,
    )
    return report


def _read_report(process: subprocess.Popen[str], wait_seconds: float) -> PinReport | None:
    stdout = process.stdout
    if stdout is None:
        return None
    ready, _, _ = select.select([stdout], [], [], wait_seconds)
    if not ready:
        return None
    try:
        fields = json.loads(stdout.readline())
        return PinReport(
            int(fields["files"]),
            int(fields["bytes"]),
            int(fields["skipped"]),
            float(fields["seconds"]),
        )
    except (ValueError, KeyError, TypeError):
        return None


def pinned_count() -> int:
    return len(_PINNED)


def _hold() -> None:  # pragma: no cover - blocks until the process is killed.
    while True:
        signal.pause()


def main(argv: list[str], hold: Callable[[], None] = _hold) -> int:
    """Pinner process: map the manifest, report on stdout, then hold the pages."""
    lines = Path(argv[1]).read_text(encoding="utf-8").splitlines()
    report = pin(expand(lines))
    sys.stdout.write(json.dumps(asdict(report)) + "\n")
    sys.stdout.flush()
    hold()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised in the image smoke.
    sys.exit(main(sys.argv))
