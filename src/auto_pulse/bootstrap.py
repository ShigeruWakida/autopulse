"""`autopulse init`: plan the Project/fields/config, and only write with --apply."""

import difflib
from dataclasses import dataclass, field

from auto_pulse import config as config_mod
from auto_pulse.config import FIELD_SPECS, STATUS_COLORS, STATUSES, Config
from auto_pulse.errors import ConfigError, PolicyRefused
from auto_pulse.github import GhClient, ProjectInfo

# A new Projects v2 board comes with this Status field.
DEFAULT_NEW_STATUS = ["Todo", "In Progress", "Done"]


@dataclass
class InitPlan:
    owner: str
    title: str
    number: int | None
    create_project: bool = False
    create_fields: list[str] = field(default_factory=list)  # logical keys
    replace_status_options: list[str] | None = None  # current options to be replaced
    conflicts: list[str] = field(default_factory=list)
    config_diff: str = ""
    config: Config | None = None


def _field_diff(plan: InitPlan, cfg: Config, info: ProjectInfo | None) -> None:
    fields = info.fields if info else {"Status": None}
    for key, (_, data_type, _) in FIELD_SPECS.items():
        options = cfg.field_options(key)
        name = cfg.field(key)
        if name not in fields:
            plan.create_fields.append(key)
            continue
        found = fields[name]
        if found is None:  # Status of a project that does not exist yet
            plan.replace_status_options = DEFAULT_NEW_STATUS
            continue
        if found.data_type != data_type:
            plan.conflicts.append(f"{name!r} は {found.data_type} 型です（期待値 {data_type}）。自動変更しません")
        elif options and found.options != options:
            if key == "status" and not set(options) <= set(found.options):
                plan.replace_status_options = found.options
            elif not set(options) <= set(found.options):
                plan.conflicts.append(f"{name!r} の選択肢 {found.options} が仕様 {options} と異なります。自動変更しません")


def build(client: GhClient, owner: str, title: str, number: int | None, repos: list[str]) -> InitPlan:
    info = None
    if number is None:
        for project in client.list_projects(owner):
            if project.get("title") == title and not project.get("closed"):
                number = int(project["number"])
                break
    if number is not None:
        info = client.get_project(owner, number)
        if info is None:
            raise ConfigError(f"{owner} の Project #{number} が見つかりません")
        title = info.title
    plan = InitPlan(owner=owner, title=title, number=number, create_project=info is None)

    try:
        existing = config_mod.load()
    except ConfigError:
        existing = None
    cfg = Config(owner=owner, project_number=number or 0, project_title=title)
    if existing:
        cfg.fields = existing.fields
        cfg.human_approval_patterns = existing.human_approval_patterns
        cfg.repos = list(existing.repos)
    for repo in repos:
        if not cfg.allows(repo):
            cfg.repos.append(repo)
    plan.config = cfg
    _field_diff(plan, cfg, info)

    path = config_mod.config_path()
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = config_mod.dumps(cfg)
    if number is None:
        new = new.replace("number = 0", "number = <作成後に決定>")
    plan.config_diff = "".join(
        difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(path) + " (現在)", str(path) + " (変更後)")
    )
    return plan


def describe(plan: InitPlan) -> list[str]:
    lines = []
    if plan.create_project:
        lines.append(f"[作成] User Project {plan.title!r}（owner {plan.owner}、非公開）")
    else:
        lines.append(f"[既存] Project #{plan.number} {plan.title!r}")
    for key in plan.create_fields:
        name, data_type, _ = FIELD_SPECS[key]
        options = plan.config.field_options(key)
        lines.append(f"[作成] フィールド {name!r} {data_type}" + (f" {options}" if options else ""))
    if plan.replace_status_options is not None:
        lines.append(
            f"[置換] Status の選択肢 {plan.replace_status_options} → {plan.config.field_options('status')}"
            "（--update-status-options が必要。項目が 0 件のときだけ実行）"
        )
    for conflict in plan.conflicts:
        lines.append(f"[衝突] {conflict}")
    lines.append("[設定] " + ("変更なし" if not plan.config_diff else "設定ファイルを書き込み"))
    return lines


def apply(client: GhClient, plan: InitPlan, update_status_options: bool) -> Config:
    if plan.conflicts:
        raise PolicyRefused("衝突があるため初期化しません。Projects 側を確認してください")
    if plan.replace_status_options is not None and not update_status_options:
        raise PolicyRefused("Status の選択肢の置換が必要です。内容を確認して --update-status-options を付けてください")
    cfg = plan.config
    number = plan.number
    if plan.create_project:
        number = client.create_project(plan.owner, plan.title)
    cfg.project_number = number
    info = client.get_project(plan.owner, number)
    status = info.fields.get(cfg.field("status"))
    wanted = cfg.field_options("status")
    if status and not set(wanted) <= set(status.options):
        if info.item_count:
            raise PolicyRefused(f"Project に {info.item_count} 件の項目があるため Status の選択肢は置換しません")
        client.replace_single_select_options(status.id, [(cfg.label(s), STATUS_COLORS[s]) for s in STATUSES])
    for key, (_, data_type, _) in FIELD_SPECS.items():
        name = cfg.field(key)
        if name not in info.fields:
            client.create_field(plan.owner, number, name, data_type, cfg.field_options(key) or None)
    config_mod.save(cfg)
    return cfg
