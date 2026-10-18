import tempfile
import unittest
from pathlib import Path

from auto_pulse import body
from auto_pulse.engine import Engine, process
from auto_pulse.errors import GhError
from auto_pulse.spool import Event, Spool

from tests.fakes import FakeClient, make_config, offline


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.tmp.name) / "queue")
        self.fake = FakeClient()
        self.url = self.fake.add_issue(1)
        self.engine = Engine(self.fake, make_config())

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, event, force=False):
        self.spool.put(event)
        return process(self.engine, self.spool, force=force)

    def test_start_twice_adds_item_once(self):
        self.send(Event(type="start", repo="me/repo", issue=1, agent="codex"))
        self.send(Event(type="start", repo="me/repo", issue=1, agent="codex"))
        self.assertEqual([c for c in self.fake.calls if c[0] == "add_item"], [("add_item", self.url)])
        self.assertEqual(self.fake.items[self.url]["Status"], "In Progress")

    def test_offline_event_is_kept_and_synced_without_duplicates(self):
        self.send(Event(type="start", repo="me/repo", issue=1, agent="claude"))
        self.fake.fail_next = [offline()]
        results = self.send(Event(type="note", repo="me/repo", issue=1, agent="claude", data={"summary": "s", "next": "n"}))
        self.assertEqual(results[-1].status, "queued")
        self.assertEqual(len(self.spool.pending()), 1)
        self.assertEqual(self.spool.pending()[0].attempts, 1)

        results = process(self.engine, self.spool, force=True)
        self.assertEqual(results[0].status, "applied")
        self.assertEqual(self.spool.pending(), [])
        results = process(self.engine, self.spool, force=True)
        self.assertEqual(results, [])
        self.assertEqual(len(self.fake.comments[("me/repo", 1)]), 2)  # start + note, no duplicate

    def test_crash_after_comment_is_not_reapplied(self):
        event = Event(type="start", repo="me/repo", issue=1, agent="claude")
        self.spool.put(event)
        self.engine.apply(event)  # applied, but the queue file was never removed (crash)
        results = process(self.engine, self.spool, force=True)
        self.assertEqual(results[0].status, "noop")
        self.assertEqual(len(self.fake.comments[("me/repo", 1)]), 1)

    def test_later_events_wait_behind_failed_earlier_event(self):
        self.send(Event(type="start", repo="me/repo", issue=1, agent="claude"))
        self.fake.fail_next = [offline()]
        self.spool.put(Event(type="note", repo="me/repo", issue=1, data={"summary": "1"}))
        self.spool.put(Event(type="review", repo="me/repo", issue=1, data={"evidence": "e"}))
        results = process(self.engine, self.spool)
        self.assertEqual([r.status for r in results], ["queued", "waiting"])
        self.assertEqual(self.fake.items[self.url]["Status"], "In Progress")

    def test_refused_event_moves_to_failed(self):
        results = self.send(Event(type="review", repo="me/repo", issue=1, data={"evidence": "e"}))
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(len(self.spool.failed_events()), 1)
        self.assertEqual(self.spool.pending(), [])

    def test_not_allowlisted_repo_is_never_written(self):
        self.fake.add_issue(2, repo="other/repo")
        results = self.send(Event(type="start", repo="other/repo", issue=2, agent="claude"))
        self.assertEqual(results[0].status, "failed")
        self.assertEqual(self.fake.calls, [])

    def test_concurrent_human_edit_of_body_is_preserved(self):
        self.send(Event(type="start", repo="me/repo", issue=1, agent="claude"))
        reads = {"n": 0}

        def human_edits_once(key):
            reads["n"] += 1
            if reads["n"] == 2:  # between planning and writing
                self.fake.issues[key]["body"] += "\n人間が同時に追記した行\n"

        self.fake.on_read = human_edits_once
        results = self.send(Event(type="note", repo="me/repo", issue=1, data={"summary": "s", "next": "n"}))
        self.assertEqual(results[0].status, "applied")
        final = self.fake.issues[("me/repo", 1)]["body"]
        self.assertIn("人間が同時に追記した行", final)
        self.assertEqual(body.parse(final).values["Next Action"], "n")

    def test_persistent_body_race_gives_up_and_reports(self):
        self.send(Event(type="start", repo="me/repo", issue=1, agent="claude"))
        counter = {"n": 0}

        def always_edit(key):
            counter["n"] += 1
            self.fake.issues[key]["body"] += f"\n編集{counter['n']}"

        self.fake.on_read = always_edit
        results = self.send(Event(type="note", repo="me/repo", issue=1, data={"summary": "s"}))
        self.assertEqual(results[0].status, "conflict")
        self.assertIn("競合", self.fake.comments[("me/repo", 1)][-1])

    def test_permanent_error_does_not_block_other_issues(self):
        self.fake.add_issue(2)
        self.fake.fail_next = [GhError("HTTP 422")]
        self.spool.put(Event(type="start", repo="me/repo", issue=1, agent="claude"))
        self.spool.put(Event(type="start", repo="me/repo", issue=2, agent="claude"))
        results = process(self.engine, self.spool)
        self.assertEqual([r.status for r in results], ["failed", "applied"])


class NextActionHoldTest(unittest.TestCase):
    """Option B: a human Next Action is held until the Status changes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.spool = Spool(Path(self.tmp.name) / "queue")
        self.fake = FakeClient()
        self.url = self.fake.add_issue(1)
        self.engine = Engine(self.fake, make_config())
        self.send("start", agent="claude", next="AI の一手")

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, kind, agent="claude", **data):
        self.spool.put(Event(type=kind, repo="me/repo", issue=1, agent=agent, data=data))
        return process(self.engine, self.spool)[-1]

    def human_sets(self, text):
        self.fake.items[self.url]["Next Action"] = text

    @property
    def board(self):
        return self.fake.items[self.url]["Next Action"]

    def test_held_while_status_unchanged_then_released(self):
        self.human_sets("人の指示")
        self.assertEqual(self.send("note", summary="s", next="AI-1").status, "conflict")
        self.assertEqual(self.send("note", summary="s", next="AI-2").status, "conflict")
        self.assertEqual(self.board, "人の指示")
        result = self.send("review", evidence="e", next="レビュー依頼")
        self.assertEqual(result.status, "applied")
        self.assertEqual(self.board, "レビュー依頼")
        self.assertNotIn("hold_next", body.parse(self.fake.issues[("me/repo", 1)]["body"]).meta)

    def test_hold_remembers_status_it_was_first_seen_in(self):
        self.human_sets("人の指示")
        self.send("note", summary="次を指定しない記録")  # sees the human value in In Progress
        self.send("review", evidence="e")  # Status changes, nothing to write
        self.assertEqual(self.board, "人の指示")
        self.assertEqual(self.send("note", summary="s", next="AI の次").status, "applied")
        self.assertEqual(self.board, "AI の次")

    def test_new_human_edit_after_release_is_held_again(self):
        self.human_sets("人の指示1")
        self.send("review", evidence="e", next="AI")
        self.assertEqual(self.board, "AI")
        self.human_sets("人の指示2")
        self.assertEqual(self.send("note", summary="s", next="AI-2").status, "conflict")
        self.assertEqual(self.board, "人の指示2")

    def test_human_clearing_the_field_hands_it_back(self):
        self.human_sets("人の指示")
        self.send("note", summary="s", next="AI-1")
        self.human_sets("")
        self.assertEqual(self.send("note", summary="s", next="AI-2").status, "applied")
        self.assertEqual(self.board, "AI-2")


class SpoolTest(unittest.TestCase):
    def test_secrets_are_masked_before_hitting_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(Path(tmp))
            path = spool.put(Event(type="note", repo="me/repo", issue=1, data={"summary": "token=ghp_abcdefghijklmnop1234 を設定"}))
            self.assertNotIn("ghp_abcdefghijklmnop1234", path.read_text(encoding="utf-8"))

    def test_backoff_grows(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(Path(tmp))
            event = Event(type="note", repo="me/repo", issue=1)
            spool.put(event)
            spool.retry_later(event, "x")
            first = event.next_attempt_at
            spool.retry_later(event, "x")
            self.assertGreater(event.next_attempt_at, first)

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            spool = Spool(Path(tmp))
            with spool.lock("me/repo", 1) as first:
                with spool.lock("me/repo", 1) as second:
                    self.assertTrue(first)
                    self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
