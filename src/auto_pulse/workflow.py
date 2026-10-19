"""Pure planning: given an event and the current GitHub state, decide what to change.

Nothing here talks to GitHub, so every rule (state transitions, Done gating,
conflict handling) is unit-testable without mocks.
"""

import hashlib
from dataclasses import dataclass, field
from typing import Any

from auto_pulse import body as managed_body
from auto_pulse.config import AGENTS, DEFAULT_PRIORITY, STATUSES, Config
from auto_pulse.errors import PolicyRefused
from auto_pulse.github import IssueSnapshot
from auto_pulse.spool import Event

TARGET = {"start": "In Progress", "note": None, "block": "Blocked", "review": "Review", "complete": "Done"}

# target -> statuses it may be entered from (None = not yet on the board)
ALLOWED_FROM: dict[str, set[str | None]] = {
    "In Progress": {None, "Ready", "In Progress", "Blocked", "Review", "Done"},
    "Blocked": {"In Progress", "Blocked", "Done"},
    "Review": {"In Progress", "Review"},
    "Done": {"Review", "In Progress"},  # from In Progress it passes through Review in one step
}
REASON_REQUIRED = {("Done", "In Progress"), ("Done", "Blocked")}


@dataclass
class Op:
    kind: str  # add_item | set_field | edit_body | close | comment
    field: str | None = None
    value: Any = None
    body: str | None = None
    base_body: str | None = None

    def describe(self) -> str:
        if self.kind == "set_field":
            return f"Projects: {self.field} = {self.value!r}"
        if self.kind == "add_item":
            return "Projects: Issue を追加"
        if self.kind == "edit_body":
            return "Issue 本文: AutoPulse 管理欄を更新"
        if self.kind == "close":
            return "Issue: close (completed)"
        return "Issue コメント: 記録を追加"


@dataclass
class Plan:
    event: Event
    ops: list[Op] = field(default_factory=list)
    outcome: str = "applied"  # applied | noop | downgraded
    status_before: str | None = None
    status_after: str | None = None
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)


def check_transition(current: str | None, target: str, event: Event, cfg: Config | None = None) -> None:
    if cfg is not None and cfg.mode == "auto":
        return  # the AI may move an issue anywhere; mistakes are cheaper than friction
    name = cfg.label if cfg else (lambda s: s)
    if current is not None and current not in STATUSES:
        raise PolicyRefused(f"現在の Status {current!r} は AutoPulse の状態にありません（Projects の選択肢を確認してください）")
    if current == "Backlog" and target == "In Progress":
        raise PolicyRefused(
            f"{name('Backlog')} の Issue は要件と完了条件を確認して {name('Ready')} にしてから start してください"
        )
    if current not in ALLOWED_FROM[target]:
        shown = name(current) if current else "（Projects 未登録）"
        raise PolicyRefused(f"{shown} → {name(target)} には遷移できません（先に start してください）")
    if (current, target) in REASON_REQUIRED and not event.data.get("reason"):
        raise PolicyRefused(f"{name('Done')} → {name(target)} に戻すには --reason で理由を記録してください")


def completion_blockers(event: Event, snap: IssueSnapshot, cfg: Config) -> list[str]:
    reasons = []
    if not event.data.get("verified"):
        reasons.append("--verified が指定されていません")
    if not (event.data.get("evidence") or "").strip():
        reasons.append("検証の証跡（--evidence）がありません")
    criteria = managed_body.acceptance_criteria(snap.body)
    if criteria is None:
        reasons.append("Issue 本文に Acceptance Criteria 欄がありません")
    elif not criteria:
        reasons.append("Acceptance Criteria にチェック項目がありません")
    else:
        patterns = [p.lower() for p in cfg.human_approval_patterns]
        human = [c.text for c in criteria if any(p in c.text.lower() for p in patterns)]
        if human:
            reasons.append("人間の承認が必要な完了条件があります: " + " / ".join(human))
        unchecked = [c.text for c in criteria if not c.checked]
        if unchecked:
            reasons.append("未チェックの完了条件: " + " / ".join(unchecked))
    return reasons


def build_plan(
    event: Event,
    snap: IssueSnapshot,
    cfg: Config,
    posted_event_ids: set[str],
    *,
    drop_body_edit: bool = False,
) -> Plan:
    F = cfg.field
    fields = snap.fields
    in_project = snap.item_id is not None
    current = cfg.logical(fields.get(F("status"))) if in_project else None
    plan = Plan(event=event, status_before=current, status_after=current)

    if event.id in posted_event_ids:
        # The record comment is the last write of an event, so it was fully applied before.
        plan.outcome = "noop"
        plan.notes.append("このイベントは反映済みです（重複送信を抑止）")
        return plan

    target = TARGET[event.type]
    if event.type == "complete":
        reasons = completion_blockers(event, snap, cfg)
        if reasons and cfg.mode == "auto":
            plan.notes.append("未確認の点（自動モードのため完了にしました）: " + "; ".join(reasons))
        elif reasons:
            target = "Review"
            plan.outcome = "downgraded"
            plan.notes.append(f"{cfg.label('Done')} にせず {cfg.label('Review')} に留めました: " + "; ".join(reasons))
    if target:
        check_transition(current, target, event, cfg)
        if target == "Done" and current == "In Progress":
            plan.notes.append(f"{cfg.label('In Progress')} → {cfg.label('Review')} → {cfg.label('Done')} を一度に記録しました")
        plan.status_after = target

    ops = plan.ops
    if not in_project:
        ops.append(Op("add_item"))

    if target and current != target:
        ops.append(Op("set_field", F("status"), cfg.label(target)))

    agent_label = fields.get(F("ai_agent"))
    if event.agent:
        label = AGENTS[event.agent]
        new_label = label if not agent_label or agent_label == label else AGENTS["mixed"]
        if new_label != agent_label:
            ops.append(Op("set_field", F("ai_agent"), new_label))
        agent_label = new_label

    if not fields.get(F("priority")):
        ops.append(Op("set_field", F("priority"), DEFAULT_PRIORITY))

    repo_name = event.repo.split("/")[-1]
    if fields.get(F("repo_name")) != repo_name:
        ops.append(Op("set_field", F("repo_name"), repo_name))

    activity = fields.get(F("last_activity"))
    if event.type != "start":  # starting a session alone is not verified work
        if not activity or activity < event.local_date:
            ops.append(Op("set_field", F("last_activity"), event.local_date))
            activity = event.local_date

    managed = managed_body.parse(snap.body)
    meta = dict(managed.meta)
    next_action = (event.data.get("next") or "").strip()
    written_next = None

    # A Next Action a human typed on the board is held until the Status changes. The hold
    # remembers the Status it was first seen in and a digest of the value, so a fresh human
    # edit starts a new hold.
    on_board = fields.get(F("next_action")) or ""
    last_ours = managed.values.get("Next Action", "")
    hold = meta.get("hold_next")
    if on_board and on_board != last_ours:
        if not hold or hold.get("value") != _digest(on_board):
            hold = {"status": current, "value": _digest(on_board)}
    else:
        hold = None

    if next_action:
        if on_board == next_action:
            written_next = next_action
            hold = None
        elif hold and hold["status"] == plan.status_after:
            plan.conflicts.append(
                f"Projects の Next Action は人が設定した値のため、Status が {cfg.label(hold['status']) or '（未登録）'} "
                f"から変わるまで上書きしません（現行値: {on_board!r}、提案: {next_action!r}）"
            )
        else:
            if hold:
                plan.notes.append(f"人が設定した Next Action {on_board!r} は Status が変わったため更新しました")
            ops.append(Op("set_field", F("next_action"), next_action))
            written_next = next_action
            hold = None

    if hold:
        meta["hold_next"] = hold
    else:
        meta.pop("hold_next", None)

    if managed.present and not managed.intact:
        plan.conflicts.append("Issue 本文の AutoPulse 管理欄が手動で編集されているため、管理欄は更新しませんでした")
    elif drop_body_edit:
        plan.conflicts.append("Issue 本文の同時更新が続いたため、管理欄の更新を見送りました")
    else:
        values = dict(managed.values)
        values["Status"] = cfg.label(plan.status_after) or values.get("Status", "")
        if agent_label:
            values["Agent"] = agent_label
        if activity:
            values["Last Activity"] = activity
        if written_next:
            values["Next Action"] = written_next
        if event.type == "block":
            values["Blockers"] = event.data.get("reason", "")
        elif (target and current == "Blocked") or "Blockers" not in values:
            values["Blockers"] = "None"
        # A downgraded complete keeps the evidence recorded by review; its own goes to the comment only.
        if event.type in ("review", "complete") and event.data.get("evidence") and plan.outcome != "downgraded":
            values["Evidence"] = event.data["evidence"]
        new_body = managed_body.apply(snap.body, values, meta)
        # CRLF vs LF alone is no change; stray "\r\r\n" from older versions does get repaired.
        if new_body != snap.body.replace("\r\n", "\n"):
            ops.append(Op("edit_body", body=new_body, base_body=snap.body))

    if plan.status_after == "Done" and event.data.get("close") and snap.state == "OPEN":
        ops.append(Op("close"))

    ops.append(Op("comment", body=render_comment(event, plan, cfg)))
    return plan


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def render_comment(event: Event, plan: Plan, cfg: Config | None = None) -> str:
    name = cfg.label if cfg else (lambda s: s)
    agent = AGENTS.get(event.agent or "", "")
    head = f"**AutoPulse** `{event.type}`" + (f" · {agent}" if agent else "") + f" · {event.created_at[:16].replace('T', ' ')}"
    lines = [f"<!-- autopulse:event={event.id} -->", head, ""]
    if plan.status_before != plan.status_after:
        lines.append(f"- 状態: {name(plan.status_before) or '（未登録）'} → {name(plan.status_after)}")
    labels = [("summary", "要約"), ("next", "次の一手"), ("reason", "理由"), ("evidence", "証跡")]
    for key, label in labels:
        if event.data.get(key):
            lines.append(f"- {label}: {event.data[key]}")
    if event.type == "complete":
        lines.append(f"- 検証: {'済（--verified）' if event.data.get('verified') else '未'}")
    for note in plan.notes:
        lines.append(f"\n> {note}")
    for conflict in plan.conflicts:
        lines.append(f"\n> **競合:** {conflict}")
    return "\n".join(lines) + "\n"
