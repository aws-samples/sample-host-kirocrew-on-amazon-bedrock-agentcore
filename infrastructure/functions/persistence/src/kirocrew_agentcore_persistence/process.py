from __future__ import annotations

import os
import signal
from collections.abc import Callable, Iterable


class ProcessGroupQuiescer:
    def __init__(
        self,
        process_groups: Iterable[int],
        signal_sender: Callable[[int, signal.Signals], None] = os.killpg,
    ) -> None:
        groups = tuple(process_groups)
        if not groups or any(group <= 1 for group in groups):
            raise ValueError("Explicit non-system process groups are required.")
        self._groups = groups
        self._signal_sender = signal_sender
        self._paused = False

    def pause(self) -> None:
        if self._paused:
            return
        for group in self._groups:
            self._signal_sender(group, signal.SIGSTOP)
        self._paused = True

    def resume(self) -> None:
        if not self._paused:
            return
        for group in reversed(self._groups):
            self._signal_sender(group, signal.SIGCONT)
        self._paused = False
