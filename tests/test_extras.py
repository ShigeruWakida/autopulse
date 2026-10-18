import json
import os
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from auto_pulse import body, config, hooks, onboard, reconcile
from auto_pulse.errors import PolicyRefused
from auto_pulse.github import IssueSnapshot, parse_github_url
from auto_pulse.spool import Event, Spool

from tests.fakes import make_config


def item(number, status=None, state="OPEN", issue_body="", repo="me/repo", **fields):
    values = {"Status": status, **fields} if status else dict(fields)
    return {
        "id": f"PVTI_{number}", "type": "Issue", "repo": repo, "number": number, "title": f"t{number}",
        "url": f"https://github.com/{repo}/issues/{number}", "state": state, "body": issue_body, "fields": values,
    }


class BoardFake:
    def __init__(self, items, open_issues=()):
        self.items = items
        self.open = list(open_issues)
        self.added = []
        self.labels = set()
        self.created_labels = []

    def project_items(self, owner, number):
        return self.items

    def open_issues(self, repo, owner, number):
        return [s for s in self.open if s.repo.lower() == repo.lower()]

    def add_item(self, owner, number, url):
        self.added.append(url)

    def label_exists(self, repo, name):
        return name in self.labels

    def ensure_label(self, repo, name, color, description):
        self.created_labels.append(name)
        return True


def snap(number, labels=(), fields=None, title="t"):
    return IssueSnapshot(
        repo="me/repo", number=number, node_id="I", title=title, body="", url=f"https://github.com/me/repo/issues/{number}",
        state="OPEN", updated_at="", labels=list(labels), item_id="PVTI" if fields is not None else None, fields=fields or {},
    )


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self, findings):
        return sorted(f.kind for f in findings)

    def test_detects_inconsistencies(self):
        done_body = body.apply("x", {"Status": "Done"})
        items = [
            item(1, "Done", issue_body=done_body),  # open + no evidence
            item(2, "Review", state="CLOSED"),
            item(3, "In Progress", **{"Last Activity": "2026-09-01"}),
            item(4, "Blocked"),
            item(5, None),
            item(6, "In Progress", repo="stranger/repo"),
        ]
        fake = BoardFake(items, [snap(7, labels=["ai-managed"])])
        self.spool.put(Event(type="note", repo="me/repo", issue=3))
        findings = reconcile.run(fake, make_config(), self.spool, [], today=date(2026, 10, 18))
        self.assertEqual(
            self.kinds(findings),
            sorted([
                "done-but-open", "done-without-evidence", "closed-not-done", "stale", "blocked-without-reason",
                "no-status", "not-allowlisted", "missing-from-board", "pending-event",
            ]),
        )
        self.assertEqual(fake.added, [], "dry-run must not write")

    def test_apply_only_adds_missing_items(self):
        fake = BoardFake([item(1, "Done", state="OPEN")], [snap(7, labels=["ai-managed"])])
        findings = reconcile.run(fake, make_config(), self.spool, [], today=date(2026, 10, 18))
        fixed = reconcile.apply(fake, make_config(), findings)
        self.assertEqual([f.kind for f in fixed], ["missing-from-board"])
        self.assertEqual(fake.added, ["https://github.com/me/repo/issues/7"])

    def test_clean_board_has_no_findings(self):
        good = body.apply("x", {"Status": "In Progress"})
        fake = BoardFake([item(1, "In Progress", issue_body=good, **{"Last Activity": "2026-10-17"})])
        self.assertEqual(reconcile.run(fake, make_config(), self.spool, [], today=date(2026, 10, 18)), [])


class OnboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "clone"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(["git", "-C", str(self.root), "remote", "add", "origin", "https://github.com/me/repo.git"], check=True)
        self.env = mock.patch.dict(os.environ, {"AUTOPULSE_HOME": str(Path(self.tmp.name) / "home")})
        self.env.start()
        os.environ.pop("AUTOPULSE_CONFIG", None)
        self.cfg = make_config(repos=[])
        self.fake = BoardFake([])

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_existing_files_are_preserved_and_settings_merged(self):
        self.write("AGENTS.md", "# 既存のルール\n- 手書きの指示\n")
        self.write("docs/STATUS.md", "人が書いた状況\n")
        self.write(".claude/settings.json", json.dumps({"permissions": {"allow": ["Bash(npm test)"]}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        plan = onboard.build(self.fake, self.cfg, "me/repo", self.root)
        actions = {c.path: c.action for c in plan.files}
        self.assertEqual(actions["AGENTS.md"], "append")
        self.assertEqual(actions["CLAUDE.md"], "create")
        self.assertEqual(actions["docs/STATUS.md"], "skip")
        self.assertEqual(actions[".claude/settings.json"], "merge")
        self.assertTrue(plan.create_label and plan.add_to_allowlist)
        self.assertFalse((self.root / "CLAUDE.md").exists(), "build is a dry run")

        backup = onboard.apply(self.fake, self.cfg, plan)
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents.startswith("# 既存のルール\n- 手書きの指示\n"))
        self.assertIn(".ai/workflow.md", agents)
        self.assertEqual((self.root / "docs/STATUS.md").read_text(encoding="utf-8"), "人が書いた状況\n")
        settings = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["permissions"], {"allow": ["Bash(npm test)"]})
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop_commands, ["echo mine", "autopulse hook stop"])
        self.assertIn("me/repo", (self.root / ".ai/workflow.md").read_text(encoding="utf-8"))
        self.assertTrue((backup / "AGENTS.md").exists())
        self.assertEqual(self.fake.created_labels, ["ai-managed"])
        self.assertEqual(config.load().repos, ["me/repo"])

        again = onboard.build(self.fake, config.load(), "me/repo", self.root)
        self.assertEqual({c.action for c in again.files} - {"skip"}, {"unchanged"}, "second run changes nothing")

    def test_invalid_json_is_left_alone(self):
        self.write(".codex/hooks.json", "{ not json")
        plan = onboard.build(self.fake, self.cfg, "me/repo", self.root)
        self.assertEqual({c.path: c.action for c in plan.files}[".codex/hooks.json"], "skip")

    def test_origin_mismatch_is_refused(self):
        with self.assertRaises(PolicyRefused):
            onboard.build(self.fake, self.cfg, "me/other", self.root)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AUTOPULSE_HOME": self.tmp.name})
        self.env.start()
        os.environ.pop("AUTOPULSE_CONFIG", None)
        config.save(make_config(), Path(self.tmp.name) / "config.toml")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_session_start_lists_active_issues(self):
        fake = BoardFake([], [
            snap(3, fields={"Status": "Blocked"}, title="部品待ち"),
            snap(1, fields={"Status": "In Progress", "Next Action": "配線を確認"}, title="基板"),
            snap(2, fields={"Status": "Done"}),
        ])
        with mock.patch.object(hooks, "origin_of", return_value="me/repo"):
            text = hooks.run("session-start", "claude", json.dumps({"cwd": self.tmp.name}), fake)
        self.assertIn("#1 [In Progress] 基板", text)
        self.assertIn("次: 配線を確認", text)
        self.assertLess(text.index("#1"), text.index("#3"))
        self.assertNotIn("#2", text)
        self.assertIn("--agent claude", text)

    def test_unknown_repo_prints_nothing(self):
        with mock.patch.object(hooks, "origin_of", return_value="other/repo"):
            self.assertEqual(hooks.run("session-start", "codex", "", BoardFake([])), "")

    def test_stop_with_empty_queue_does_not_touch_github(self):
        class Exploding:
            def __getattr__(self, name):
                raise AssertionError("GitHub must not be called")

        self.assertEqual(hooks.run("stop", None, "{}", Exploding()), "")

    def test_errors_are_swallowed(self):
        class Broken:
            def open_issues(self, *a):
                raise RuntimeError("boom")

        with mock.patch.object(hooks, "origin_of", return_value="me/repo"):
            self.assertEqual(hooks.run("session-start", "claude", "not json", Broken()), "")


class UrlTest(unittest.TestCase):
    def test_parse_github_url(self):
        self.assertEqual(parse_github_url("https://github.com/me/repo.git\n"), "me/repo")
        self.assertEqual(parse_github_url("git@github.com:me/repo.git"), "me/repo")
        self.assertEqual(parse_github_url("https://github.com/me/repo"), "me/repo")
        self.assertIsNone(parse_github_url("https://gitlab.com/me/repo"))


if __name__ == "__main__":
    unittest.main()
