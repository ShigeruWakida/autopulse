"""`autopulse import-decisions`: bring a decision record from a ChatGPT discussion into the repo.

ChatGPT stays read-only towards GitHub (spec §9). A human pastes its decision record into a
file; this module turns it into: an entry in docs/DECISIONS.md, a ChatGPT note on the related
issue, new issues for new work, and the record itself under docs/records/. Status is never
changed here - a person closes decision issues.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from auto_pulse.config import FIELD_SPECS, Config
from auto_pulse.errors import AutoPulseError
from auto_pulse.github import GhClient
from auto_pulse.spool import Event

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "decision-record.md"
EVENT_NAMESPACE = uuid.UUID("6f3c2b1e-9a43-4d2e-8f5e-2c7d1a9b4e10")
SECTIONS = {
    "決定": "decisions", "decisions": "decisions",
    "新しくやること": "tasks", "新しい作業": "tasks", "new tasks": "tasks",
    "未決": "open", "未決事項": "open", "open": "open",
}
_ITEM = re.compile(r"^\s*-\s*([^:：]+?)\s*[:：]\s*(.*)$")
_CHECK = re.compile(r"^\s+[-*]\s+(?:\[[ xX]\]\s*)?(.+)$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_NONE = {"", "なし", "none", "-", "—"}


@dataclass
class Decision:
    title: str
    content: str = ""
    reason: str = ""
    date: str = ""
    issue: int | None = None


@dataclass
class Task:
    title: str
    priority: str = "P2"
    category: str = "Other"
    summary: str = ""
    criteria: list[str] = field(default_factory=list)
    verification: str = ""
    next_action: str = ""


@dataclass
class OpenItem:
    title: str
    state: str = ""


@dataclass
class Record:
    text: str
    title: str
    date: str
    decisions: list[Decision] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    open_items: list[OpenItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def digest(self) -> str:
        normalized = "\n".join(line.rstrip() for line in self.text.replace("\r\n", "\n").strip().split("\n"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


def template(repo_name: str) -> str:
    return TEMPLATE.read_bytes().decode("utf-8").replace("\r\n", "\n").replace("{{REPO_NAME}}", repo_name)


def _issue_number(value: str) -> int | None:
    m = re.search(r"#?(\d+)", value)
    return int(m.group(1)) if m and value.strip().lower() not in _NONE else None


def parse(text: str) -> Record:
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    heading = next((l[2:].strip() for l in lines if l.startswith("# ")), "決定記録")
    date = (_DATE.search(heading) or _DATE.search(text))
    record = Record(text=text, title=heading, date=date.group(0) if date else "")
    if not record.date:
        record.warnings.append("記録の日付（YYYY-MM-DD）が見つかりません")

    section, current, list_key = None, None, None
    for line in lines:
        if line.startswith("## "):
            section = SECTIONS.get(line[3:].strip().lower()) or SECTIONS.get(line[3:].strip())
            current, list_key = None, None
            if section is None:
                record.warnings.append(f"知らない節は読み飛ばしました: {line.strip()}")
            continue
        if section is None or line.startswith("```"):
            continue
        if line.startswith("### "):
            title = line[4:].strip()
            current = {"decisions": Decision, "tasks": Task, "open": OpenItem}[section](title=title)
            getattr(record, {"decisions": "decisions", "tasks": "tasks", "open": "open_items"}[section]).append(current)
            list_key = None
            continue
        if current is None:
            continue
        if list_key and _CHECK.match(line):
            current.criteria.append(_CHECK.match(line).group(1).strip())
            continue
        m = _ITEM.match(line)
        if not m:
            continue
        key, value = m.group(1).strip(), m.group(2).strip()
        list_key = None
        if isinstance(current, Decision):
            if key.lower() == "issue":
                current.issue = _issue_number(value)
            elif key == "内容":
                current.content = value
            elif key == "理由":
                current.reason = value
            elif key == "日付":
                current.date = value
        elif isinstance(current, Task):
            if key == "優先度":
                current.priority = value.upper() if value.upper() in ("P0", "P1", "P2", "P3") else current.priority
            elif key == "分類":
                options = FIELD_SPECS["category"][2]
                current.category = next((o for o in options if o.lower() == value.lower()), "Other")
            elif key == "概要":
                current.summary = value
            elif key == "完了条件":
                list_key = "criteria"
                if value and value.lower() not in _NONE:
                    current.criteria.append(value)
            elif key == "検証":
                current.verification = value
            elif key == "次の一手":
                current.next_action = value
        elif isinstance(current, OpenItem) and key in ("状態", "内容"):
            current.state = value

    for d in record.decisions:
        d.date = d.date or record.date
        if not d.content:
            record.warnings.append(f"決定「{d.title}」に内容がありません（取り込みません）")
    for t in record.tasks:
        if not t.summary or not t.criteria:
            record.warnings.append(f"作業「{t.title}」に概要か完了条件がありません（取り込みません）")
    record.decisions = [d for d in record.decisions if d.content]
    record.tasks = [t for t in record.tasks if t.summary and t.criteria]
    if not (record.decisions or record.tasks or record.open_items):
        raise AutoPulseError("決定記録に取り込める項目がありません（書式は autopulse decision-template を参照）")
    return record


def task_body(task: Task, record: Record, record_path: str) -> str:
    parts = [
        "### Summary", task.summary, "",
        "### Acceptance Criteria", *[f"- [ ] {c}" for c in task.criteria], "",
        "### Verification", task.verification or "完了条件を確認する", "",
        "### Next Action", task.next_action or "着手方法を決める", "",
        "### Blockers", "None", "",
        "### Related decisions", f"{record.title}（{record_path}）", "",
        "### Notes", f"ChatGPT の決定記録（{record.date or '日付なし'}）から登録。",
    ]
    return "\n".join(parts) + "\n"


def decisions_entry(record: Record, index: int, decision: Decision, record_path: str) -> str:
    lines = [
        f"<!-- autopulse:record={record.digest}:{index} -->",
        f"## {decision.date or record.date} {decision.title}",
        f"- 判断: {decision.content}",
    ]
    if decision.reason:
        lines.append(f"- 理由: {decision.reason}")
    if decision.issue:
        lines.append(f"- 関連: #{decision.issue}")
    lines.append(f"- 出典: ChatGPT の決定記録（{record_path}）")
    return "\n".join(lines) + "\n"


def note_event(record: Record, index: int, decision: Decision, repo: str) -> Event:
    summary = f"ChatGPT で決定: {decision.content}" + (f"（理由: {decision.reason}）" if decision.reason else "")
    return Event(
        id=str(uuid.uuid5(EVENT_NAMESPACE, f"{repo.lower()}:{record.digest}:{index}")),
        type="note",
        repo=repo,
        issue=decision.issue,
        agent="chatgpt",
        data={"summary": summary, "next": "決定内容を確認し、完了条件にチェックを入れて完了にする"},
    )


@dataclass
class ImportPlan:
    record: Record
    repo: str
    root: Path
    record_path: str
    already_imported: bool = False
    new_decision_entries: list[tuple[int, Decision]] = field(default_factory=list)
    notes: list[Event] = field(default_factory=list)
    new_tasks: list[Task] = field(default_factory=list)
    existing_tasks: list[Task] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def build(client: GhClient, cfg: Config, repo: str, root: Path, record: Record) -> ImportPlan:
    record_path = f"docs/records/{record.date or 'undated'}-{record.digest}.md"
    plan = ImportPlan(record=record, repo=repo, root=root, record_path=record_path, warnings=list(record.warnings))
    plan.already_imported = (root / record_path).exists()

    decisions_md = root / "docs" / "DECISIONS.md"
    existing = decisions_md.read_bytes().decode("utf-8") if decisions_md.exists() else ""
    for i, decision in enumerate(record.decisions):
        if f"autopulse:record={record.digest}:{i} " not in existing:
            plan.new_decision_entries.append((i, decision))
        if decision.issue:
            try:
                client.get_issue(repo, decision.issue, cfg.owner, cfg.project_number)
            except AutoPulseError as exc:
                plan.warnings.append(f"#{decision.issue} を確認できないためコメントしません: {exc}")
                continue
            plan.notes.append(note_event(record, i, decision, repo))

    titles = client.issue_titles(repo) if record.tasks else {}
    for task in record.tasks:
        (plan.existing_tasks if task.title in titles else plan.new_tasks).append(task)
    return plan


def apply(client: GhClient, cfg: Config, plan: ImportPlan, spool, engine_factory) -> list[str]:
    """Write files, queue the ChatGPT notes, create issues. Returns the created issue URLs."""
    from auto_pulse.engine import process

    root, record = plan.root, plan.record
    target = root / plan.record_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(record.text.replace("\r\n", "\n").encode("utf-8"))

    if plan.new_decision_entries:
        decisions_md = root / "docs" / "DECISIONS.md"
        old = decisions_md.read_bytes().decode("utf-8") if decisions_md.exists() else "# 設計判断の記録\n"
        crlf = "\r\n" in old
        text = old.replace("\r\n", "\n").rstrip("\n") + "\n\n" + "\n".join(
            decisions_entry(record, i, d, plan.record_path) for i, d in plan.new_decision_entries
        )
        decisions_md.parent.mkdir(parents=True, exist_ok=True)
        decisions_md.write_bytes((text.replace("\n", "\r\n") if crlf else text).encode("utf-8"))

    for event in plan.notes:
        spool.put(event)  # write-ahead; same id on re-import, so the comment is never duplicated
    if plan.notes:
        process(engine_factory(), spool)

    created = []
    repo_name = plan.repo.split("/")[-1]
    for task in plan.new_tasks:
        url = client.create_issue(plan.repo, task.title, task_body(task, record, plan.record_path), ["ai-managed"])
        values = {"status": cfg.label("Backlog"), "priority": task.priority, "category": task.category, "repo_name": repo_name}
        client.set_fields(cfg.owner, cfg.project_number, url, {cfg.field(k): v for k, v in values.items()})
        created.append(url)
    return created
