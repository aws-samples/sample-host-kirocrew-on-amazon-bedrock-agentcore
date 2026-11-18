"""Runtime V2 snapshot restores must not share identities drawn before the snapshot."""

from __future__ import annotations

import logging
from typing import Any

from kirocrew_agentcore_runtime.cloudwatch_logs import CloudWatchLogHandler
from kirocrew_agentcore_runtime.snapshot import RestoreDetector


class Clock:
    """Wall and monotonic clocks a test can move apart, as a restore does."""

    def __init__(self) -> None:
        self.wall_now = 1_700_000_000.0
        self.mono_now = 100.0

    def wall(self) -> float:
        return self.wall_now

    def monotonic(self) -> float:
        return self.mono_now

    def advance(self, seconds: float) -> None:
        self.wall_now += seconds
        self.mono_now += seconds

    def restore_after(self, idle_seconds: float) -> None:
        # The snapshot sat idle: wall time moved, monotonic time did not.
        self.wall_now += idle_seconds


def detector(clock: Clock) -> RestoreDetector:
    return RestoreDetector(wall=clock.wall, monotonic=clock.monotonic)


def test_ordinary_running_time_is_not_a_restore() -> None:
    clock = Clock()
    restores = detector(clock)
    clock.advance(3600)
    assert not restores.restored()


def test_small_clock_adjustments_are_not_a_restore() -> None:
    clock = Clock()
    restores = detector(clock)
    clock.wall_now += 1.5
    assert not restores.restored()


def test_a_restore_is_reported_once() -> None:
    clock = Clock()
    restores = detector(clock)
    clock.restore_after(600)
    assert restores.restored()
    assert not restores.restored()
    clock.advance(60)
    assert not restores.restored()
    clock.restore_after(30)
    assert restores.restored()


class FakeLogsClient:
    class exceptions:  # noqa: N801 - mirrors the AWS SDK attribute
        class ResourceAlreadyExistsException(Exception):  # noqa: N818
            pass

    def __init__(self) -> None:
        self.created: list[str] = []
        self.put: list[str] = []

    def create_log_stream(self, logGroupName: str, logStreamName: str) -> None:  # noqa: N803
        self.created.append(logStreamName)

    def put_log_events(self, logGroupName: str, logStreamName: str, logEvents: list[Any]) -> None:  # noqa: N803
        self.put.append(logStreamName)


def record() -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, "line", None, None)


def test_a_generated_log_stream_is_redrawn_after_a_restore() -> None:
    clock = Clock()
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", restore_detector=detector(clock))
    try:
        handler.emit(record())
        handler.flush()
        before = handler.stream_name
        clock.restore_after(900)
        handler.emit(record())
        handler.flush()
        after = handler.stream_name
    finally:
        handler.close()
    assert before != after
    assert client.created == [before, after]
    assert client.put == [before, after]


def test_an_explicit_log_stream_is_kept_after_a_restore() -> None:
    clock = Clock()
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(
        client, "group", stream="fixed", restore_detector=detector(clock)
    )
    try:
        handler.emit(record())
        handler.flush()
        clock.restore_after(900)
        handler.emit(record())
        handler.flush()
    finally:
        handler.close()
    assert client.created == ["fixed"]
    assert client.put == ["fixed", "fixed"]
