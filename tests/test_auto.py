"""Auto mode: the AI updates progress without human gates; gaps are only noted."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auto_pulse import cli, config
from auto_pulse.errors import ExitCode
from auto_pulse.spool import Event
from auto_pulse.workflow import build_plan

from tests.fakes import FakeClient, make_config


def auto_config(**kw):
    return make_config(mode="auto", **kw)


def plan(event_type, status, issue_body="no criteria", **data):
    fake = FakeClient()
    fake.add_issue(1, body=issue_body, fields={"Status": status} if status else None)
    event = Event(type=event_type, repo="me/repo", issue=1, agent="claude", data=data)
    return build_plan(event, fake.get_issue("me/repo", 1, "me", 1), auto_config(), set())


def status_op(p):
    return next((op.value for op in p.ops if op.kind == "set_field" and op.field == "Status"), None)


class AutoTransitionTest(unittest.TestCase):
    def test_start_straight_from_backlog(self):
        self.assertEqual(status_op(plan("start", "Backlog")), "In Progress")

    def test_complete_without_any_evidence_still_finishes(self):
        p = plan("complete", "In Progress", issue_body="## Acceptance Criteria\n- [ ] A\n- [ ] [human] B\n")
        self.assertEqual(p.outcome, "applied")
        self.assertEqual(status_op(p), "Done")
        self.assertIn("未確認の点", p.notes[0], "the gaps are written down instead of blocking")

    def test_reopen_without_reason_and_review_to_blocked(self):
        self.assertEqual(status_op(plan("start", "Done")), "In Progress")
        self.assertEqual(status_op(plan("block", "Review", reason="r")), "Blocked")

    def test_config_default_is_auto_and_mode_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = config.Config(owner="me", project_number=1)
            self.assertEqual(cfg.mode, "auto")
            cfg.mode = "strict"
            config.save(cfg, path)
            self.assertEqual(config.load(path).mode, "strict")


class CreateCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AUTOPULSE_HOME": self.tmp.name})
        self.env.start()
        os.environ.pop("AUTOPULSE_CONFIG", None)
        config.save(auto_config(), Path(self.tmp.name) / "config.toml")
        self.fake = FakeClient()
        self.patch = mock.patch.object(cli, "GhClient", lambda runner: self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_create_and_start(self):
        code, out = self.run_cli(
            "create", "--repo", "repo", "--title", "ログ出力を追加", "--summary", "処理時間を記録する",
            "--criteria", "ログが出る", "--priority", "P1", "--category", "Software", "--agent", "claude", "--start", "--json",
        )
        self.assertEqual(code, ExitCode.OK, out)
        data = json.loads(out)
        url = data["url"]
        self.assertTrue(data["created"])
        self.assertEqual(self.fake.items[url]["Status"], "In Progress")
        self.assertEqual(self.fake.items[url]["Priority"], "P1")
        self.assertEqual(self.fake.items[url]["Repo Name"], "repo")
        self.assertIn("- [ ] ログが出る", self.fake.issues[("me/repo", data["number"])]["body"])

    def test_same_title_is_reused(self):
        self.run_cli("create", "--repo", "me/repo", "--title", "同じ", "--summary", "s")
        code, out = self.run_cli("create", "--repo", "me/repo", "--title", "同じ", "--summary", "s", "--json")
        self.assertFalse(json.loads(out)["created"])
        self.assertEqual(len(self.fake.issues), 1)


if __name__ == "__main__":
    unittest.main()
