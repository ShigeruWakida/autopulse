"""`autopulse doctor`: read-only environment check with concrete fixes."""

import sys
from dataclasses import asdict, dataclass

from auto_pulse import config as config_mod
from auto_pulse.config import FIELD_SPECS
from auto_pulse.errors import AutoPulseError
from auto_pulse.github import GhClient
from auto_pulse.spool import Spool, default_spool

WRITE_PERMISSIONS = {"ADMIN", "MAINTAIN", "WRITE"}


@dataclass
class Check:
    name: str
    level: str  # ok | warn | fail | skip
    detail: str
    fix: str = ""


def run(client: GhClient, cfg_path=None) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    py = sys.version_info
    add(Check("python", "ok" if py >= (3, 12) else "fail", f"Python {py.major}.{py.minor}.{py.micro}", "Python 3.12 以降をインストール"))

    try:
        add(Check("gh", "ok", client.version()))
    except AutoPulseError as exc:
        add(Check("gh", "fail", str(exc), "winget install --id GitHub.cli（PATH 反映のためターミナルを開き直す）"))
        return checks

    auth = client.auth_status()
    if not auth["logged_in"]:
        add(Check("auth", "fail", "GitHub にログインしていません", "gh auth login --scopes project"))
        return checks
    missing = [s for s in ("repo", "project") if s not in auth["scopes"]]
    add(Check(
        "auth",
        "fail" if missing else "ok",
        f"{auth['account']} scopes={','.join(auth['scopes']) or '(不明)'}",
        f"gh auth refresh -s {','.join(missing)}" if missing else "",
    ))

    try:
        cfg = config_mod.load(cfg_path)
        add(Check("config", "ok", str(cfg.path)))
    except AutoPulseError as exc:
        add(Check("config", "fail", str(exc), 'autopulse init --owner <login> --title "Personal Development"'))
        cfg = None

    if cfg:
        try:
            info = client.get_project(cfg.owner, cfg.project_number)
        except AutoPulseError as exc:
            info = None
            add(Check("project", "fail", str(exc), "gh auth refresh -s project"))
        else:
            if info is None:
                add(Check("project", "fail", f"{cfg.owner} の Project #{cfg.project_number} が見つかりません", "config の [project] を確認"))
            else:
                add(Check("project", "ok", f"#{info.number} {info.title} ({info.item_count} items) {info.url}"))
        if info:
            for key, (_, data_type, _) in FIELD_SPECS.items():
                options = cfg.field_options(key)
                name = cfg.field(key)
                found = info.fields.get(name)
                if not found:
                    add(Check(f"field:{key}", "fail", f"{name!r} がありません", "autopulse init（差分を確認して --apply）"))
                elif found.data_type != data_type:
                    add(Check(f"field:{key}", "fail", f"{name!r} の型が {found.data_type}（期待値 {data_type}）", "Projects 側で手動確認"))
                elif options and not set(options) <= set(found.options):
                    lacking = [o for o in options if o not in found.options]
                    add(Check(f"field:{key}", "fail", f"{name!r} に選択肢 {lacking} がありません", "autopulse init で差分を確認"))
                else:
                    extra = [o for o in found.options if o not in options] if options else []
                    level = "warn" if extra else "ok"
                    add(Check(f"field:{key}", level, f"{name!r} {data_type}" + (f" 余分な選択肢 {extra}" if extra else "")))

        if not cfg.repos:
            add(Check("repos", "warn", "allowlist が空です。どのリポジトリにも書き込みません", "config の [repos] allow に owner/name を追加"))
        for repo in cfg.repos:
            try:
                info_repo = client.repo_view(repo)
            except AutoPulseError as exc:
                add(Check(f"repo:{repo}", "fail", str(exc)))
                continue
            problems = []
            if info_repo.get("viewerPermission") not in WRITE_PERMISSIONS:
                problems.append(f"権限 {info_repo.get('viewerPermission')}")
            if not info_repo.get("hasIssuesEnabled"):
                problems.append("Issues 無効")
            if info_repo.get("isArchived"):
                problems.append("アーカイブ済み")
            add(Check(f"repo:{repo}", "fail" if problems else "ok", ", ".join(problems) or info_repo.get("viewerPermission", "")))

    spool = default_spool()
    try:
        spool.root.mkdir(parents=True, exist_ok=True)
        probe = spool.root / ".probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        pending, failed = len(spool.pending()), len(spool.failed_events())
        level = "warn" if pending or failed else "ok"
        add(Check("queue", level, f"{spool.root} 未送信 {pending} / 失敗 {failed}", "autopulse sync" if pending else ""))
    except OSError as exc:
        add(Check("queue", "fail", f"{spool.root} に書き込めません: {exc}"))
    return checks


def to_json(checks: list[Check]) -> list[dict]:
    return [asdict(c) for c in checks]
