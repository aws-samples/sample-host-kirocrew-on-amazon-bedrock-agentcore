"""Tests for keeping log I/O off the event loop and reporting loop stalls."""

from __future__ import annotations

import asyncio
import io
import logging
import queue
import sys
import threading
import time
from types import FrameType

import pytest
from kirocrew_agentcore_runtime.loop_health import (
    DroppingQueueHandler,
    LoopStallWatchdog,
    offload_stream_handlers,
)


def make_record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)


def test_a_full_queue_drops_records_without_touching_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    records: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = DroppingQueueHandler(records)
    handler.handle(make_record("kept"))
    handler.handle(make_record("dropped"))
    assert records.qsize() == 1
    assert records.get_nowait().getMessage() == "kept"
    assert capsys.readouterr().err == ""


def test_stream_handlers_are_served_from_a_background_thread() -> None:
    logger = logging.getLogger("loop-health-offload")
    logger.propagate = False
    logger.setLevel(logging.INFO)
    written: list[str] = []

    class RecordingStream(io.StringIO):
        def write(self, text: str) -> int:
            written.append(threading.current_thread().name)
            return super().write(text)

    stream = RecordingStream()
    stream_handler = logging.StreamHandler(stream)
    other = logging.NullHandler()
    logger.addHandler(stream_handler)
    logger.addHandler(other)
    listener = offload_stream_handlers(logger, limit=10)
    assert listener is not None
    try:
        assert stream_handler not in logger.handlers
        assert other in logger.handlers
        assert any(isinstance(h, DroppingQueueHandler) for h in logger.handlers)
        logger.info("through the queue")
    finally:
        listener.stop()
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
    assert "through the queue" in stream.getvalue()
    assert written and threading.main_thread().name not in written


def test_nothing_to_offload_returns_none() -> None:
    logger = logging.getLogger("loop-health-empty")
    logger.propagate = False
    assert offload_stream_handlers(logger) is None


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class ManualLoop:
    """Collects scheduled beats so the test decides when the loop "runs"."""

    def __init__(self) -> None:
        self.pending: list[object] = []
        self.closed = False

    def call_soon_threadsafe(self, callback: object) -> None:
        if self.closed:
            raise RuntimeError("Event loop is closed")
        self.pending.append(callback)

    def run_pending(self) -> None:
        callbacks, self.pending = self.pending, []
        for callback in callbacks:
            callback()  # type: ignore[operator]


def test_a_stall_is_logged_once_with_the_loop_stack_and_its_end(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock()
    loop = ManualLoop()
    current = sys._getframe()

    def frames() -> dict[int, FrameType]:
        return {threading.get_ident(): current}

    watchdog = LoopStallWatchdog(
        loop,  # type: ignore[arg-type]
        threshold=5.0,
        clock=clock,
        frames=frames,
    )
    with caplog.at_level(logging.WARNING, logger="kirocrew_agentcore_runtime.loop_health"):
        watchdog.check()  # healthy: lag 0
        loop.run_pending()
        clock.now += 6
        watchdog.check()  # loop did not run for 6 s
        clock.now += 30
        watchdog.check()  # still stalled: no second warning
        loop.run_pending()  # the loop turns again
        watchdog.check()
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert "has not run for 6.0 s" in messages[0]
    assert "test_a_stall_is_logged_once" in messages[0]
    assert "after a 36.0 s stall" in messages[1]


def test_a_missing_loop_frame_still_reports(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    loop = ManualLoop()
    watchdog = LoopStallWatchdog(
        loop,  # type: ignore[arg-type]
        threshold=1.0,
        clock=clock,
        frames=dict,
    )
    clock.now += 2
    with caplog.at_level(logging.WARNING, logger="kirocrew_agentcore_runtime.loop_health"):
        watchdog.check()
    assert "unavailable" in caplog.text


def test_a_closed_loop_stops_the_watchdog() -> None:
    loop = ManualLoop()
    loop.closed = True
    watchdog = LoopStallWatchdog(loop)  # type: ignore[arg-type]
    watchdog.check()
    assert watchdog._stop.is_set()


def test_the_thread_detects_a_blocked_real_loop(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        watchdog = LoopStallWatchdog(asyncio.get_running_loop(), interval=0.02, threshold=0.1)
        watchdog.start()
        time.sleep(0.4)  # deliberately block the loop
        await asyncio.sleep(0.2)
        watchdog.stop()
        watchdog._thread.join(1)

    with caplog.at_level(logging.WARNING, logger="kirocrew_agentcore_runtime.loop_health"):
        asyncio.run(scenario())
    assert "has not run for" in caplog.text
    assert "running again" in caplog.text
