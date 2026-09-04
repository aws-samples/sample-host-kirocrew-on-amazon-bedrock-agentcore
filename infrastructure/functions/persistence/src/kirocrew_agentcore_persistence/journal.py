from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Final

from inotify_simple import INotify, flags

_WATCH_FLAGS: Final = (
    flags.CREATE
    | flags.DELETE
    | flags.MODIFY
    | flags.MOVED_FROM
    | flags.MOVED_TO
    | flags.CLOSE_WRITE
    | flags.ATTRIB
)


@dataclass(frozen=True, slots=True)
class JournalEvent:
    sequence: int
    path: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class JournalSnapshot:
    through_sequence: int
    paths: frozenset[str]
    first_dirty_at: datetime | None


class DirtyJournal:
    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        self._directory = workspace / ".agentcore/dirty-journal"
        self._path = self._directory / "events.jsonl"
        self._events = self._load()
        self._next_sequence = self._events[-1].sequence + 1 if self._events else 1

    def mark(self, relative_path: str, recorded_at: datetime) -> JournalEvent:
        path = PurePosixPath(relative_path)
        if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
            raise ValueError("Dirty path must be a safe relative workspace path.")
        event = JournalEvent(self._next_sequence, path.as_posix(), recorded_at.astimezone(UTC))
        self._next_sequence += 1
        self._events.append(event)
        self._append(event)
        return event

    def snapshot(self) -> JournalSnapshot:
        if not self._events:
            return JournalSnapshot(0, frozenset(), None)
        return JournalSnapshot(
            self._events[-1].sequence,
            frozenset(event.path for event in self._events),
            self._events[0].recorded_at,
        )

    def clear_through(self, sequence: int) -> None:
        self._events = [event for event in self._events if event.sequence > sequence]
        self._rewrite()

    def _load(self) -> list[JournalEvent]:
        if not self._path.exists():
            return []
        events: list[JournalEvent] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            value = json.loads(line)
            events.append(
                JournalEvent(
                    int(value["sequence"]),
                    str(value["path"]),
                    datetime.fromisoformat(str(value["recordedAt"]).replace("Z", "+00:00")),
                )
            )
        return events

    def _append(self, event: JournalEvent) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as target:
            target.write(self._serialize(event) + "\n")
            target.flush()

    def _rewrite(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(
            "".join(f"{self._serialize(event)}\n" for event in self._events),
            encoding="utf-8",
        )
        temporary.replace(self._path)

    @staticmethod
    def _serialize(event: JournalEvent) -> str:
        return json.dumps(
            {
                "sequence": event.sequence,
                "path": event.path,
                "recordedAt": event.recorded_at.isoformat().replace("+00:00", "Z"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


class InotifyDirtyWatcher:
    def __init__(self, workspace: Path, journal: DirtyJournal) -> None:
        self._workspace = workspace
        self._journal = journal
        self._inotify = INotify()
        self._paths_by_descriptor: dict[int, Path] = {}
        self._add_recursive(workspace)

    def poll_once(self, timeout_ms: int, now: datetime) -> int:
        count = 0
        for event in self._inotify.read(timeout=timeout_ms):
            parent = self._paths_by_descriptor.get(event.wd)
            if parent is None:
                continue
            path = parent / event.name if event.name else parent
            if path == self._workspace / ".agentcore" or (
                self._workspace / ".agentcore" in path.parents
            ):
                continue
            relative = path.relative_to(self._workspace).as_posix()
            self._journal.mark(relative, now)
            count += 1
            if flags.CREATE in flags.from_mask(event.mask) and path.is_dir():
                self._add_recursive(path)
        return count

    def _add_recursive(self, directory: Path) -> None:
        if not directory.exists() or not directory.is_dir():
            return
        for path in [directory, *(item for item in directory.rglob("*") if item.is_dir())]:
            if path == self._workspace / ".agentcore" or (
                self._workspace / ".agentcore" in path.parents
            ):
                continue
            descriptor = self._inotify.add_watch(str(path), _WATCH_FLAGS)
            self._paths_by_descriptor[descriptor] = path
