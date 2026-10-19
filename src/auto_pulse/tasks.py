"""`autopulse create`: issues the AI files for itself."""

from dataclasses import dataclass, field

from auto_pulse.config import Config
from auto_pulse.github import GhClient


@dataclass
class Task:
    title: str
    priority: str = "P2"
    category: str = "Other"
    summary: str = ""
    criteria: list[str] = field(default_factory=list)
    verification: str = ""
    next_action: str = ""


def task_body(task: Task, note: str = "") -> str:
    parts = [
        "### Summary", task.summary, "",
        "### Acceptance Criteria", *[f"- [ ] {c}" for c in task.criteria or ["作業が終わっている"]], "",
        "### Verification", task.verification or "完了条件を確認する", "",
        "### Next Action", task.next_action or "着手方法を決める", "",
        "### Blockers", "None", "",
        "### Notes", note or "AI が AutoPulse で登録。",
    ]
    return "\n".join(parts) + "\n"


def create_task_issue(client: GhClient, cfg: Config, repo: str, task: Task, body_text: str, status: str = "Backlog") -> tuple[str, bool]:
    """Create the issue (or reuse one with the same title) and put it on the Project. Returns (url, created)."""
    existing = client.issue_titles(repo).get(task.title)
    url = existing or client.create_issue(repo, task.title, body_text, ["ai-managed"])
    if not existing:
        values = {
            "status": cfg.label(status),
            "priority": task.priority,
            "category": task.category,
            "repo_name": repo.split("/")[-1],
        }
        client.set_fields(cfg.owner, cfg.project_number, url, {cfg.field(k): v for k, v in values.items()})
    return url, not existing
