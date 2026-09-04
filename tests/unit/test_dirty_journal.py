from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from kirocrew_agentcore_persistence.journal import DirtyJournal, InotifyDirtyWatcher

NOW = datetime(2026, 8, 17, 16, 0, tzinfo=UTC)


def test_dirty_journal_persists_snapshots_and_clears_only_captured_events(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    journal = DirtyJournal(workspace)
    assert journal.snapshot().first_dirty_at is None
    for invalid in ["/absolute", "../escape", "."]:
        with pytest.raises(ValueError, match="safe relative"):
            journal.mark(invalid, NOW)
    first = journal.mark("user/first.txt", NOW)
    second = journal.mark("projects/default/file.txt", NOW + timedelta(seconds=1))
    snapshot = journal.snapshot()
    assert snapshot.through_sequence == second.sequence
    assert snapshot.paths == frozenset({"user/first.txt", "projects/default/file.txt"})
    assert snapshot.first_dirty_at == NOW

    reloaded = DirtyJournal(workspace)
    assert reloaded.snapshot() == snapshot
    reloaded.clear_through(first.sequence)
    assert reloaded.snapshot().paths == frozenset({"projects/default/file.txt"})
    reloaded.clear_through(second.sequence)
    assert reloaded.snapshot().paths == frozenset()
    assert DirtyJournal(workspace).snapshot().paths == frozenset()


def test_inotify_watcher_records_changes_and_ignores_internal_journal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    user = workspace / "user"
    internal = workspace / ".agentcore"
    user.mkdir(parents=True)
    internal.mkdir()
    journal = DirtyJournal(workspace)
    watcher = InotifyDirtyWatcher(workspace, journal)

    (user / "new.txt").write_text("changed", encoding="utf-8")
    count = watcher.poll_once(1_000, NOW)
    assert count >= 1
    assert "user/new.txt" in journal.snapshot().paths

    before = journal.snapshot().through_sequence
    (internal / "ignored").write_text("internal", encoding="utf-8")
    time.sleep(0.01)
    watcher.poll_once(50, NOW)
    assert journal.snapshot().through_sequence == before


def test_inotify_watcher_adds_new_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = DirtyJournal(workspace)
    watcher = InotifyDirtyWatcher(workspace, journal)
    nested = workspace / "user"
    nested.mkdir()
    assert watcher.poll_once(1_000, NOW) >= 1
    (nested / "file.txt").write_text("content", encoding="utf-8")
    assert watcher.poll_once(1_000, NOW) >= 1
    assert "user/file.txt" in journal.snapshot().paths


def test_inotify_watcher_ignores_unknown_descriptors_and_missing_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    journal = DirtyJournal(workspace)
    watcher = InotifyDirtyWatcher(workspace, journal)

    class UnknownDescriptorInotify:
        def read(self, timeout: int) -> list[object]:
            assert timeout == 1
            return [type("Event", (), {"wd": 999, "name": "ignored", "mask": 0})()]

    watcher._inotify = UnknownDescriptorInotify()  # type: ignore[assignment]
    assert watcher.poll_once(1, NOW) == 0
    watcher._add_recursive(workspace / "missing")
