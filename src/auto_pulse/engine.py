"""Apply planned changes to GitHub and drain the local queue."""

from dataclasses import dataclass
from datetime import datetime

from auto_pulse.config import Config
from auto_pulse.errors import AutoPulseError, PolicyRefused, TransientError
from auto_pulse.github import GhClient, IssueSnapshot
from auto_pulse.spool import Event, Spool
from auto_pulse.workflow import Plan, build_plan

BODY_RETRIES = 3


class _BodyRaced(Exception):
    pass


def _same(a: str, b: str) -> bool:
    return a.replace("\r\n", "\n").strip() == b.replace("\r\n", "\n").strip()


class Engine:
    def __init__(self, client: GhClient, cfg: Config):
        self.client = client
        self.cfg = cfg

    def _snapshot(self, event: Event) -> IssueSnapshot:
        return self.client.get_issue(event.repo, event.issue, self.cfg.owner, self.cfg.project_number)

    def plan(self, event: Event) -> Plan:
        """Read-only: what apply() would do right now."""
        self._guard(event)
        snap = self._snapshot(event)
        return build_plan(event, snap, self.cfg, self.client.comment_event_ids(event.repo, event.issue))

    def apply(self, event: Event) -> Plan:
        self._guard(event)
        for attempt in range(BODY_RETRIES + 1):
            snap = self._snapshot(event)  # always re-read right before writing
            posted = self.client.comment_event_ids(event.repo, event.issue)
            plan = build_plan(event, snap, self.cfg, posted, drop_body_edit=attempt == BODY_RETRIES)
            try:
                self._execute(plan, snap)
                return plan
            except _BodyRaced:
                continue
        raise AssertionError("unreachable")

    def _guard(self, event: Event) -> None:
        if not self.cfg.allows(event.repo):
            raise PolicyRefused(f"{event.repo} は allowlist（config の [repos] allow）にありません")

    def _execute(self, plan: Plan, snap: IssueSnapshot) -> None:
        owner, number, c = self.cfg.owner, self.cfg.project_number, self.client
        for op in plan.ops:
            if op.kind == "add_item":
                c.add_item(owner, number, snap.url)
            elif op.kind == "set_field":
                c.set_field(owner, number, snap.url, op.field, op.value)
            elif op.kind == "edit_body":
                fresh = self._snapshot(plan.event).body
                if not _same(fresh, op.base_body or ""):
                    raise _BodyRaced()
                c.edit_body(snap.repo, snap.number, op.body)
            elif op.kind == "close":
                c.close_issue(snap.repo, snap.number)
            elif op.kind == "comment":
                c.add_comment(snap.repo, snap.number, op.body)


def _quietly(fn, *args) -> None:
    try:
        fn(*args)
    except OSError:
        pass


@dataclass
class Outcome:
    event: Event
    status: str  # applied | noop | downgraded | conflict | queued | waiting | locked | failed
    plan: Plan | None = None
    error: str | None = None
    code: int | None = None  # exit code of the error that failed the event


def process(engine: Engine, spool: Spool, only: tuple[str, int] | None = None, force: bool = False) -> list[Outcome]:
    """Send pending events oldest-first. Events of one issue are kept in order."""
    now = datetime.now().astimezone()
    held: set[tuple[str, int]] = set()
    results = []
    for event in spool.pending():
        key = (event.repo.lower(), event.issue)
        if only and key != (only[0].lower(), only[1]):
            continue
        if key in held:
            results.append(Outcome(event, "waiting", error="同じ Issue の先行イベントが未送信"))
            continue
        if not force and not event.due(now):
            held.add(key)
            results.append(Outcome(event, "waiting", error=f"再試行予定 {event.next_attempt_at}"))
            continue
        with spool.lock(event.repo, event.issue) as acquired:
            if not acquired:
                held.add(key)
                results.append(Outcome(event, "locked", error="別プロセスが処理中"))
                continue
            # Queue bookkeeping can fail where the queue file is read-only (e.g. a sandboxed
            # process seeing the main queue). The event then simply stays for the next sync.
            try:
                plan = engine.apply(event)
            except TransientError as exc:
                _quietly(spool.retry_later, event, str(exc), now)
                held.add(key)
                results.append(Outcome(event, "queued", error=str(exc)))
                continue
            except AutoPulseError as exc:
                _quietly(spool.fail, event, str(exc))
                results.append(Outcome(event, "failed", error=str(exc), code=int(exc.exit_code)))
                continue
            _quietly(spool.done, event)
            status = plan.outcome
            if status == "applied" and plan.conflicts:
                status = "conflict"
            results.append(Outcome(event, status, plan))
    return results
