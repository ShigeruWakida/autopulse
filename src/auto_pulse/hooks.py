"""`autopulse hook <event>`: entry point for Claude Code / Codex hooks.

Always best effort: it never raises and the CLI always exits 0, so a broken network,
missing config or an unknown repository never gets in the way of development.
Stop / SessionEnd print nothing (Codex requires empty or JSON stdout for Stop).
"""

import json
import os
import time
from datetime import datetime

from auto_pulse import config as config_mod
from auto_pulse.redact import redact
from auto_pulse.config import Config
from auto_pulse.engine import Engine, process
from auto_pulse.github import GhClient, origin_of
from auto_pulse.spool import Spool, default_spool

SHOWN_STATUSES = ("In Progress", "Blocked", "Review", "Ready")
MAX_ISSUES = 10
HOOK_BUDGET = {"stop": 35.0, "session-end": 10.0}  # seconds before no new event is started


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
        status = cfg.logical(snap.fields.get(status_key))
        if status in SHOWN_STATUSES:
            rows.append((SHOWN_STATUSES.index(status), snap, cfg.label(status)))
    rows.sort(key=lambda r: (r[0], r[1].number))
    lines = [f"[AutoPulse] {repo} の進行中タスク（GitHub Projects「{cfg.project_title}」）"]
    if not rows:
        lines.append("- 進行中・待機中の Issue はありません")
    for _, snap, status in rows[:MAX_ISSUES]:
        agent_label = snap.fields.get(cfg.field("ai_agent"))
        lines.append(f"- #{snap.number} [{status}] {snap.title}" + (f"（AI Agent: {agent_label}）" if agent_label else ""))
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


def log(event: str, started: float, outcome: str) -> None:
    """One line per hook run in %LOCALAPPDATA%/AutoPulse/hook.log, kept small."""
    try:
        path = config_mod.home() / "hook.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 256_000:
            path.replace(path.with_suffix(".log.1"))
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        line = f"{stamp} {event} {time.monotonic() - started:.1f}s pid={os.getpid()} {redact(outcome)}\n"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def run(event: str, agent: str | None, stdin_text: str, client: GhClient) -> str:
    started = time.monotonic()
    outcome = "ok"
    try:
        payload = _payload(stdin_text)
        cwd = payload.get("cwd") or os.getcwd()
        spool = default_spool()
        if event in ("stop", "session-end") and not spool.pending():
            outcome = "queue empty"
            return ""  # nothing to send: no network, no delay
        cfg = config_mod.load()
        repo = origin_of(cwd)
        if event == "session-start":
            if not repo or not cfg.allows(repo):
                outcome = f"skip repo={repo} cwd={cwd}"
                return ""
            text = session_context(client, cfg, spool, repo, agent)
            outcome = f"context {len(text)} chars repo={repo}"
            # JSON, not plain text: Codex tries to parse stdout starting with "[" as JSON and
            # marks the hook failed. Both Claude Code and Codex accept additionalContext.
            return json.dumps(
                {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                ensure_ascii=False,
            )
        # force: events queued inside the Codex sandbox got a backoff from a failure that was
        # never a real GitHub attempt (no keyring there). Stop fires at most once per turn.
        # One event takes up to ~15 s of gh calls; stay inside the hook timeouts
        # (Stop 60 s, SessionEnd 30 s in the onboard templates).
        results = process(Engine(client, cfg), spool, force=True, budget=HOOK_BUDGET.get(event, 10))
        outcome = "sync " + ",".join(r.status for r in results)
        return ""
    except Exception as exc:  # noqa: BLE001 - hooks must never break the session
        outcome = f"error {type(exc).__name__}: {exc}"
        return ""
    finally:
        log(event, started, outcome)
