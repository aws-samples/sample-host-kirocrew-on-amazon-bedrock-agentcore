"""Keep the event loop free of blocking log I/O, and report when it stalls.

Production logs showed the runtime going completely silent for 47-62 s
several times an hour: no access log line, not even the platform's 2 s
``/ping``, followed by a burst of queued pings once it came back. The log
shipper thread kept its 5 s cadence through every gap, so the process was
running and only the event loop was stuck. Every send that landed in such a
gap failed, and the browser's WebSocket dropped when it ended.

Two pieces live here:

* ``offload_stream_handlers`` moves the root logger's stream handlers
  behind a queue, so a stalled stdout pipe blocks a background thread
  instead of the event loop. The queue is bounded and drops on overflow:
  a lost log line is better than a frozen sandbox or unbounded memory.
* ``LoopStallWatchdog`` notices a stalled loop from a separate thread and
  logs the loop thread's stack, so the next stall names its own cause.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import queue
import sys
import threading
import time
import traceback
from collections.abc import Callable
from types import FrameType

_LOGGER = logging.getLogger(__name__)

_QUEUE_LIMIT = 10_000


class DroppingQueueHandler(logging.handlers.QueueHandler):
    """A queue handler that never blocks and never reports a full queue.

    The stock handler routes ``queue.Full`` to ``handleError``, which writes
    to stderr: the very pipe that may be stalled.
    """

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            return


def offload_stream_handlers(
    logger: logging.Logger | None = None, *, limit: int = _QUEUE_LIMIT
) -> logging.handlers.QueueListener | None:
    """Serve ``logger``'s stream handlers from a background thread.

    Returns the started listener, or ``None`` when there is nothing to move.
    """
    target = logger or logging.getLogger()
    streams = [handler for handler in target.handlers if type(handler) is logging.StreamHandler]
    if not streams:
        return None
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=limit)
    for handler in streams:
        target.removeHandler(handler)
    target.addHandler(DroppingQueueHandler(records))
    listener = logging.handlers.QueueListener(records, *streams, respect_handler_level=True)
    listener.start()
    return listener


class LoopStallWatchdog:
    """Log the event loop thread's stack when the loop stops turning.

    Construct it on the loop thread. Every ``interval`` seconds a background
    thread schedules a beat on the loop; when the last beat is older than
    ``threshold`` it logs one warning with the loop's stack, and one more
    when the loop turns again.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        *,
        interval: float = 1.0,
        threshold: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        frames: Callable[[], dict[int, FrameType]] = sys._current_frames,
    ) -> None:
        self._loop = loop
        self._interval = interval
        self._threshold = threshold
        self._clock = clock
        self._frames = frames
        self._loop_thread = threading.get_ident()
        self._last_beat = clock()
        self._stalled_since: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loop-watchdog", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _beat(self) -> None:
        self._last_beat = self._clock()

    def check(self) -> None:
        """Run one watchdog round (the thread calls this every interval)."""
        try:
            self._loop.call_soon_threadsafe(self._beat)
        except RuntimeError:
            # The loop is closed: the process is shutting down.
            self._stop.set()
            return
        lag = self._clock() - self._last_beat
        if self._stalled_since is None:
            if lag >= self._threshold:
                self._stalled_since = self._last_beat
                frame = self._frames().get(self._loop_thread)
                stack = "".join(traceback.format_stack(frame)) if frame else "unavailable\n"
                _LOGGER.warning(
                    "Event loop has not run for %.1f s; loop thread stack:\n%s", lag, stack
                )
        elif lag < self._threshold:
            _LOGGER.warning(
                "Event loop running again after a %.1f s stall.",
                self._last_beat - self._stalled_since,
            )
            self._stalled_since = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.check()
