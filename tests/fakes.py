"""In-memory stand-in for GhClient: one project, a few issues, failure injection."""

import copy

from auto_pulse.config import STATUSES, Config
from auto_pulse.errors import GhError, TransientError
from auto_pulse.github import EVENT_MARKER, IssueSnapshot

AC_BODY = """### Summary
テスト用 Issue

### Acceptance Criteria
- [x] 機能が動く
- [x] テストが通る

### Verification
unittest
"""


def make_config(**kw) -> Config:
    # Tests speak in logical status names; the label mapping has its own tests.
    # Most rule tests exercise the strict gates; auto mode has its own tests.
    cfg = Config(owner="me", project_number=1, repos=["me/repo"], status_labels={s: s for s in STATUSES}, mode="strict")
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class FakeClient:
    def __init__(self):
        self.issues: dict[tuple[str, int], dict] = {}
        self.items: dict[str, dict] = {}  # issue url -> field values
        self.comments: dict[tuple[str, int], list[str]] = {}
        self.calls: list[tuple] = []
        self.fail_next: list[Exception] = []  # raised by the next write calls, in order
        self.on_read = None  # hook(key) called on each get_issue, e.g. to simulate a concurrent edit

    def add_issue(self, number: int, body: str = AC_BODY, repo: str = "me/repo", fields: dict | None = None):
        url = f"https://github.com/{repo}/issues/{number}"
        self.issues[(repo, number)] = {"body": body, "url": url, "state": "OPEN", "title": f"Issue {number}"}
        self.comments[(repo, number)] = []
        if fields is not None:
            self.items[url] = dict(fields)
        return url

    def _write(self, *call):
        self.calls.append(call)
        if self.fail_next:
            raise self.fail_next.pop(0)

    # -- reads
    def get_issue(self, repo, number, project_owner, project_number):
        if self.on_read:
            self.on_read((repo, number))
        if (repo, number) not in self.issues:
            raise GhError(f"Could not resolve to an Issue with the number of {number}.")
        issue = self.issues[(repo, number)]
        fields = self.items.get(issue["url"])
        return IssueSnapshot(
            repo=repo,
            number=number,
            node_id=f"I_{number}",
            title=issue["title"],
            body=issue["body"],
            url=issue["url"],
            state=issue["state"],
            updated_at="2026-10-18T00:00:00Z",
            item_id=f"PVTI_{number}" if fields is not None else None,
            fields=copy.deepcopy(fields or {}),
        )

    def comment_event_ids(self, repo, number):
        ids = set()
        for body in self.comments[(repo, number)]:
            ids.update(EVENT_MARKER.findall(body))
        return ids

    # -- writes
    def add_item(self, owner, number, url):
        self._write("add_item", url)
        self.items.setdefault(url, {})
        return "PVTI"

    def set_field(self, owner, number, url, field_name, value):
        self._write("set_field", field_name, value)
        self.items[url][field_name] = value

    def set_fields(self, owner, number, url, values, item_id=None):
        self._write("set_fields", tuple(values))
        self.items.setdefault(url, {})
        for name, value in values.items():
            self.calls.append(("set_field", name, value))
            self.items[url][name] = value

    def edit_body(self, repo, number, body):
        self._write("edit_body", number)
        self.issues[(repo, number)]["body"] = body

    def add_comment(self, repo, number, body):
        self._write("comment", number)
        self.comments[(repo, number)].append(body)

    def create_issue(self, repo, title, body, labels):
        number = max([n for (r, n) in self.issues if r == repo] or [0]) + 1
        self._write("create_issue", title)
        url = self.add_issue(number, body=body, repo=repo)
        self.issues[(repo, number)]["title"] = title
        return url

    def issue_titles(self, repo):
        return {i["title"]: i["url"] for (r, _), i in self.issues.items() if r == repo}

    def close_issue(self, repo, number):
        self._write("close", number)
        self.issues[(repo, number)]["state"] = "CLOSED"


def offline() -> TransientError:
    return TransientError("dial tcp: no such host")
