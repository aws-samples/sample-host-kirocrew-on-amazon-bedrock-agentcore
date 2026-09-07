"""Tests for the direct CloudWatch log shipping handler."""

from __future__ import annotations

import logging
import time
from typing import Any

import kirocrew_agentcore_runtime.cloudwatch_logs as module
import pytest
from kirocrew_agentcore_runtime.cloudwatch_logs import CloudWatchLogHandler


class StreamExists(Exception):  # noqa: N818 - stands in for the AWS SDK name
    pass


class FakeExceptions:
    ResourceAlreadyExistsException = StreamExists


class FakeLogsClient:
    def __init__(self) -> None:
        self.exceptions = FakeExceptions()
        self.streams: list[str] = []
        self.batches: list[list[dict[str, Any]]] = []
        self.fail_create = False
        self.fail_put = False
        self.stream_exists = False

    def create_log_stream(self, logGroupName: str, logStreamName: str) -> None:  # noqa: N803
        if self.fail_create:
            raise RuntimeError("create failed")
        if self.stream_exists:
            raise StreamExists()
        self.streams.append(f"{logGroupName}/{logStreamName}")

    def put_log_events(
        self,
        logGroupName: str,  # noqa: N803
        logStreamName: str,  # noqa: N803
        logEvents: list[dict[str, Any]],  # noqa: N803
    ) -> None:
        if self.fail_put:
            raise RuntimeError("put failed")
        self.batches.append(logEvents)


def make_record(message: str = "hello") -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, None, None)


def test_ships_records_in_chronological_batches() -> None:
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", stream="s")
    late = make_record("late")
    early = make_record("early")
    early.created = late.created - 10
    handler.emit(late)
    handler.emit(early)
    handler.flush()
    assert client.streams == ["group/s"]
    assert [e["message"] for e in client.batches[0]] == ["early", "late"]
    # A second round reuses the ready stream without re-creating it.
    handler.emit(make_record("second"))
    handler.flush()
    assert client.streams == ["group/s"]
    assert [e["message"] for e in client.batches[1]] == ["second"]
    handler.close()


def test_flush_without_records_and_existing_stream_are_fine() -> None:
    client = FakeLogsClient()
    client.stream_exists = True
    handler = CloudWatchLogHandler(client, "group")
    handler.flush()  # empty buffer: no calls
    assert client.batches == []
    handler.emit(make_record())
    handler.flush()  # stream already exists: tolerated
    assert len(client.batches) == 1
    assert handler.stream_name.startswith("runtime-")
    handler.close()


def test_shipping_failures_drop_the_batch_silently() -> None:
    client = FakeLogsClient()
    client.fail_create = True
    handler = CloudWatchLogHandler(client, "group", stream="s")
    handler.emit(make_record())
    handler.flush()  # create fails: batch dropped, no raise
    assert client.batches == []
    client.fail_create = False
    client.fail_put = True
    handler.emit(make_record())
    handler.flush()
    assert client.batches == []
    handler.close()


def test_formatting_failures_never_reach_the_buffer() -> None:
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", stream="s")

    class BadFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            raise ValueError("boom")

    handler.setFormatter(BadFormatter())
    handler.emit(make_record())
    handler.flush()
    assert client.batches == []
    handler.close()


def test_a_full_buffer_triggers_an_immediate_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_BATCH_LIMIT", 2)
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", stream="s")
    handler.emit(make_record("one"))
    assert client.batches == []
    handler.emit(make_record("two"))
    assert [e["message"] for e in client.batches[0]] == ["one", "two"]
    handler.close()


def test_long_messages_are_truncated() -> None:
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", stream="s")
    handler.emit(make_record("x" * 20_000))
    handler.flush()
    assert len(client.batches[0][0]["message"]) == module._MESSAGE_LIMIT
    handler.close()


def test_the_pump_thread_flushes_periodically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_FLUSH_SECONDS", 0.05)
    client = FakeLogsClient()
    handler = CloudWatchLogHandler(client, "group", stream="s")
    handler.emit(make_record("pumped"))
    deadline = time.time() + 2
    while not client.batches and time.time() < deadline:
        time.sleep(0.02)
    assert client.batches, "the pump thread should flush on its own"
    handler.close()
