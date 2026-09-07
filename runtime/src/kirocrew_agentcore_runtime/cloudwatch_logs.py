"""Direct CloudWatch Logs shipping for the runtime's own records.

The AgentCore platform's stdout pipeline has repeatedly stopped delivering
container output (both runtime log groups went silent mid-incident), which
left production failures undiagnosable. The runtime therefore ships its own
records to the deployment's dedicated log group over the AWS API. Shipping
is strictly best-effort: a logging failure must never break the sandbox.
"""

from __future__ import annotations

import atexit
import logging
import threading
import uuid
from typing import Any

_BATCH_LIMIT = 100
_FLUSH_SECONDS = 5.0
# PutLogEvents caps a single event at 256 KiB; stay far below it.
_MESSAGE_LIMIT = 8_000


class CloudWatchLogHandler(logging.Handler):
    """Buffer log records and ship them to CloudWatch in batches.

    A daemon thread flushes every ``_FLUSH_SECONDS`` or ``_BATCH_LIMIT``
    records. Every AWS interaction is wrapped: the handler degrades to
    dropping records rather than raising into application code.
    """

    def __init__(self, client: Any, group: str, stream: str | None = None) -> None:
        super().__init__()
        self._client = client
        self._group = group
        self._stream = stream or f"runtime-{uuid.uuid4()}"
        self._buffer: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stream_ready = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, name="cw-log-shipper", daemon=True)
        self._thread.start()
        atexit.register(self.flush)

    @property
    def stream_name(self) -> str:
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)[:_MESSAGE_LIMIT]
        except Exception:
            return
        event = {"timestamp": int(record.created * 1000), "message": message}
        with self._lock:
            self._buffer.append(event)
            full = len(self._buffer) >= _BATCH_LIMIT
        if full:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._buffer:
                return
            batch, self._buffer = self._buffer[:_BATCH_LIMIT], self._buffer[_BATCH_LIMIT:]
        try:
            if not self._stream_ready:
                try:
                    self._client.create_log_stream(
                        logGroupName=self._group, logStreamName=self._stream
                    )
                except self._client.exceptions.ResourceAlreadyExistsException:
                    pass
                self._stream_ready = True
            # PutLogEvents requires chronological order within a batch.
            batch.sort(key=lambda event: event["timestamp"])
            self._client.put_log_events(
                logGroupName=self._group,
                logStreamName=self._stream,
                logEvents=batch,
            )
        except Exception:
            # Drop the batch: growing the buffer during an outage would
            # trade a lost log line for a memory leak.
            return

    def close(self) -> None:
        self._stop.set()
        self.flush()
        super().close()

    def _pump(self) -> None:
        while not self._stop.wait(_FLUSH_SECONDS):
            self.flush()
