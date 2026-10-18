import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from auto_pulse import config
from auto_pulse.config import DEFAULT_STATUS_LABELS, Config
from auto_pulse.errors import ConfigError, PolicyRefused
from auto_pulse.spool import Event
from auto_pulse.workflow import build_plan

from tests.fakes import AC_BODY, FakeClient


def japanese_config() -> Config:
    return Config(owner="me", project_number=1, repos=["me/repo"])  # default labels


def plan(event_type, status, **data):
    fake = FakeClient()
    fake.add_issue(1, body=AC_BODY, fields={"Status": status} if status else None)
    event = Event(type=event_type, repo="me/repo", issue=1, agent="claude", data=data)
    return build_plan(event, fake.get_issue("me/repo", 1, "me", 1), japanese_config(), set())


class StatusLabelTest(unittest.TestCase):
    def test_default_labels_are_numbered_japanese(self):
        cfg = japanese_config()
        self.assertEqual(cfg.field_options("status"), ["1.未着手", "2.着手可", "3.作業中", "4.停止中", "5.確認待ち", "6.完了"])
        self.assertEqual(cfg.logical("5.確認待ち"), "Review")
        self.assertEqual(cfg.logical("Review"), "Review", "old English option names are still understood")

    def test_plan_writes_labels_and_reads_them_back(self):
        start = plan("start", None)
        self.assertIn(("Status", "3.作業中"), [(op.field, op.value) for op in start.ops if op.kind == "set_field"])
        self.assertIn("3.作業中", start.ops[-1].body)
        review = plan("review", "3.作業中", evidence="e")
        self.assertIn(("Status", "5.確認待ち"), [(op.field, op.value) for op in review.ops if op.kind == "set_field"])

    def test_rules_still_apply_to_labels(self):
        with self.assertRaises(PolicyRefused) as ctx:
            plan("start", "1.未着手")
        self.assertIn("2.着手可", str(ctx.exception))
        downgraded = plan("complete", "5.確認待ち", evidence="e")  # no --verified
        self.assertEqual(downgraded.outcome, "downgraded")
        self.assertIn("6.完了", downgraded.notes[0])

    def test_config_file_roundtrip_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            cfg = japanese_config()
            cfg.status_labels = dict(DEFAULT_STATUS_LABELS, Review="5.レビュー")
            config.save(cfg, path)
            self.assertEqual(config.load(path).label("Review"), "5.レビュー")
            path.write_text(path.read_text(encoding="utf-8").replace('"5.レビュー"', '"6.完了"'), encoding="utf-8")
            with self.assertRaises(ConfigError):
                config.load(path)


if __name__ == "__main__":
    unittest.main()
