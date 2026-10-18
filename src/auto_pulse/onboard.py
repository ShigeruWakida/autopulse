"""`autopulse onboard`: add the AI workflow files and hooks to a repository clone.

Existing files are never destroyed: AGENTS.md / CLAUDE.md get an appended block between
markers, JSON settings are merged, other existing files are left alone. Anything that is
rewritten is first copied to %LOCALAPPDATA%/AutoPulse/backups/.
"""

import copy
import difflib
import json
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from auto_pulse import config as config_mod
from auto_pulse.config import Config
from auto_pulse.errors import AutoPulseError, PolicyRefused
from auto_pulse.github import GhClient, origin_of

TEMPLATES = Path(__file__).resolve().parents[2] / "templates"
MARK_START = "<!-- autopulse:onboard:start -->"
MARK_END = "<!-- autopulse:onboard:end -->"
HOOK_TAG = "autopulse hook"
AI_LABEL = ("ai-managed", "0E8A16", "AutoPulse が進捗を記録するタスク")

CLAUDE_HOOKS = {
    "SessionStart": [{"hooks": [{"type": "command", "command": "autopulse hook session-start --agent claude", "timeout": 30}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "autopulse hook stop", "timeout": 60}]}],
    "SessionEnd": [{"hooks": [{"type": "command", "command": "autopulse hook session-end", "timeout": 30}]}],
}
CODEX_HOOKS = {
    "SessionStart": [{"hooks": [{"type": "command", "command": "autopulse hook session-start --agent codex", "timeout": 30}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "autopulse hook stop", "timeout": 60}]}],
}


@dataclass
class FileChange:
    path: str  # relative to the repo root
    action: str  # create | append | update | merge | skip | unchanged
    new: str | None = None
    old: str | None = None
    reason: str = ""

    def diff(self) -> str:
        if self.new is None:
            return ""
        return "".join(difflib.unified_diff(
            (self.old or "").splitlines(True), self.new.splitlines(True), f"a/{self.path}", f"b/{self.path}"
        ))


@dataclass
class OnboardPlan:
    repo: str
    root: Path
    files: list[FileChange] = field(default_factory=list)
    create_label: bool = False
    add_to_allowlist: bool = False


def _template(name: str, cfg: Config, repo: str) -> str:
    # git autocrlf may check templates out with CRLF; compare and write with LF only.
    text = (TEMPLATES / name).read_bytes().decode("utf-8").replace("\r\n", "\n")
    return text.replace("{{REPO}}", repo).replace("{{PROJECT_TITLE}}", cfg.project_title)


def _read(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _snippet_change(root: Path, rel: str, snippet: str) -> FileChange:
    old = _read(root / rel)
    block = f"{MARK_START}\n{snippet.rstrip()}\n{MARK_END}\n"
    if old is None:
        return FileChange(rel, "create", block)
    start, end = old.find(MARK_START), old.find(MARK_END)
    if start >= 0 and end > start:
        new = old[:start] + block.rstrip("\n") + old[end + len(MARK_END):]
        return FileChange(rel, "unchanged" if new == old else "update", None if new == old else new, old)
    return FileChange(rel, "append", old.rstrip("\n") + "\n\n" + block, old, "既存の内容の後ろに追記")


def _owned_change(root: Path, rel: str, content: str, overwrite: bool) -> FileChange:
    old = _read(root / rel)
    if old is None:
        return FileChange(rel, "create", content)
    if old == content:
        return FileChange(rel, "unchanged")
    if overwrite:
        return FileChange(rel, "update", content, old)
    return FileChange(rel, "skip", None, old, "既存ファイルがあり内容が異なるため変更しません")


def merge_hooks(existing: dict, wanted: dict) -> dict:
    merged = copy.deepcopy(existing)
    hooks = merged.setdefault("hooks", {})
    for event, groups in wanted.items():
        current = hooks.setdefault(event, [])
        present = any(HOOK_TAG in h.get("command", "") for g in current for h in g.get("hooks", []))
        if not present:
            current.extend(copy.deepcopy(groups))
    return merged


def _json_change(root: Path, rel: str, wanted: dict) -> FileChange:
    old = _read(root / rel)
    if old is None:
        return FileChange(rel, "create", json.dumps({"hooks": wanted}, ensure_ascii=False, indent=2) + "\n")
    try:
        data = json.loads(old)
    except json.JSONDecodeError:
        return FileChange(rel, "skip", None, old, "JSON として読めないため変更しません")
    merged = merge_hooks(data, wanted)
    if merged == data:
        return FileChange(rel, "unchanged")
    return FileChange(rel, "merge", json.dumps(merged, ensure_ascii=False, indent=2) + "\n", old, "既存の設定を残して Hook を追加")


def _ignored(root: Path, rel: str) -> bool:
    try:
        result = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", rel], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def build(client: GhClient, cfg: Config, repo: str, root: Path, update_workflow: bool = False) -> OnboardPlan:
    if not (root / ".git").exists():
        raise AutoPulseError(f"{root} は git リポジトリのルートではありません")
    origin = origin_of(str(root))
    if not origin or origin.lower() != repo.lower():
        raise PolicyRefused(f"{root} の origin（{origin}）が {repo} と一致しません")
    plan = OnboardPlan(repo=repo, root=root)
    plan.files = [
        _snippet_change(root, "AGENTS.md", _template("AGENTS.snippet.md", cfg, repo)),
        _snippet_change(root, "CLAUDE.md", _template("CLAUDE.snippet.md", cfg, repo)),
        _owned_change(root, ".ai/workflow.md", _template("workflow.md", cfg, repo), update_workflow),
        _owned_change(root, ".github/ISSUE_TEMPLATE/ai-task.yml", _template("issue-template.yml", cfg, repo), False),
        _owned_change(root, "docs/DECISIONS.md", _template("DECISIONS.md", cfg, repo), False),
        _owned_change(root, "docs/STATUS.md", _template("STATUS.md", cfg, repo), False),
        _json_change(root, ".claude/settings.json", CLAUDE_HOOKS),
        _json_change(root, ".codex/hooks.json", CODEX_HOOKS),
    ]
    for change in plan.files:
        if change.action != "skip" and _ignored(root, change.path):
            change.reason = (change.reason + "; " if change.reason else "") + ".gitignore で除外されるためコミットされません（他の PC には届きません）"
    plan.create_label = not client.label_exists(repo, AI_LABEL[0])
    plan.add_to_allowlist = not cfg.allows(repo)
    return plan


def apply(client: GhClient, cfg: Config, plan: OnboardPlan) -> Path | None:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = config_mod.home() / "backups" / plan.repo.replace("/", "__") / stamp
    backed_up = False
    for change in plan.files:
        if change.action not in ("create", "append", "update", "merge"):
            continue
        target = plan.root / change.path
        content = change.new
        if target.exists():
            dest = backup_root / change.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            backed_up = True
            if b"\r\n" in target.read_bytes():  # keep the file's own line-ending style
                content = content.replace("\n", "\r\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    if plan.create_label:
        client.ensure_label(plan.repo, *AI_LABEL)
    if plan.add_to_allowlist:
        cfg.repos.append(plan.repo)
        config_mod.save(cfg)
    return backup_root if backed_up else None
