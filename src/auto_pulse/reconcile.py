"""`autopulse reconcile`: find Issue / Project / queue inconsistencies. Fixes only with --apply."""

import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from auto_pulse import body as managed_body
from auto_pulse.config import Config
from auto_pulse.github import GhClient
from auto_pulse.spool import Spool

AI_LABEL = "ai-managed"


@dataclass
class Finding:
    kind: str
    repo: str | None
    number: int | None
    message: str
    fixable: bool = False  # --apply may fix it (only safe, additive fixes)
    url: str | None = None


_BLOCKERS_SECTION = re.compile(r"^#{1,6}\s*Blockers\s*$\n(.*?)(?=^#{1,6}\s|\Z)", re.M | re.S | re.I)


def _blocker_reason(text: str, managed) -> str:
    """Reason from the managed block, else from the issue form's Blockers section."""
    reason = managed.values.get("Blockers", "")
    if reason and reason != "None":
        return reason
    m = _BLOCKERS_SECTION.search(managed_body.outside(text).replace("\r\n", "\n"))
    section = m.group(1).strip() if m else ""
    return "" if section.lower() in ("", "none", "_no response_") else section


def run(client: GhClient, cfg: Config, spool: Spool, repos: list[str], stale_days: int = 14, today: date | None = None) -> list[Finding]:
    today = today or date.today()
    F = cfg.field
    findings: list[Finding] = []
    add = findings.append
    allowed = {r.lower() for r in cfg.repos}
    targets = {r.lower() for r in repos} if repos else allowed

    items = client.project_items(cfg.owner, cfg.project_number)
    on_board = set()
    for item in items:
        repo, number = item["repo"], item["number"]
        if item["type"] != "Issue":
            if item["type"] == "PullRequest":
                add(Finding("pr-on-board", repo, number, "PR が Projects の項目になっています（タスクは Issue で管理）", url=item["url"]))
            continue
        on_board.add((repo.lower(), number))
        if repo.lower() not in targets:
            if repo.lower() not in allowed:
                add(Finding("not-allowlisted", repo, number, "allowlist 外のリポジトリの Issue が Projects にあります（書き込み対象外）", url=item["url"]))
            continue
        fields = item["fields"]
        status = fields.get(F("status"))
        managed = managed_body.parse(item["body"])
        if item["state"] == "OPEN" and status == "Done":
            add(Finding("done-but-open", repo, number, "Status が Done ですが Issue が open のままです", url=item["url"]))
        if item["state"] == "CLOSED" and status not in ("Done", None):
            add(Finding("closed-not-done", repo, number, f"Issue は closed ですが Status が {status} です", url=item["url"]))
        if item["state"] == "OPEN" and not status:
            add(Finding("no-status", repo, number, "Projects にありますが Status が未設定です", url=item["url"]))
        if status == "In Progress":
            last = fields.get(F("last_activity"))
            if not last:
                add(Finding("stale", repo, number, "In Progress ですが Last Activity が一度も記録されていません", url=item["url"]))
            elif date.fromisoformat(last) < today - timedelta(days=stale_days):
                add(Finding("stale", repo, number, f"In Progress のまま {last} 以降の活動記録がありません", url=item["url"]))
        if status == "Done" and not managed.values.get("Evidence"):
            add(Finding("done-without-evidence", repo, number, "Done ですが管理欄に検証の証跡がありません", url=item["url"]))
        if status == "Blocked" and not _blocker_reason(item["body"], managed):
            add(Finding("blocked-without-reason", repo, number, "Blocked ですが Blocker の理由が記録されていません", url=item["url"]))
        if managed.present and not managed.intact:
            add(Finding("managed-edited", repo, number, "管理欄が手動編集されています（AutoPulse は更新を止めています）", url=item["url"]))
        if status and managed.present and managed.intact and managed.values.get("Status") not in (None, status):
            add(Finding(
                "status-mismatch", repo, number,
                f"Projects の Status ({status}) と管理欄 ({managed.values.get('Status')}) が異なります（手動変更の可能性）",
                url=item["url"],
            ))

    for repo in sorted(targets):
        if repo not in allowed:
            add(Finding("not-allowlisted", repo, None, "指定リポジトリが allowlist にありません"))
            continue
        canonical = next(r for r in cfg.repos if r.lower() == repo)
        for snap in client.open_issues(canonical, cfg.owner, cfg.project_number):
            if AI_LABEL in snap.labels and (repo, snap.number) not in on_board:
                add(Finding("missing-from-board", canonical, snap.number, f"{AI_LABEL} ラベル付きですが Projects にありません", fixable=True, url=snap.url))

    for event in spool.pending():
        add(Finding("pending-event", event.repo, event.issue, f"未送信イベント {event.type}（試行 {event.attempts}）: autopulse sync"))
    for event in spool.failed_events():
        add(Finding("failed-event", event.repo, event.issue, f"失敗イベント {event.type}: {event.last_error}（autopulse sync --retry-failed）"))
    return findings


def apply(client: GhClient, cfg: Config, findings: list[Finding]) -> list[Finding]:
    fixed = []
    for finding in findings:
        if finding.fixable and finding.kind == "missing-from-board":
            client.add_item(cfg.owner, cfg.project_number, finding.url)
            fixed.append(finding)
    return fixed


def to_json(findings: list[Finding]) -> list[dict]:
    return [asdict(f) for f in findings]
