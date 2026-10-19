"""Command line entry point: `autopulse <command>`."""

import argparse
import json
import os
import sys
from pathlib import Path

from auto_pulse import __version__, body, bootstrap, doctor, hooks, onboard, reconcile, records
from auto_pulse import config as config_mod
from auto_pulse.config import AGENTS, FIELD_SPECS
from auto_pulse.engine import Engine, process
from auto_pulse.errors import AutoPulseError, ExitCode, PolicyRefused, TransientError
from auto_pulse.github import GhClient, origin_of
from auto_pulse.redact import redact
from auto_pulse.runner import SubprocessRunner
from auto_pulse.spool import Event, default_spool

OUTCOME_CODES = {
    "applied": ExitCode.OK,
    "noop": ExitCode.OK,
    "downgraded": ExitCode.REFUSED,
    "conflict": ExitCode.CONFLICT,
    "queued": ExitCode.QUEUED,
    "waiting": ExitCode.QUEUED,
    "locked": ExitCode.QUEUED,
}
OUTCOME_LABELS = {
    "applied": "反映しました",
    "noop": "反映済みのため何もしませんでした",
    "downgraded": "完了（Done）にせず確認待ち（Review）に留めました",
    "conflict": "反映しました（競合あり。現行値を保持）",
    "queued": "GitHub に反映できなかったためキューに保存しました（autopulse sync で再送）",
    "waiting": "先行イベントの送信待ちのためキューに保存しました",
    "locked": "別プロセスが同じ Issue を処理中のためキューに保存しました",
    "failed": "失敗しました",
}


class Ctx:
    def __init__(self, args):
        self.args = args
        # Hooks run inside an agent session with their own timeout: keep each gh call short.
        self.client = GhClient(SubprocessRunner(timeout=15 if args.command == "hook" else 60))
        self.spool = default_spool()

    def config(self):
        return config_mod.load(Path(self.args.config) if self.args.config else None)

    def out(self, human: str | list[str], data) -> None:
        if self.args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif not getattr(self.args, "quiet", False):
            print(redact(human if isinstance(human, str) else "\n".join(human)))


# -- helpers -----------------------------------------------------------------
def resolve_repo(args) -> str:
    repo = args.repo or origin_of()
    if not repo:
        raise AutoPulseError("--repo owner/name を指定してください（origin から推定できませんでした）")
    return repo


# -- commands ----------------------------------------------------------------
def cmd_doctor(ctx: Ctx) -> int:
    checks = doctor.run(ctx.client, Path(ctx.args.config) if ctx.args.config else None)
    marks = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}
    lines = []
    for c in checks:
        lines.append(f"[{marks[c.level]}] {c.name}: {c.detail}")
        if c.fix and c.level != "ok":
            lines.append(f"       対処: {c.fix}")
    ctx.out(lines, doctor.to_json(checks))
    return ExitCode.SETUP if any(c.level == "fail" for c in checks) else ExitCode.OK


def cmd_init(ctx: Ctx) -> int:
    a = ctx.args
    plan = bootstrap.build(ctx.client, a.owner, a.title, a.project, a.repo or [])
    lines = bootstrap.describe(plan)
    if plan.config_diff:
        lines += ["", plan.config_diff]
    data = {
        "create_project": plan.create_project,
        "number": plan.number,
        "create_fields": plan.create_fields,
        "replace_status_options": plan.replace_status_options,
        "conflicts": plan.conflicts,
        "config_diff": plan.config_diff,
        "applied": False,
    }
    if not a.apply:
        lines += ["", "（dry-run: 何も変更していません。内容を確認して --apply で実行）"]
        ctx.out(lines, data)
        return ExitCode.SETUP if plan.conflicts else ExitCode.OK
    cfg = bootstrap.apply(ctx.client, plan, a.update_status_options)
    data.update(applied=True, number=cfg.project_number, config=str(config_mod.config_path()))
    ctx.out(
        lines
        + [
            "",
            f"初期化しました: Project #{cfg.project_number} / 設定 {config_mod.config_path()}",
            "表の列は自動では増えません（GitHub に View を変更する API がないため）。Project の画面で見出し右端の「+」から",
            "Repo Name / Priority / Next Action / Last Activity / AI Agent / Category を表示して Save してください。",
        ],
        data,
    )
    return ExitCode.OK


def _snap_row(snap, cfg) -> dict:
    f = snap.fields
    get = (lambda k: f.get(cfg.field(k))) if cfg else (lambda k: None)
    return {
        "number": snap.number,
        "title": snap.title,
        "url": snap.url,
        "in_project": snap.item_id is not None,
        "status": get("status"),
        "priority": get("priority"),
        "last_activity": get("last_activity"),
        "next_action": get("next_action") or body.parse(snap.body).values.get("Next Action"),
        "agent": get("ai_agent"),
    }


def cmd_status(ctx: Ctx) -> int:
    a = ctx.args
    repo = resolve_repo(a)
    try:
        cfg = ctx.config()
    except AutoPulseError:
        cfg = None
    owner, number = (cfg.owner, cfg.project_number) if cfg else (None, None)
    pending = [e for e in ctx.spool.pending() if e.repo.lower() == repo.lower()]
    if a.issue:
        snap = ctx.client.get_issue(repo, a.issue, owner, number)
        row = _snap_row(snap, cfg)
        managed = body.parse(snap.body)
        criteria = body.acceptance_criteria(snap.body)
        row["managed"] = managed.values
        row["managed_intact"] = managed.intact
        row["acceptance_criteria"] = None if criteria is None else [{"checked": c.checked, "text": c.text} for c in criteria]
        row["pending_events"] = [e.type for e in pending if e.issue == a.issue]
        lines = [f"{repo}#{snap.number} {snap.title}", f"  {snap.url}"]
        for key in ("status", "priority", "agent", "last_activity", "next_action"):
            lines.append(f"  {key:14} {row[key] or '-'}")
        if managed.present:
            lines.append("  管理欄" + ("" if managed.intact else "（手動編集あり）") + ": " + ", ".join(f"{k}={v}" for k, v in managed.values.items()))
        if criteria is None:
            lines.append("  Acceptance Criteria: なし")
        else:
            done = sum(c.checked for c in criteria)
            lines.append(f"  Acceptance Criteria: {done}/{len(criteria)}")
            lines += [f"    [{'x' if c.checked else ' '}] {c.text}" for c in criteria]
        if row["pending_events"]:
            lines.append(f"  未送信イベント: {', '.join(row['pending_events'])}")
        ctx.out(lines, row)
        return ExitCode.OK

    snaps = ctx.client.open_issues(repo, owner, number)
    rows = [_snap_row(s, cfg) for s in snaps]
    lines = [f"{repo}: open issues {len(rows)}" + ("" if cfg else "（設定なし: Projects の値は表示しません）")]
    for r in rows:
        status = r["status"] or ("(未登録)" if not r["in_project"] else "-")
        lines.append(f"  #{r['number']:<5} {status:12} {r['last_activity'] or '-':10}  {r['title'][:50]}")
        if r["next_action"]:
            lines.append(f"         次: {r['next_action']}")
    if pending:
        lines.append(f"未送信イベント {len(pending)} 件（autopulse sync）")
    ctx.out(lines, {"repo": repo, "issues": rows, "pending_events": len(pending)})
    return ExitCode.OK


def _event_from_args(a, repo: str) -> Event:
    keys = {
        "start": ["next", "reason"],
        "note": ["summary", "next"],
        "block": ["reason", "next"],
        "review": ["evidence", "summary", "next"],
        "complete": ["evidence", "summary", "verified", "close"],
    }[a.command]
    data = {k: getattr(a, k) for k in keys if getattr(a, k, None) not in (None, False, "")}
    return Event(type=a.command, repo=repo, issue=a.issue, agent=a.agent, data=data)


def _plan_lines(plan) -> list[str]:
    lines = [f"  - {op.describe()}" for op in plan.ops]
    lines += [f"  注: {n}" for n in plan.notes]
    lines += [f"  競合: {c}" for c in plan.conflicts]
    return lines


def _plan_json(plan) -> dict:
    return {
        "ops": [op.describe() for op in plan.ops],
        "status_before": plan.status_before,
        "status_after": plan.status_after,
        "notes": plan.notes,
        "conflicts": plan.conflicts,
    }


def cmd_event(ctx: Ctx) -> int:
    a = ctx.args
    repo = resolve_repo(a)
    cfg = ctx.config()
    if not cfg.allows(repo):
        raise PolicyRefused(f"{repo} は allowlist（config の [repos] allow）にありません。書き込みません")
    event = _event_from_args(a, repo)
    engine = Engine(ctx.client, cfg)
    label = f"{repo}#{a.issue} {a.command}"

    if a.dry_run:
        try:
            plan = engine.plan(event)
        except TransientError as exc:
            ctx.out(f"{label}: GitHub に接続できないため計画を作れません: {exc}", {"outcome": "unknown", "error": str(exc)})
            return ExitCode.QUEUED
        outcome = plan.outcome if not plan.conflicts or plan.outcome != "applied" else "conflict"
        ctx.out([f"{label}（dry-run: 何も変更していません）", *_plan_lines(plan)], {"dry_run": True, "outcome": outcome, **_plan_json(plan)})
        return OUTCOME_CODES.get(outcome, ExitCode.OK)

    ctx.spool.put(event)  # write-ahead: the event survives a crash or network loss from here on
    if a.queue_only:
        ctx.out(f"{label}: キューに保存しました（{event.id}）", {"event_id": event.id, "outcome": "queued"})
        return ExitCode.QUEUED
    results = process(engine, ctx.spool, only=(repo, a.issue))
    mine = next((r for r in results if r.event.id == event.id), None)
    if mine is None:
        ctx.out(f"{label}: キューに保存しました（{event.id}）", {"event_id": event.id, "outcome": "queued"})
        return ExitCode.QUEUED
    lines = [f"{label}: {OUTCOME_LABELS[mine.status]}"]
    if mine.plan:
        lines += _plan_lines(mine.plan)
    if mine.error:
        lines.append(f"  {mine.error}")
    data = {"event_id": event.id, "outcome": mine.status, "error": mine.error}
    if mine.plan:
        data.update(_plan_json(mine.plan))
    ctx.out(lines, data)
    if mine.status == "failed":
        return mine.code or ExitCode.ERROR
    return OUTCOME_CODES[mine.status]


def cmd_sync(ctx: Ctx) -> int:
    a = ctx.args
    if a.dry_run:
        pending = ctx.spool.pending()
        lines = [f"未送信 {len(pending)} 件（dry-run: 送信しません）"]
        lines += [f"  {e.created_at} {e.repo}#{e.issue} {e.type} 試行{e.attempts} {e.last_error or ''}" for e in pending]
        ctx.out(lines, [{"id": e.id, "repo": e.repo, "issue": e.issue, "type": e.type, "attempts": e.attempts, "last_error": e.last_error} for e in pending])
        return ExitCode.OK
    if a.retry_failed:
        requeued = ctx.spool.requeue_failed()
        if requeued and not a.json:
            ctx.out(f"失敗イベント {len(requeued)} 件をキューに戻しました", None)
    pending = ctx.spool.pending()
    if not pending:
        failed = len(ctx.spool.failed_events())
        note = f"（失敗 {failed} 件: --retry-failed で再送）" if failed else ""
        ctx.out("未送信イベントはありません" + note, {"results": [], "failed": failed})
        return ExitCode.OK
    results = process(Engine(ctx.client, ctx.config()), ctx.spool, force=a.force)
    lines = [f"{r.event.repo}#{r.event.issue} {r.event.type}: {OUTCOME_LABELS[r.status]}" + (f" {r.error}" if r.error else "") for r in results]
    ctx.out(lines, {"results": [{"id": r.event.id, "outcome": r.status, "error": r.error} for r in results]})
    if any(r.status == "failed" for r in results):
        return ExitCode.ERROR
    if any(r.status in ("queued", "waiting", "locked") for r in results):
        return ExitCode.QUEUED
    return ExitCode.OK


def cmd_reconcile(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.config()
    findings = reconcile.run(ctx.client, cfg, ctx.spool, a.repo or [], stale_days=a.stale_days)
    lines = [f"検出 {len(findings)} 件" + ("" if a.apply else "（dry-run: 何も変更していません）")]
    for f in findings:
        where = f"{f.repo}#{f.number}" if f.number else (f.repo or "-")
        lines.append(f"  [{f.kind}] {where}: {f.message}" + ("（--apply で修正可）" if f.fixable and not a.apply else ""))
    fixed = reconcile.apply(ctx.client, cfg, findings) if a.apply else []
    if a.apply:
        lines.append(f"修正 {len(fixed)} 件（Projects への追加と Repo Name 列の補完だけ。その他は報告のみ）")
    ctx.out(lines, {"findings": reconcile.to_json(findings), "fixed": reconcile.to_json(fixed), "applied": a.apply})
    return ExitCode.OK


def cmd_onboard(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.config()
    root = Path(a.path).resolve() if a.path else Path.cwd()
    plan = onboard.build(ctx.client, cfg, a.repo, root, update_workflow=a.update_workflow)
    labels = {"create": "作成", "append": "追記", "update": "更新", "merge": "マージ", "skip": "変更しない", "unchanged": "変更なし"}
    lines = [f"{a.repo} ({root})"]
    for change in plan.files:
        lines.append(f"  [{labels[change.action]}] {change.path}" + (f" — {change.reason}" if change.reason else ""))
    lines.append(f"  [GitHub] ラベル ai-managed: {'作成' if plan.create_label else '既存'}")
    lines.append(f"  [設定] allowlist: {'追加' if plan.add_to_allowlist else '登録済み'}")
    diffs = [c.diff() for c in plan.files if c.action in ("create", "append", "update", "merge")]
    data = {
        "repo": a.repo,
        "root": str(root),
        "files": [{"path": c.path, "action": c.action, "reason": c.reason} for c in plan.files],
        "create_label": plan.create_label,
        "add_to_allowlist": plan.add_to_allowlist,
        "applied": False,
    }
    if not a.apply:
        if a.diff:
            lines += ["", *diffs]
        lines += ["", "（dry-run: 何も変更していません。--diff で差分、--apply で実行）"]
        ctx.out(lines, data | {"diffs": diffs})
        return ExitCode.OK
    backup = onboard.apply(ctx.client, cfg, plan)
    lines.append(f"導入しました。バックアップ: {backup}" if backup else "導入しました（上書きしたファイルはありません）")
    lines.append("ファイルはコミットしていません。内容を確認してコミットしてください。Codex は /hooks で Hook を信頼する必要があります。")
    ctx.out(lines, data | {"applied": True, "backup": str(backup) if backup else None})
    return ExitCode.OK


def cmd_install_hooks(ctx: Ctx) -> int:
    a = ctx.args
    change = onboard.user_hooks_change()
    target = onboard.codex_user_hooks_path()
    labels = {"create": "作成", "merge": "マージ", "unchanged": "変更なし", "skip": "変更しない"}
    lines = [f"[{labels.get(change.action, change.action)}] {target}" + (f" — {change.reason}" if change.reason else "")]
    data = {"path": str(target), "action": change.action, "reason": change.reason, "applied": False}
    if not a.apply:
        if change.new is not None:
            lines += ["", change.diff()]
        lines.append("（dry-run: 何も変更していません。--apply で実行）")
        ctx.out(lines, data)
        return ExitCode.OK
    backup = onboard.apply_user_hooks(change)
    if change.action in ("create", "merge"):
        lines.append("導入しました。" + (f"バックアップ: {backup}" if backup else ""))
        lines.append("Codex で /hooks を開き、AutoPulse の Hook を信頼してください。")
        lines.append("リポジトリ側に .codex/hooks.json がある場合は二重に動くため削除してください。")
    ctx.out(lines, data | {"applied": True, "backup": str(backup) if backup else None})
    return ExitCode.OK


def _full_repo(cfg, repo: str) -> str:
    """`ScanPilot` -> `<owner>/ScanPilot` using the configured owner."""
    return repo if "/" in repo else f"{cfg.owner}/{repo}"


def cmd_decision_template(ctx: Ctx) -> int:
    a = ctx.args
    name = (a.repo or "（プロジェクト名）").split("/")[-1]
    text = records.template(name)
    ctx.out(text.rstrip("\n"), {"template": text})
    return ExitCode.OK


def cmd_import_decisions(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.config()
    repo = _full_repo(cfg, a.repo)
    if not cfg.allows(repo):
        raise PolicyRefused(f"{repo} は allowlist（config の [repos] allow）にありません。書き込みません")
    root = Path(a.path).resolve() if a.path else Path.cwd()
    origin = origin_of(str(root))
    if not origin or origin.lower() != repo.lower():
        raise PolicyRefused(f"{root} の origin（{origin}）が {repo} と一致しません。--path でクローンを指定してください")
    text = Path(a.file).read_bytes().decode("utf-8-sig")
    record = records.parse(text)
    plan = records.build(ctx.client, cfg, repo, root, record)

    lines = [f"{repo}: {record.title}（記録 {plan.record_path}" + ("、取り込み済み" if plan.already_imported else "") + "）"]
    for i, d in enumerate(record.decisions):
        new = any(i == j for j, _ in plan.new_decision_entries)
        target = "、".join(f"#{n}" for n in d.issues) or "Issue なし"
        closing = {n for j, n in plan.closes if j == i}
        noted = [e.issue for e, j in zip(plan.notes, plan.note_decision) if j == i and e.issue not in closing]
        before = [n for j, n in plan.noted_before if j == i]
        lines.append(f"  [決定] {d.title}（{target}）: DECISIONS.md に{'追記' if new else '記録済み'}"
                     + (f"、{'・'.join(f'#{n}' for n in noted)} に ChatGPT の記録を残す" if noted else "")
                     + (f"、{'・'.join(f'#{n}' for n in sorted(closing))} を完了にして close" if closing else "")
                     + (f"、{'・'.join(f'#{n}' for n in before)} はコメント済み" if before else ""))
    for t in plan.new_tasks:
        lines.append(f"  [新規] Issue「{t.title}」{t.priority} {t.category} 完了条件 {len(t.criteria)} 件 → {cfg.label('Backlog')}")
    for t in plan.existing_tasks:
        lines.append(f"  [既存] Issue「{t.title}」は同じタイトルがあるため作りません")
    for t, refs in plan.referenced_tasks:
        lines.append(f"  [既存参照] 「{t.title}」は {'、'.join(f'#{n}' for n in refs)} の作業なので作りません（内容の違いは Issue を手で直す）")
    for o in record.open_items:
        lines.append(f"  [未決] {o.title}: {o.state or '-'}（記録に残すだけで Issue は作りません）")
    lines += [f"  注意: {w}" for w in plan.warnings]
    data = {
        "repo": repo,
        "record": plan.record_path,
        "already_imported": plan.already_imported,
        "decisions": [{"title": d.title, "issues": d.issues, "new": any(i == j for j, _ in plan.new_decision_entries)} for i, d in enumerate(record.decisions)],
        "referenced_tasks": [{"title": t.title, "issues": refs} for t, refs in plan.referenced_tasks],
        "new_tasks": [t.title for t in plan.new_tasks],
        "existing_tasks": [t.title for t in plan.existing_tasks],
        "open": [o.title for o in record.open_items],
        "warnings": plan.warnings,
        "applied": False,
    }
    if a.dry_run or (not a.apply and cfg.mode != "auto"):
        lines.append("（dry-run: 何も変更していません。" + ("--dry-run を外すと反映します）" if cfg.mode == "auto" else "--apply で実行）"))
        ctx.out(lines, data)
        return ExitCode.OK
    created = records.apply(ctx.client, cfg, plan, ctx.spool, lambda: Engine(ctx.client, cfg))
    lines.append(f"反映しました。作成した Issue: {', '.join(created) or 'なし'}")
    lines.append("docs/ の変更はコミットしていません。内容を確認してコミットしてください。決定の Issue を完了にするのは人です。")
    ctx.out(lines, data | {"applied": True, "created": created})
    return ExitCode.OK


def cmd_create(ctx: Ctx) -> int:
    a = ctx.args
    cfg = ctx.config()
    repo = _full_repo(cfg, a.repo or resolve_repo(a))
    if not cfg.allows(repo):
        raise PolicyRefused(f"{repo} は allowlist（config の [repos] allow）にありません。書き込みません")
    task = records.Task(
        title=a.title,
        priority=a.priority,
        category=a.category,
        summary=a.summary,
        criteria=a.criteria or [],
        verification=a.verification or "",
        next_action=a.next or "",
    )
    text = records.task_body(task, note=f"AI（{AGENTS.get(a.agent or '', '不明')}）が AutoPulse で登録。")
    if a.dry_run:
        ctx.out([f"{repo}: Issue「{a.title}」を作成（dry-run）", text], {"dry_run": True, "title": a.title, "body": text})
        return ExitCode.OK
    url, created = records.create_task_issue(ctx.client, cfg, repo, task, text)
    number = int(url.rsplit("/", 1)[1])
    lines = [f"{repo}#{number}: {'作成しました' if created else '同じタイトルの Issue があるため再利用します'} {url}"]
    data = {"url": url, "number": number, "created": created}
    code = ExitCode.OK
    if a.start:
        event = Event(type="start", repo=repo, issue=number, agent=a.agent, data={"next": a.next} if a.next else {})
        ctx.spool.put(event)
        results = process(Engine(ctx.client, cfg), ctx.spool, only=(repo, number))
        mine = next((r for r in results if r.event.id == event.id), None)
        status = mine.status if mine else "queued"
        lines.append(f"  start: {OUTCOME_LABELS.get(status, status)}")
        data["start"] = status
        code = OUTCOME_CODES.get(status, ExitCode.OK) if status != "failed" else (mine.code or ExitCode.ERROR)
    ctx.out(lines, data)
    return code


def cmd_hook(ctx: Ctx) -> int:
    try:
        stdin_text = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
    except (OSError, ValueError):
        stdin_text = ""
    output = hooks.run(ctx.args.event, ctx.args.agent, stdin_text, ctx.client)
    if output:
        print(output)
    return ExitCode.OK


# -- parser ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="JSON で出力")
    common.add_argument("--quiet", action="store_true", help="成功時は何も出力しない")
    common.add_argument("--config", help="設定ファイルのパス（既定: %%LOCALAPPDATA%%/AutoPulse/config.toml）")

    p = argparse.ArgumentParser(prog="autopulse", description="GitHub Projects/Issues への進捗自動記録")
    p.add_argument("--version", action="version", version=f"autopulse {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", parents=[common], help="環境を read-only で診断")

    s = sub.add_parser("init", parents=[common], help="Project/フィールド/設定を初期化（既定は dry-run）")
    s.add_argument("--owner", required=True)
    s.add_argument("--project", type=int, help="既存 Project の番号")
    s.add_argument("--title", default="Personal Development")
    s.add_argument("--repo", action="append", help="allowlist に加えるリポジトリ（複数可）")
    s.add_argument("--apply", action="store_true", help="表示した差分を実行する")
    s.add_argument("--update-status-options", action="store_true", help="Status の選択肢を仕様の 6 状態に置換（項目 0 件時のみ）")

    s = sub.add_parser("status", parents=[common], help="Issue の状態と次の作業を表示")
    s.add_argument("--repo")
    s.add_argument("--issue", type=int)

    agent_default = os.environ.get("AUTOPULSE_AGENT")
    for name, help_text in [
        ("start", "In Progress にして作業開始を記録"),
        ("note", "作業要約と次の一手を記録"),
        ("block", "Blocked にして理由を記録"),
        ("review", "Review にして証跡を記録"),
        ("complete", "検証済みなら Done（不十分なら Review）"),
    ]:
        s = sub.add_parser(name, parents=[common], help=help_text)
        s.add_argument("--repo", help="owner/name（省略時は git origin から推定）")
        s.add_argument("--issue", type=int, required=True)
        s.add_argument("--agent", choices=[k for k in AGENTS if k != "mixed"], default=agent_default, required=name == "start" and not agent_default)
        s.add_argument("--dry-run", action="store_true", help="GitHub を読むだけで変更しない")
        s.add_argument("--queue-only", action="store_true", help="キューに保存するだけで送信しない")
        s.add_argument("--next", help="次に取りかかれる具体的な一文")
        if name == "note":
            s.add_argument("--summary", required=True)
        if name in ("review", "complete"):
            s.add_argument("--summary")
            s.add_argument("--evidence", required=name == "review", help="テスト結果・PR・コミットなどの証跡")
        if name == "complete":
            s.add_argument("--verified", action="store_true", help="要求された検証に成功した")
            s.add_argument("--close", action="store_true", help="Done にしたら Issue を close する")
        if name in ("start", "block"):
            s.add_argument("--reason", required=name == "block", help="Blocker や再開の理由")
        s.set_defaults(handler=cmd_event)

    s = sub.add_parser("sync", parents=[common], help="キューを再送")
    s.add_argument("--force", action="store_true", help="バックオフ待ちも今すぐ送る")
    s.add_argument("--dry-run", action="store_true", help="未送信イベントを表示するだけ")
    s.add_argument("--retry-failed", action="store_true", help="失敗扱いのイベント（queue/failed）もキューに戻して再送")

    s = sub.add_parser("reconcile", parents=[common], help="Issue/Projects/キューの矛盾を報告（既定は dry-run）")
    s.add_argument("--repo", action="append", help="対象リポジトリ（省略時は allowlist 全体）")
    s.add_argument("--stale-days", type=int, default=14, help="In Progress を停滞とみなす日数")
    s.add_argument("--apply", action="store_true", help="安全な修正（ai-managed Issue の Projects 追加、Repo Name 列の補完）だけ実行")
    s.add_argument("--dry-run", action="store_true", help="既定の動作（明示用）")

    s = sub.add_parser("onboard", parents=[common], help="リポジトリに AI 共通ファイルと Hook を導入（既定は dry-run）")
    s.add_argument("--repo", required=True, help="owner/name")
    s.add_argument("--path", help="ローカルのクローン（省略時はカレントディレクトリ）")
    s.add_argument("--apply", action="store_true", help="表示した変更を実行する")
    s.add_argument("--diff", action="store_true", help="dry-run で差分を表示")
    s.add_argument("--update-workflow", action="store_true", help="既存の .ai/workflow.md をテンプレートで更新（バックアップあり）")
    s.add_argument("--dry-run", action="store_true", help="既定の動作（明示用）")

    s = sub.add_parser("create", parents=[common], help="Issue を作って Projects に追加（AI が自分で使う）")
    s.add_argument("--repo", help="owner/name または name（省略時は git origin から推定）")
    s.add_argument("--title", required=True)
    s.add_argument("--summary", required=True, help="何をするか")
    s.add_argument("--criteria", action="append", help="完了条件（複数可）")
    s.add_argument("--verification", help="どう確かめるか")
    s.add_argument("--next", help="次に取りかかれる具体的な一文")
    s.add_argument("--priority", choices=["P0", "P1", "P2", "P3"], default="P2")
    s.add_argument("--category", choices=FIELD_SPECS["category"][2], default="Other")
    s.add_argument("--agent", choices=[k for k in AGENTS if k != "mixed"], default=agent_default)
    s.add_argument("--start", action="store_true", help="作成後にすぐ start する")
    s.add_argument("--dry-run", action="store_true", help="作る内容を表示するだけ")

    s = sub.add_parser("decision-template", parents=[common], help="ChatGPT に渡す決定記録の書式を表示")
    s.add_argument("--repo", help="プロジェクト名（見出しに入る）")

    s = sub.add_parser("import-decisions", parents=[common], help="ChatGPT の決定記録を取り込む（既定は dry-run）")
    s.add_argument("--repo", required=True, help="owner/name または name（owner は設定から補う）")
    s.add_argument("--file", required=True, help="決定記録の Markdown ファイル")
    s.add_argument("--path", help="ローカルのクローン（省略時はカレントディレクトリ）")
    s.add_argument("--apply", action="store_true", help="反映する（auto モードでは既定で反映）")
    s.add_argument("--dry-run", action="store_true", help="反映せず内容を表示するだけ")

    s = sub.add_parser("install-hooks", parents=[common], help="Codex の Hook をこの PC のユーザー設定に導入（既定は dry-run）")
    s.add_argument("--apply", action="store_true", help="表示した変更を実行する")
    s.add_argument("--dry-run", action="store_true", help="既定の動作（明示用）")

    s = sub.add_parser("hook", parents=[common], help="Claude Code / Codex の Hook から呼ぶ（常に終了コード 0）")
    s.add_argument("event", choices=["session-start", "stop", "session-end"])
    s.add_argument("--agent", choices=["claude", "codex"])

    for name, handler in [
        ("doctor", cmd_doctor),
        ("init", cmd_init),
        ("status", cmd_status),
        ("sync", cmd_sync),
        ("reconcile", cmd_reconcile),
        ("onboard", cmd_onboard),
        ("install-hooks", cmd_install_hooks),
        ("create", cmd_create),
        ("decision-template", cmd_decision_template),
        ("import-decisions", cmd_import_decisions),
        ("hook", cmd_hook),
    ]:
        sub.choices[name].set_defaults(handler=handler)
    return p


def main(argv: list[str] | None = None) -> int:
    # Pipes (hooks, scripts) get UTF-8 regardless of the ANSI code page (cp932 on Japanese Windows).
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty() or os.environ.get("PYTHONIOENCODING"):
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    is_hook = args.command == "hook"  # a hook must never fail the agent session
    try:
        return int(args.handler(Ctx(args)))
    except AutoPulseError as exc:
        if is_hook:
            return ExitCode.OK
        print(f"autopulse: {redact(str(exc))}", file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        return ExitCode.OK if is_hook else ExitCode.ERROR
    except Exception as exc:  # never let a crash leak a traceback with secrets
        if is_hook:
            return ExitCode.OK
        print(f"autopulse: 予期しないエラー: {redact(repr(exc))}", file=sys.stderr)
        return ExitCode.ERROR
