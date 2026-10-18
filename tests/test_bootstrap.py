import os
import tempfile
import unittest
from unittest import mock

from auto_pulse import bootstrap, config
from auto_pulse.config import FIELD_SPECS, STATUSES
from auto_pulse.errors import PolicyRefused
from auto_pulse.github import ProjectField, ProjectInfo


class FakeProjects:
    def __init__(self, projects=None):
        self.projects = projects or {}  # number -> ProjectInfo
        self.calls = []

    def list_projects(self, owner):
        return [{"number": n, "title": p.title, "closed": False} for n, p in self.projects.items()]

    def get_project(self, owner, number):
        return self.projects.get(number)

    def create_project(self, owner, title):
        self.calls.append(("create_project", title))
        status = ProjectField("F_status", "Status", "SINGLE_SELECT", list(bootstrap.DEFAULT_NEW_STATUS))
        self.projects[3] = ProjectInfo("P", 3, title, "u", 0, {"Title": ProjectField("F_t", "Title", "TITLE"), "Status": status})
        return 3

    def replace_single_select_options(self, field_id, options):
        self.calls.append(("replace", field_id, [n for n, _ in options]))
        for p in self.projects.values():
            for f in p.fields.values():
                if f.id == field_id:
                    f.options = [n for n, _ in options]

    def create_field(self, owner, number, name, data_type, options=None):
        self.calls.append(("create_field", name, data_type))
        self.projects[number].fields[name] = ProjectField("F_" + name, name, data_type, options or [])


class BootstrapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"AUTOPULSE_HOME": self.tmp.name})
        self.env.start()
        os.environ.pop("AUTOPULSE_CONFIG", None)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_new_project_plan_needs_status_flag(self):
        fake = FakeProjects()
        plan = bootstrap.build(fake, "me", "Personal Development", None, ["me/RoboScope"])
        self.assertTrue(plan.create_project)
        self.assertEqual(plan.replace_status_options, bootstrap.DEFAULT_NEW_STATUS)
        self.assertEqual(len(plan.create_fields), len(FIELD_SPECS) - 1)
        self.assertEqual(fake.calls, [], "planning must not write")
        with self.assertRaises(PolicyRefused):
            bootstrap.apply(fake, plan, update_status_options=False)
        self.assertEqual(fake.calls, [])

    def test_apply_creates_project_fields_and_config(self):
        fake = FakeProjects()
        plan = bootstrap.build(fake, "me", "Personal Development", None, ["me/RoboScope"])
        cfg = bootstrap.apply(fake, plan, update_status_options=True)
        self.assertEqual(cfg.project_number, 3)
        self.assertEqual(fake.projects[3].fields["Status"].options, STATUSES)
        self.assertIn(("create_field", "Next Action", "TEXT"), fake.calls)
        loaded = config.load()
        self.assertEqual((loaded.project_number, loaded.repos), (3, ["me/RoboScope"]))

    def test_existing_field_with_wrong_type_is_a_conflict(self):
        fields = {"Status": ProjectField("s", "Status", "SINGLE_SELECT", STATUSES), "Effort": ProjectField("e", "Effort", "TEXT")}
        fake = FakeProjects({5: ProjectInfo("P", 5, "Personal Development", "u", 4, fields)})
        plan = bootstrap.build(fake, "me", "Personal Development", None, [])
        self.assertEqual(plan.number, 5)
        self.assertTrue(any("Effort" in c for c in plan.conflicts))
        with self.assertRaises(PolicyRefused):
            bootstrap.apply(fake, plan, update_status_options=True)

    def test_status_options_not_replaced_when_items_exist(self):
        fields = {"Status": ProjectField("s", "Status", "SINGLE_SELECT", ["Todo", "In Progress", "Done"])}
        fake = FakeProjects({5: ProjectInfo("P", 5, "Personal Development", "u", 2, fields)})
        plan = bootstrap.build(fake, "me", "Personal Development", 5, [])
        with self.assertRaises(PolicyRefused):
            bootstrap.apply(fake, plan, update_status_options=True)
        self.assertNotIn("replace", [c[0] for c in fake.calls])


if __name__ == "__main__":
    unittest.main()
