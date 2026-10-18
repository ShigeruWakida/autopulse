"""Configuration: paths, field-name mapping, repo allowlist."""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from auto_pulse.errors import ConfigError

STATUSES = ["Backlog", "Ready", "In Progress", "Blocked", "Review", "Done"]
STATUS_COLORS = {
    "Backlog": "GRAY",
    "Ready": "BLUE",
    "In Progress": "YELLOW",
    "Blocked": "RED",
    "Review": "PURPLE",
    "Done": "GREEN",
}
AGENTS = {"codex": "Codex", "claude": "Claude", "chatgpt": "ChatGPT", "mixed": "Mixed"}

# logical key -> (default field name, data type, single-select options)
FIELD_SPECS: dict[str, tuple[str, str, list[str]]] = {
    "status": ("Status", "SINGLE_SELECT", STATUSES),
    "priority": ("Priority", "SINGLE_SELECT", ["P0", "P1", "P2", "P3"]),
    "category": ("Category", "SINGLE_SELECT", ["Spec", "Hardware", "PCB", "Firmware", "Software", "Test", "Docs", "Other"]),
    "last_activity": ("Last Activity", "DATE", []),
    "next_action": ("Next Action", "TEXT", []),
    "ai_agent": ("AI Agent", "SINGLE_SELECT", list(AGENTS.values())),
    "effort": ("Effort", "NUMBER", []),
    # The built-in Repository column always shows owner/name; this one holds the name only.
    "repo_name": ("Repo", "TEXT", []),
}
DEFAULT_PRIORITY = "P2"
DEFAULT_HUMAN_APPROVAL_PATTERNS = ["[human]", "[人間承認]", "人間承認"]


def home() -> Path:
    env = os.environ.get("AUTOPULSE_HOME")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "AutoPulse"
    return Path.home() / ".local" / "share" / "AutoPulse"


def config_path() -> Path:
    env = os.environ.get("AUTOPULSE_CONFIG")
    return Path(env) if env else home() / "config.toml"


@dataclass
class Config:
    owner: str
    project_number: int
    project_title: str = "Personal Development"
    fields: dict[str, str] = field(default_factory=lambda: {k: v[0] for k, v in FIELD_SPECS.items()})
    repos: list[str] = field(default_factory=list)
    human_approval_patterns: list[str] = field(default_factory=lambda: list(DEFAULT_HUMAN_APPROVAL_PATTERNS))
    path: Path | None = None

    def field(self, key: str) -> str:
        return self.fields[key]

    def allows(self, repo: str) -> bool:
        return repo.lower() in {r.lower() for r in self.repos}


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        raise ConfigError(f"設定ファイルがありません: {path}（autopulse init を実行してください）")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"設定ファイルを読めません: {path}: {exc}") from exc
    project = data.get("project", {})
    if "owner" not in project or "number" not in project:
        raise ConfigError(f"[project] owner / number がありません: {path}")
    fields = {k: v[0] for k, v in FIELD_SPECS.items()}
    unknown = set(data.get("fields", {})) - set(fields)
    if unknown:
        raise ConfigError(f"[fields] に不明なキーがあります: {', '.join(sorted(unknown))}")
    fields.update(data.get("fields", {}))
    policy = data.get("policy", {})
    return Config(
        owner=project["owner"],
        project_number=int(project["number"]),
        project_title=project.get("title", "Personal Development"),
        fields=fields,
        repos=list(data.get("repos", {}).get("allow", [])),
        human_approval_patterns=list(policy.get("human_approval_patterns", DEFAULT_HUMAN_APPROVAL_PATTERNS)),
        path=path,
    )


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(cfg: Config) -> str:
    lines = [
        "# AutoPulse configuration. Field names map to the actual GitHub Projects field names.",
        "[project]",
        f"owner = {_q(cfg.owner)}",
        f"number = {cfg.project_number}",
        f"title = {_q(cfg.project_title)}",
        "",
        "[fields]",
        *(f"{key} = {_q(name)}" for key, name in cfg.fields.items()),
        "",
        "[repos]",
        "# Only these repositories are ever written to.",
        "allow = [" + ", ".join(_q(r) for r in cfg.repos) + "]",
        "",
        "[policy]",
        "# An Acceptance Criteria line containing one of these needs a human to mark Done.",
        "human_approval_patterns = [" + ", ".join(_q(p) for p in cfg.human_approval_patterns) + "]",
        "",
    ]
    return "\n".join(lines)


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or cfg.path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    os.replace(tmp, path)
    return path
