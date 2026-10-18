"""Write-ahead event queue under %LOCALAPPDATA%/AutoPulse/queue/ (fallback: %TEMP%/AutoPulse/queue/).

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
    """Queue over one or more directories.

    The first root is the normal place. Later roots are fallbacks used only when the
    earlier ones cannot be written, e.g. inside the Codex sandbox, which may read but not
    write %LOCALAPPDATA%. Reading always covers every root, so `sync` and the hooks
    (which run outside the sandbox) deliver what a sandboxed command queued.
    """

    def __init__(self, root: Path, *fallbacks: Path):
        self.roots = [root, *fallbacks]
        self.root = root
        self.failed = root / "failed"
        self._where: dict[str, Path] = {}  # event id -> file it was read from / written to

    @staticmethod
    def _name(event: Event) -> str:
        stamp = datetime.fromisoformat(event.created_at).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"{stamp}-{event.id}.json"

    def _path(self, event: Event) -> Path:
        return self._where.get(event.id) or self.root / self._name(event)

    def _write(self, path: Path, event: Event) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(event), fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def put(self, event: Event) -> Path:
        error: OSError | None = None
        for root in self.roots:
            path = root / self._name(event)
            try:
                self._write(path, event)
            except OSError as exc:
                error = exc
                continue
            self._where[event.id] = path
            return path
        raise error

    def _scan(self, folders: list[Path]) -> list[Event]:
        found: dict[str, Path] = {}
        for folder in folders:
            try:
                for path in folder.glob("*.json"):
                    found.setdefault(path.name, path)
            except OSError:
                continue
        events = []
        for name in sorted(found):  # names start with a UTC timestamp: oldest first across roots
            path = found[name]
            try:
                event = Event(**json.loads(path.read_text(encoding="utf-8")))
            except (ValueError, TypeError, json.JSONDecodeError):
                try:
                    path.rename(path.with_suffix(".corrupt"))
                except OSError:
                    pass
                continue
            except OSError:
                continue
            self._where[event.id] = path
            events.append(event)
        return events

    def pending(self) -> list[Event]:
        return self._scan(self.roots)

    def failed_events(self) -> list[Event]:
        return self._scan([root / "failed" for root in self.roots])

    def requeue_failed(self) -> list[Event]:
        events = self.failed_events()
        for event in events:
            source = self._where[event.id]
            event.next_attempt_at = None
            self._write(source.parent.parent / source.name, event)
            self._where[event.id] = source.parent.parent / source.name
            source.unlink(missing_ok=True)
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
        source = self._path(event)
        for folder in (self.failed, source.parent / "failed"):
            try:
                self._write(folder / source.name, event)
                break
            except OSError:
                continue
        source.unlink(missing_ok=True)

    # -- per-issue lock so two local processes never apply the same issue at once
    def lock(self, repo: str, issue: int) -> "IssueLock":
        return IssueLock([root / f".lock-{repo.replace('/', '__')}-{issue}" for root in self.roots])


def default_spool() -> "Spool":
    from auto_pulse import config as config_mod

    fallbacks = []
    override = os.environ.get("AUTOPULSE_FALLBACK_QUEUE")
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if override:
        fallbacks.append(Path(override))
    elif temp:
        fallbacks.append(Path(temp) / "AutoPulse" / "queue")
    return Spool(config_mod.home() / "queue", *fallbacks)


class IssueLock:
    def __init__(self, candidates: list[Path]):
        self.candidates = candidates
        self.path = candidates[0]
        self.acquired = False

    def __enter__(self) -> bool:
        for candidate in self.candidates:  # first directory we can write to
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                probe = candidate.parent / f".probe-{os.getpid()}"
                probe.write_bytes(b"")
                probe.unlink()
            except OSError:
                continue
            self.path = candidate
            break
        else:
            return False
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
