import tempfile
import unittest
from pathlib import Path

from auto_pulse import body, records
from auto_pulse.engine import Engine
from auto_pulse.errors import AutoPulseError
from auto_pulse.spool import Spool

from tests.fakes import FakeClient, make_config

RECORD = """# 決定記録 repo 2026-10-19

## 決定
### アプリの実装言語は Flutter
- Issue: #1
- 内容: Flutter / Dart を正式採用する
- 理由: Android と iPhone を1つのコードで作れるため
- 日付: 2026-10-19

### シールは大型優先
- Issue: なし
- 内容: 貼れる限り大型を使う

## 新しくやること
### Flutter のカメラ試作
- 優先度: P1
- 分類: software
- 概要: Flutter でカメラプレビューを出す
- 完了条件:
  - [ ] プレビューが出る
  - [ ] [human] 実機で確認した
- 検証: 実機で確認
- 次の一手: camera パッケージを入れる

### 概要のない作業
- 優先度: P2

## 未決
### PC との接続方式
- 状態: mDNS と IP 直指定で比較中
"""


class ParseTest(unittest.TestCase):
    def test_parses_all_sections(self):
        r = records.parse(RECORD)
        self.assertEqual(r.date, "2026-10-19")
        self.assertEqual([(d.title, d.issue) for d in r.decisions], [("アプリの実装言語は Flutter", 1), ("シールは大型優先", None)])
        self.assertEqual(r.decisions[1].date, "2026-10-19", "falls back to the record date")
        task = r.tasks[0]
        self.assertEqual((task.priority, task.category), ("P1", "Software"))
        self.assertEqual(task.criteria, ["プレビューが出る", "[human] 実機で確認した"])
        self.assertEqual(r.open_items[0].state, "mDNS と IP 直指定で比較中")
        self.assertTrue(any("概要のない作業" in w for w in r.warnings), "incomplete task is reported, not imported")
        self.assertEqual(len(r.tasks), 1)

    def test_crlf_and_empty_records(self):
        self.assertEqual(len(records.parse(RECORD.replace("\n", "\r\n")).decisions), 2)
        with self.assertRaises(AutoPulseError):
            records.parse("# 決定記録\n\n## 決定\n- なし\n")

    def test_template_has_the_parsed_headings(self):
        text = records.template("ScanPilot")
        self.assertIn("決定記録 ScanPilot", text)
        for heading in ("## 決定", "## 新しくやること", "## 未決", "- 完了条件:"):
            self.assertIn(heading, text)


class ImportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "clone"
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "DECISIONS.md").write_text("# 設計判断の記録\n\n人が書いた判断\n", encoding="utf-8")
        self.fake = FakeClient()
        self.fake.add_issue(1, fields={"Status": "Ready"})
        self.cfg = make_config()
        self.spool = Spool(Path(self.tmp.name) / "queue")

    def tearDown(self):
        self.tmp.cleanup()

    def run_import(self):
        plan = records.build(self.fake, self.cfg, "me/repo", self.root, records.parse(RECORD))
        created = records.apply(self.fake, self.cfg, plan, self.spool, lambda: Engine(self.fake, self.cfg))
        return plan, created

    def test_import_and_reimport(self):
        plan, created = self.run_import()
        self.assertEqual(len(plan.new_decision_entries), 2)
        decisions = (self.root / "docs" / "DECISIONS.md").read_text(encoding="utf-8")
        self.assertTrue(decisions.startswith("# 設計判断の記録\n\n人が書いた判断\n"), "human text kept")
        self.assertIn("## 2026-10-19 アプリの実装言語は Flutter", decisions)
        self.assertIn("- 関連: #1", decisions)
        self.assertTrue((self.root / plan.record_path).exists())

        comments = self.fake.comments[("me/repo", 1)]
        self.assertEqual(len(comments), 1)
        self.assertIn("ChatGPT で決定: Flutter / Dart を正式採用する", comments[0])
        url1 = self.fake.issues[("me/repo", 1)]["url"]
        self.assertEqual(self.fake.items[url1]["Status"], "Ready", "status is left to a person")
        self.assertEqual(self.fake.items[url1]["AI Agent"], "ChatGPT")

        self.assertEqual(len(created), 1)
        new = self.fake.issues[("me/repo", 2)]
        self.assertEqual(new["title"], "Flutter のカメラ試作")
        self.assertEqual(len(body.acceptance_criteria(new["body"])), 2)
        self.assertEqual(self.fake.items[new["url"]], {"Status": "Backlog", "Priority": "P1", "Category": "Software", "Repo Name": "repo"})

        plan2, created2 = self.run_import()  # the same record again
        self.assertTrue(plan2.already_imported)
        self.assertEqual(plan2.new_decision_entries, [])
        self.assertEqual(plan2.new_tasks, [])
        self.assertEqual(created2, [])
        self.assertEqual(len(self.fake.comments[("me/repo", 1)]), 1, "no duplicate comment")
        self.assertEqual((self.root / "docs" / "DECISIONS.md").read_text(encoding="utf-8"), decisions)

    def test_unknown_issue_is_a_warning_not_a_failure(self):
        text = RECORD.replace("- Issue: #1", "- Issue: #99")
        plan = records.build(self.fake, self.cfg, "me/repo", self.root, records.parse(text))
        self.assertEqual(plan.notes, [])
        self.assertTrue(any("#99" in w for w in plan.warnings))


if __name__ == "__main__":
    unittest.main()
