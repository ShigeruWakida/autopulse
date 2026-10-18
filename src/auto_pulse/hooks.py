"""`autopulse hook <event>`: entry point for Claude Code / Codex hooks.

Always best effort: it never raises and the CLI always exits 0, so a broken network,
missing config or an unknown repository never gets in the way of development.
Stop / SessionEnd print nothing (Codex requires empty or JSON stdout for Stop).
"""

import json
import os

from auto_pulse import config as config_mod
from auto_pulse.config import Config
from auto_pulse.engine import Engine, process
from auto_pulse.github import GhClient, origin_of
from auto_pulse.spool import Spool

SHOWN_STATUSES = ("In Progress", "Blocked", "Review", "Ready")
MAX_ISSUES = 10


def _payload(stdin_text: str) -> dict:
    try:
        data = json.loads(stdin_text) if stdin_text.strip() else {}
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def session_context(client: GhClient, cfg: Config, spool: Spool, repo: str, agent: str | None) -> str:
    issues = client.open_issues(repo, cfg.owner, cfg.project_number)
    status_key = cfg.field("status")
    rows = []
    for snap in issues:
        status = snap.fields.get(status_key)
        if status in SHOWN_STATUSES:
            rows.append((SHOWN_STATUSES.index(status), snap, status))
    rows.sort(key=lambda r: (r[0], r[1].number))
    lines = [f"[AutoPulse] {repo} の進行中タスク（GitHub Projects「{cfg.project_title}」）"]
    if not rows:
        lines.append("- 進行中・待機中の Issue はありません")
    for _, snap, status in rows[:MAX_ISSUES]:
        lines.append(f"- #{snap.number} [{status}] {snap.title}")
        next_action = snap.fields.get(cfg.field("next_action"))
        if next_action:
            lines.append(f"  次: {next_action}")
        last = snap.fields.get(cfg.field("last_activity"))
        if last:
            lines.append(f"  最終活動: {last}")
    if len(rows) > MAX_ISSUES:
        lines.append(f"- ほか {len(rows) - MAX_ISSUES} 件（autopulse status --repo {repo}）")
    pending = [e for e in spool.pending() if e.repo.lower() == repo.lower()]
    if pending:
        lines.append(f"未送信イベント {len(pending)} 件（autopulse sync）")
    flag = f" --agent {agent}" if agent else ""
    lines.append(
        f"作業する Issue を決めたら autopulse start --repo {repo} --issue <番号>{flag} を実行し、"
        ".ai/workflow.md の手順で進捗を記録してください。"
    )
    return "\n".join(lines)


def run(event: str, agent: str | None, stdin_text: str, client: GhClient) -> str:
    try:
        payload = _payload(stdin_text)
        cwd = payload.get("cwd") or os.getcwd()
        spool = Spool(config_mod.home() / "queue")
        if event in ("stop", "session-end") and not spool.pending():
            return ""  # nothing to send: no network, no delay
        cfg = config_mod.load()
        repo = origin_of(cwd)
        if event == "session-start":
            if not repo or not cfg.allows(repo):
                return ""
            return session_context(client, cfg, spool, repo, agent)
        process(Engine(client, cfg), spool)
        return ""
    except Exception:  # noqa: BLE001 - hooks must never break the session
        return ""
