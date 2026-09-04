from __future__ import annotations

import signal

import pytest
from kirocrew_agentcore_persistence.process import ProcessGroupQuiescer


def test_quiescer_requires_explicit_safe_process_groups() -> None:
    with pytest.raises(ValueError, match="non-system"):
        ProcessGroupQuiescer([])
    with pytest.raises(ValueError, match="non-system"):
        ProcessGroupQuiescer([1])


def test_quiescer_pauses_and_resumes_groups_once_in_safe_order() -> None:
    calls: list[tuple[int, signal.Signals]] = []
    quiescer = ProcessGroupQuiescer([20, 30], lambda group, value: calls.append((group, value)))
    quiescer.pause()
    quiescer.pause()
    quiescer.resume()
    quiescer.resume()
    assert calls == [
        (20, signal.SIGSTOP),
        (30, signal.SIGSTOP),
        (30, signal.SIGCONT),
        (20, signal.SIGCONT),
    ]
