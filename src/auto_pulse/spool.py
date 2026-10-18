"""Write-ahead event queue under %LOCALAPPDATA%/AutoPulse/queue/.

An event is written to disk before GitHub is touched, and removed only after it was
applied. Transient failures stay in the queue with exponential backoff; permanent
failures move to queue/failed/.
"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from auto_pulse.redact import redact

EVENT_TYPES = ("start", "note", "block", "review", "complete")
BACKOFF_BASE = 60  # seconds
BACKOFF_MAX = 6 * 3600
LOCK_STALE = 300  # seconds


@dataclass
class Event:
    type: str
    repo: str
    issue: int
    data: dict[str, Any] = field(default_factory=dict)
    agent: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat(timespec="seconds"))
    attempts: int = 0
    next_attempt_at: str | None = None
    last_error: str | None = None

    def __post_init__(self):
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {self.type}")
        # Free text may come from an AI session: never persist secrets.
        self.data = {k: redact(v) if isinstance(v, str) else v for k, v in self.data.items()}

    @property
    def local_date(self) -> str:
        return datetime.fromisoformat(self.created_at).date().isoformat()

    def due(self, now: datetime) -> bool:
        return self.next_attempt_at is None or datetime.fromisoformat(self.next_attempt_at) <= now


class Spool:
    def __init__(self, root: Path):
        self.root = root
        self.failed = root / "failed"

    def _path(self, event: Event) -> Path:
        stamp = datetime.fromisoformat(event.created_at).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return self.root / f"{stamp}-{event.id}.json"

    def _write(self, path: Path, event: Event) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(event), fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def put(self, event: Event) -> Path:
        path = self._path(event)
        self._write(path, event)
        return path

    def pending(self) -> list[Event]:
        if not self.root.exists():
            return []
        events = []
        for path in sorted(self.root.glob("*.json")):
            try:
                events.append(Event(**json.loads(path.read_text(encoding="utf-8"))))
            except (ValueError, TypeError, json.JSONDecodeError):
                path.rename(path.with_suffix(".corrupt"))
        return events

    def failed_events(self) -> list[Event]:
        if not self.failed.exists():
            return []
        return [Event(**json.loads(p.read_text(encoding="utf-8"))) for p in sorted(self.failed.glob("*.json"))]

    def requeue_failed(self) -> list[Event]:
        events = self.failed_events()
        for event in events:
            event.next_attempt_at = None
            self._write(self._path(event), event)
            (self.failed / self._path(event).name).unlink(missing_ok=True)
        return events

    def done(self, event: Event) -> None:
        self._path(event).unlink(missing_ok=True)

    def retry_later(self, event: Event, error: str, now: datetime | None = None) -> None:
        now = now or datetime.now().astimezone()
        event.attempts += 1
        delay = min(BACKOFF_BASE * 2 ** (event.attempts - 1), BACKOFF_MAX)
        event.next_attempt_at = (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
        event.last_error = redact(error)[:500]
        self._write(self._path(event), event)

    def fail(self, event: Event, error: str) -> None:
        event.last_error = redact(error)[:500]
        self._write(self.failed / self._path(event).name, event)
        self.done(event)

    # -- per-issue lock so two local processes never apply the same issue at once
    def lock(self, repo: str, issue: int) -> "IssueLock":
        return IssueLock(self.root / f".lock-{repo.replace('/', '__')}-{issue}")


class IssueLock:
    def __init__(self, path: Path):
        self.path = path
        self.acquired = False

    def __enter__(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.acquired = True
                return True
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > LOCK_STALE:
                        self.path.unlink(missing_ok=True)
                        continue
                except FileNotFoundError:
                    continue
                return False
        return False

    def __exit__(self, *exc) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
