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

from tests.fakes import FakeClient, make_config, offline


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AUTOPULSE_HOME": self.tmp.name, "AUTOPULSE_CONFIG": ""}, clear=False)
        self.env.start()
        os.environ.pop("AUTOPULSE_CONFIG")
        os.environ.pop("AUTOPULSE_AGENT", None)
        config.save(make_config(), Path(self.tmp.name) / "config.toml")
        self.fake = FakeClient()
        self.url = self.fake.add_issue(1)
        self.patch = mock.patch.object(cli, "GhClient", lambda runner: self.fake)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_config_roundtrip(self):
        cfg = config.load(Path(self.tmp.name) / "config.toml")
        self.assertEqual((cfg.owner, cfg.project_number, cfg.repos), ("me", 1, ["me/repo"]))
        self.assertEqual(cfg.field("next_action"), "Next Action")

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli("start", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--dry-run", "--json")
        self.assertEqual(code, ExitCode.OK)
        data = json.loads(out)
        self.assertTrue(data["dry_run"])
        self.assertIn("Projects: Status = 'In Progress'", data["ops"])
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(list((Path(self.tmp.name) / "queue").glob("*.json")) if (Path(self.tmp.name) / "queue").exists() else [])

    def test_start_then_note(self):
        self.assertEqual(self.run_cli("start", "--repo", "me/repo", "--issue", "1", "--agent", "codex")[0], ExitCode.OK)
        code, out, _ = self.run_cli("note", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--summary", "配線完了", "--next", "導通確認")
        self.assertEqual(code, ExitCode.OK, out)
        self.assertEqual(self.fake.items[self.url]["Next Action"], "導通確認")
        self.assertEqual(self.fake.items[self.url]["AI Agent"], "Mixed")

    def test_unverified_complete_exits_refused(self):
        self.run_cli("start", "--repo", "me/repo", "--issue", "1", "--agent", "claude")
        self.run_cli("review", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--evidence", "unittest 53 ok")
        code, out, _ = self.run_cli("complete", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--evidence", "x")
        self.assertEqual(code, ExitCode.REFUSED)
        self.assertEqual(self.fake.items[self.url]["Status"], "Review")

    def test_offline_returns_queued_and_sync_recovers(self):
        self.run_cli("start", "--repo", "me/repo", "--issue", "1", "--agent", "claude")
        self.fake.fail_next = [offline()]
        code, _, _ = self.run_cli("note", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--summary", "s")
        self.assertEqual(code, ExitCode.QUEUED)
        self.assertEqual(self.run_cli("sync", "--force")[0], ExitCode.OK)
        self.assertEqual(self.run_cli("sync")[0], ExitCode.OK)
        self.assertEqual(len(self.fake.comments[("me/repo", 1)]), 2)

    def test_repo_outside_allowlist_is_refused_before_queueing(self):
        self.fake.add_issue(5, repo="other/repo")
        code, _, err = self.run_cli("start", "--repo", "other/repo", "--issue", "5", "--agent", "claude")
        self.assertEqual(code, ExitCode.REFUSED)
        self.assertIn("allowlist", err)
        self.assertEqual(self.fake.calls, [])

    def test_status_issue_detail_json(self):
        self.run_cli("start", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--next", "最初の一手")
        code, out, _ = self.run_cli("status", "--repo", "me/repo", "--issue", "1", "--json")
        self.assertEqual(code, ExitCode.OK)
        data = json.loads(out)
        self.assertEqual(data["status"], "In Progress")
        self.assertEqual(data["next_action"], "最初の一手")
        self.assertEqual(len(data["acceptance_criteria"]), 2)

    def test_missing_config_is_setup_error(self):
        (Path(self.tmp.name) / "config.toml").unlink()
        code, _, err = self.run_cli("note", "--repo", "me/repo", "--issue", "1", "--agent", "claude", "--summary", "s")
        self.assertEqual(code, ExitCode.SETUP)
        self.assertIn("autopulse init", err)

    def test_start_requires_agent(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["start", "--repo", "me/repo", "--issue", "1"])


if __name__ == "__main__":
    unittest.main()
