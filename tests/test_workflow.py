import unittest

from auto_pulse import body
from auto_pulse.errors import PolicyRefused
from auto_pulse.spool import Event
from auto_pulse.workflow import build_plan

from tests.fakes import AC_BODY, FakeClient, make_config


def plan_for(event_type, fields=None, issue_body=AC_BODY, posted=(), agent="claude", **data):
    fake = FakeClient()
    fake.add_issue(1, body=issue_body, fields=fields)
    snap = fake.get_issue("me/repo", 1, "me", 1)
    event = Event(type=event_type, repo="me/repo", issue=1, agent=agent, data=data, created_at="2026-10-18T10:00:00+09:00")
    return build_plan(event, snap, make_config(), set(posted))


def field_ops(plan):
    return {op.field: op.value for op in plan.ops if op.kind == "set_field"}


class TransitionTest(unittest.TestCase):
    def test_start_adds_to_project_and_sets_in_progress(self):
        plan = plan_for("start")
        self.assertEqual(plan.ops[0].kind, "add_item")
        ops = field_ops(plan)
        self.assertEqual(ops["Status"], "In Progress")
        self.assertEqual(ops["AI Agent"], "Claude")
        self.assertEqual(ops["Priority"], "P2")
        self.assertEqual(ops["Repo"], "repo", "repository name without the owner")
        self.assertNotIn("Last Activity", ops, "start alone must not count as work")

    def test_start_from_backlog_is_refused(self):
        with self.assertRaises(PolicyRefused):
            plan_for("start", fields={"Status": "Backlog"})

    def test_review_without_start_is_refused(self):
        with self.assertRaises(PolicyRefused):
            plan_for("review", evidence="tests")

    def test_reopen_from_done_needs_reason(self):
        with self.assertRaises(PolicyRefused):
            plan_for("start", fields={"Status": "Done"})
        plan = plan_for("start", fields={"Status": "Done"}, reason="回帰が見つかった")
        self.assertEqual(field_ops(plan)["Status"], "In Progress")

    def test_unknown_status_is_refused(self):
        with self.assertRaises(PolicyRefused):
            plan_for("start", fields={"Status": "Todo"})

    def test_existing_priority_and_effort_are_not_touched(self):
        ops = field_ops(plan_for("note", fields={"Status": "In Progress", "Priority": "P0", "Effort": 3}, summary="s"))
        self.assertNotIn("Priority", ops)
        self.assertNotIn("Effort", ops)

    def test_second_agent_becomes_mixed(self):
        ops = field_ops(plan_for("start", fields={"Status": "Ready", "AI Agent": "Codex"}, agent="claude"))
        self.assertEqual(ops["AI Agent"], "Mixed")

    def test_note_updates_last_activity(self):
        ops = field_ops(plan_for("note", fields={"Status": "In Progress", "Last Activity": "2026-10-01"}, summary="s"))
        self.assertEqual(ops["Last Activity"], "2026-10-18")


class DoneGateTest(unittest.TestCase):
    review = {"Status": "Review", "Priority": "P2"}

    def test_complete_without_verified_stays_in_review(self):
        plan = plan_for("complete", fields=self.review, evidence="pytest ok")
        self.assertEqual(plan.outcome, "downgraded")
        self.assertEqual(plan.status_after, "Review")
        self.assertNotIn("Status", field_ops(plan))

    def test_complete_without_evidence_stays_in_review(self):
        plan = plan_for("complete", fields=self.review, verified=True)
        self.assertEqual(plan.outcome, "downgraded")

    def test_unchecked_criteria_block_done(self):
        plan = plan_for("complete", fields=self.review, issue_body="## Acceptance Criteria\n- [ ] B\n", verified=True, evidence="e")
        self.assertEqual(plan.outcome, "downgraded")
        self.assertIn("未チェック", plan.notes[0])

    def test_missing_criteria_block_done(self):
        plan = plan_for("complete", fields=self.review, issue_body="no criteria", verified=True, evidence="e")
        self.assertEqual(plan.outcome, "downgraded")

    def test_human_approval_criterion_blocks_ai(self):
        text = "## Acceptance Criteria\n- [x] 動く\n- [x] [human] 実機で確認\n"
        plan = plan_for("complete", fields=self.review, issue_body=text, verified=True, evidence="e")
        self.assertEqual(plan.outcome, "downgraded")
        self.assertIn("人間の承認", plan.notes[0])

    def test_verified_complete_goes_done(self):
        plan = plan_for("complete", fields=self.review, verified=True, evidence="CI #12 green", close=True)
        self.assertEqual(plan.outcome, "applied")
        self.assertEqual(field_ops(plan)["Status"], "Done")
        self.assertIn("close", [op.kind for op in plan.ops])

    def test_downgraded_complete_keeps_review_evidence(self):
        issue_body = body.apply(AC_BODY, {"Status": "Review", "Evidence": "CI green"})
        plan = plan_for("complete", fields=self.review, issue_body=issue_body, evidence="目視のみ")
        edits = [op for op in plan.ops if op.kind == "edit_body"]
        self.assertFalse(edits and "目視のみ" in edits[0].body)

    def test_complete_from_in_progress_downgrades_to_review(self):
        plan = plan_for("complete", fields={"Status": "In Progress"}, evidence="e")
        self.assertEqual(field_ops(plan)["Status"], "Review")


class ConflictTest(unittest.TestCase):
    def test_human_changed_next_action_is_kept(self):
        issue_body = body.apply(AC_BODY, {"Status": "In Progress", "Next Action": "AIが書いた値"})
        plan = plan_for(
            "note", fields={"Status": "In Progress", "Next Action": "人が書いた値"}, issue_body=issue_body, summary="s", next="新しい値"
        )
        self.assertNotIn("Next Action", field_ops(plan))
        self.assertTrue(plan.conflicts)
        self.assertIn("人が書いた値", plan.ops[-1].body, "conflict is reported in the comment")

    def test_next_action_written_by_us_is_updated(self):
        issue_body = body.apply(AC_BODY, {"Status": "In Progress", "Next Action": "古い値"})
        plan = plan_for("note", fields={"Status": "In Progress", "Next Action": "古い値"}, issue_body=issue_body, summary="s", next="新しい値")
        self.assertEqual(field_ops(plan)["Next Action"], "新しい値")
        self.assertFalse(plan.conflicts)

    def test_hand_edited_managed_block_is_not_rewritten(self):
        issue_body = body.apply(AC_BODY, {"Status": "In Progress"}).replace("In Progress", "人が編集")
        plan = plan_for("note", fields={"Status": "In Progress"}, issue_body=issue_body, summary="s")
        self.assertNotIn("edit_body", [op.kind for op in plan.ops])
        self.assertTrue(plan.conflicts)

    def test_crlf_body_from_github_is_not_rewritten(self):
        issue_body = body.apply(AC_BODY, {"Status": "In Progress", "Agent": "Claude", "Blockers": "None"})
        issue_body = issue_body.replace("\n", "\r\n")
        plan = plan_for("start", fields={"Status": "In Progress", "AI Agent": "Claude", "Priority": "P2"}, issue_body=issue_body)
        self.assertNotIn("edit_body", [op.kind for op in plan.ops])

    def test_already_posted_event_is_noop(self):
        fake = FakeClient()
        fake.add_issue(1, fields={"Status": "Done"})
        event = Event(type="start", repo="me/repo", issue=1, agent="codex")
        plan = build_plan(event, fake.get_issue("me/repo", 1, "me", 1), make_config(), {event.id})
        self.assertEqual(plan.outcome, "noop")
        self.assertEqual(plan.ops, [])


if __name__ == "__main__":
    unittest.main()
