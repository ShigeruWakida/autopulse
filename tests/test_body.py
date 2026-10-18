import unittest

from auto_pulse import body


class ManagedBlockTest(unittest.TestCase):
    def test_append_then_parse_roundtrip(self):
        text = body.apply("人間の本文", {"Status": "In Progress", "Next Action": "配線図を確認"})
        self.assertTrue(text.startswith("人間の本文\n\n<!-- autopulse:start -->"))
        managed = body.parse(text)
        self.assertTrue(managed.present and managed.intact)
        self.assertEqual(managed.values["Next Action"], "配線図を確認")

    def test_replace_keeps_text_outside_markers(self):
        text = body.apply("before", {"Status": "In Progress"}) + "\nafter (人間が後から追記)\n"
        updated = body.apply(text, {"Status": "Review"})
        self.assertIn("before", updated)
        self.assertIn("after (人間が後から追記)", updated)
        self.assertEqual(body.parse(updated).values["Status"], "Review")
        self.assertEqual(updated.count(body.START), 1)

    def test_human_edit_inside_block_is_detected(self):
        text = body.apply("x", {"Status": "In Progress", "Next Action": "A"})
        edited = text.replace("- **Next Action:** A", "- **Next Action:** 人が書き換えた")
        self.assertFalse(body.parse(edited).intact)

    def test_crlf_from_web_editor_is_still_intact(self):
        text = body.apply("x", {"Status": "In Progress"}).replace("\n", "\r\n")
        self.assertTrue(body.parse(text).intact)

    def test_broken_markers_are_not_intact(self):
        self.assertFalse(body.parse("x\n" + body.START + "\n- **Status:** Done\n").intact)

    def test_multiline_values_are_flattened(self):
        text = body.apply("", {"Status": "Blocked", "Blockers": "部品待ち\n納期未定"})
        self.assertEqual(body.parse(text).values["Blockers"], "部品待ち 納期未定")


class AcceptanceCriteriaTest(unittest.TestCase):
    def test_parses_checkboxes_under_heading_only(self):
        text = "- [ ] 関係ない\n## Acceptance Criteria\n- [x] A\n- [ ] B\n## Notes\n- [ ] C\n"
        items = body.acceptance_criteria(text)
        self.assertEqual([(c.checked, c.text) for c in items], [(True, "A"), (False, "B")])

    def test_japanese_heading(self):
        self.assertEqual(len(body.acceptance_criteria("### 完了条件\n- [X] 動く\n")), 1)

    def test_missing_section_is_none(self):
        self.assertIsNone(body.acceptance_criteria("### Summary\n- [x] A\n"))

    def test_managed_block_is_ignored(self):
        text = body.apply("## Acceptance Criteria\n- [x] A\n", {"Status": "Review"})
        self.assertEqual(len(body.acceptance_criteria(text)), 1)


if __name__ == "__main__":
    unittest.main()
