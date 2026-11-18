"""Notice when this process was restored from an AgentCore Runtime V2 snapshot.

Runtime V2 starts a session by restoring a memory snapshot taken once per agent
version, so every value computed before the snapshot is shared by every restored
microVM. ``time.monotonic()`` does not advance across a restore while the wall
clock does, so the gap between the two jumps by the time the snapshot sat idle.
That jump is the signal: on Runtime V1 (no snapshots) it never moves beyond
ordinary clock adjustments, and the threshold keeps those from looking like a
restore.

https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-v2-optimize.html
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

# NTP slews and small steps stay well under this; a snapshot restore does not.
_RESTORE_THRESHOLD_SECONDS = 5.0


class RestoreDetector:
    """Report, once per restore, that the process resumed from a snapshot."""

    def __init__(
        self,
        threshold_seconds: float = _RESTORE_THRESHOLD_SECONDS,
        wall: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold_seconds
        self._wall = wall
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._offset = self._current_offset()

    def _current_offset(self) -> float:
        return self._wall() - self._monotonic()

    def restored(self) -> bool:
        """True the first time it is asked after a restore, then False again."""
        offset = self._current_offset()
        with self._lock:
            if abs(offset - self._offset) <= self._threshold:
                return False
            self._offset = offset
            return True
