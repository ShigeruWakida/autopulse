"""GitHub access through `gh`. Every call goes through a Runner."""

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from auto_pulse.errors import GhError
from auto_pulse.runner import Runner, check

_FIELD_VALUES = """
fieldValues(first: 50) {
  nodes {
    __typename
    ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
    ... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
    ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
    ... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
  }
}"""

_ISSUE_FIELDS = (
    """
id number title body url state updatedAt
labels(first: 30) { nodes { name } }
projectItems(first: 20) {
  nodes {
    id
    project { id number owner { ... on User { login } ... on Organization { login } } }
    """
    + _FIELD_VALUES
    + """
  }
}"""
)

ISSUE_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) { issue(number: $number) {"
    + _ISSUE_FIELDS
    + "} } }"
)

OPEN_ISSUES_QUERY = (
    "query($owner: String!, $name: String!) {"
    " repository(owner: $owner, name: $name) {"
    " issues(first: 100, states: OPEN, orderBy: {field: UPDATED_AT, direction: DESC}) { nodes {"
    + _ISSUE_FIELDS
    + "} } } }"
)

PROJECT_QUERY = """
query($owner: String!, $number: Int!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id number title url closed
        items(first: 1) { totalCount }
        fields(first: 50) {
          nodes {
            ... on ProjectV2FieldCommon { id name dataType }
            ... on ProjectV2SingleSelectField { options { id name } }
          }
        }
      }
    }
  }
}"""

PROJECT_ITEMS_QUERY = (
    """
query($owner: String!, $number: Int!, $after: String) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        items(first: 100, after: $after) {
          pageInfo { hasNextPage endCursor }
          nodes {
            id
            content {
              __typename
              ... on Issue { number title url state body repository { nameWithOwner } }
              ... on PullRequest { number title url state repository { nameWithOwner } }
            }
            """
    + _FIELD_VALUES
    + """
          }
        }
      }
    }
  }
}"""
)

UPDATE_OPTIONS_MUTATION = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field { ... on ProjectV2SingleSelectField { name options { name } } }
  }
}"""

EVENT_MARKER = re.compile(r"<!-- autopulse:event=([0-9a-f\-]{36}) -->")


@dataclass
class IssueSnapshot:
    repo: str
    number: int
    node_id: str
    title: str
    body: str
    url: str
    state: str
    updated_at: str
    labels: list[str] = field(default_factory=list)
    item_id: str | None = None  # project item in the configured project
    fields: dict[str, Any] = field(default_factory=dict)  # actual field name -> value


@dataclass
class ProjectField:
    id: str
    name: str
    data_type: str  # TEXT, SINGLE_SELECT, DATE, NUMBER, TITLE, ...
    options: list[str] = field(default_factory=list)


@dataclass
class ProjectInfo:
    id: str
    number: int
    title: str
    url: str
    item_count: int
    fields: dict[str, ProjectField]


def parse_github_url(url: str) -> str | None:
    """owner/name from an https or ssh GitHub remote URL."""
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
    return f"{m.group(1)}/{m.group(2)}" if m else None


def origin_of(path: str | None = None) -> str | None:
    args = ["git"] + (["-C", path] if path else []) + ["remote", "get-url", "origin"]
    try:
        url = subprocess.run(args, capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_github_url(url)


def split_repo(repo: str) -> tuple[str, str]:
    owner, sep, name = repo.partition("/")
    if not sep or not owner or not name or "/" in name:
        raise GhError(f"リポジトリは owner/name 形式で指定してください: {repo!r}")
    return owner, name


def _parse_issue(repo: str, node: dict, project_owner: str | None, project_number: int | None) -> IssueSnapshot:
    snap = IssueSnapshot(
        repo=repo,
        number=node["number"],
        node_id=node["id"],
        title=node["title"],
        body=node.get("body") or "",
        url=node["url"],
        state=node["state"],
        updated_at=node["updatedAt"],
        labels=[label["name"] for label in node.get("labels", {}).get("nodes", [])],
    )
    for item in node.get("projectItems", {}).get("nodes", []):
        project = item.get("project") or {}
        owner = (project.get("owner") or {}).get("login", "")
        if project.get("number") == project_number and owner.lower() == (project_owner or "").lower():
            snap.item_id = item["id"]
            for value in item.get("fieldValues", {}).get("nodes", []):
                name = (value.get("field") or {}).get("name")
                if not name:
                    continue
                for key in ("name", "text", "date", "number"):
                    if key in value:
                        snap.fields[name] = value[key]
                        break
    return snap


class GhClient:
    def __init__(self, runner: Runner):
        self.runner = runner

    # -- low level ---------------------------------------------------------
    def gh(self, *args: str, input: str | None = None) -> str:
        return check(self.runner.run(list(args), input=input)).stdout

    def graphql(self, query: str, variables: dict) -> dict:
        payload = json.dumps({"query": query, "variables": variables})
        data = json.loads(self.gh("api", "graphql", "--input", "-", input=payload))
        if data.get("errors"):
            raise GhError("; ".join(e.get("message", "?") for e in data["errors"])[:500])
        return data["data"]

    # -- environment -------------------------------------------------------
    def version(self) -> str:
        return self.gh("--version").splitlines()[0].strip()

    def auth_status(self) -> dict:
        result = self.runner.run(["auth", "status"])
        text = result.stdout + result.stderr
        account = re.search(r"account (\S+)", text)
        scopes = re.search(r"Token scopes: (.*)", text)
        return {
            "logged_in": result.returncode == 0,
            "account": account.group(1) if account else None,
            "scopes": re.findall(r"'([^']+)'", scopes.group(1)) if scopes else [],
        }

    def repo_view(self, repo: str) -> dict:
        out = self.gh("repo", "view", repo, "--json", "nameWithOwner,viewerPermission,hasIssuesEnabled,isArchived")
        return json.loads(out)

    # -- issues ------------------------------------------------------------
    def get_issue(self, repo: str, number: int, project_owner: str | None, project_number: int | None) -> IssueSnapshot:
        owner, name = split_repo(repo)
        data = self.graphql(ISSUE_QUERY, {"owner": owner, "name": name, "number": number})
        node = (data.get("repository") or {}).get("issue")
        if not node:
            raise GhError(f"{repo}#{number} が見つかりません")
        return _parse_issue(repo, node, project_owner, project_number)

    def open_issues(self, repo: str, project_owner: str | None, project_number: int | None) -> list[IssueSnapshot]:
        owner, name = split_repo(repo)
        data = self.graphql(OPEN_ISSUES_QUERY, {"owner": owner, "name": name})
        nodes = ((data.get("repository") or {}).get("issues") or {}).get("nodes", [])
        return [_parse_issue(repo, n, project_owner, project_number) for n in nodes]

    def comment_event_ids(self, repo: str, number: int) -> set[str]:
        out = self.gh("api", f"repos/{repo}/issues/{number}/comments", "--paginate", "--jq", ".[].body | @json")
        ids: set[str] = set()
        for line in out.splitlines():
            if line.strip():
                ids.update(EVENT_MARKER.findall(json.loads(line)))
        return ids

    def add_comment(self, repo: str, number: int, body: str) -> None:
        self.gh("issue", "comment", str(number), "--repo", repo, "--body-file", "-", input=body)

    def edit_body(self, repo: str, number: int, body: str) -> None:
        self.gh("issue", "edit", str(number), "--repo", repo, "--body-file", "-", input=body)

    def close_issue(self, repo: str, number: int) -> None:
        self.gh("issue", "close", str(number), "--repo", repo, "--reason", "completed")

    # -- projects ----------------------------------------------------------
    def list_projects(self, owner: str) -> list[dict]:
        out = self.gh("project", "list", "--owner", owner, "--format", "json", "--limit", "100")
        return json.loads(out).get("projects", [])

    def get_project(self, owner: str, number: int) -> ProjectInfo | None:
        data = self.graphql(PROJECT_QUERY, {"owner": owner, "number": number})
        project = (data.get("repositoryOwner") or {}).get("projectV2")
        if not project:
            return None
        fields = {}
        for node in project["fields"]["nodes"]:
            if "name" not in node:
                continue
            fields[node["name"]] = ProjectField(
                id=node["id"],
                name=node["name"],
                data_type=node.get("dataType", ""),
                options=[o["name"] for o in node.get("options") or []],
            )
        return ProjectInfo(
            id=project["id"],
            number=project["number"],
            title=project["title"],
            url=project["url"],
            item_count=project["items"]["totalCount"],
            fields=fields,
        )

    def project_items(self, owner: str, number: int) -> list[dict]:
        """All items with content type, repo, state, body and field values (by field name)."""
        items, after = [], None
        while True:
            data = self.graphql(PROJECT_ITEMS_QUERY, {"owner": owner, "number": number, "after": after})
            page = data["repositoryOwner"]["projectV2"]["items"]
            for node in page["nodes"]:
                content = node.get("content") or {}
                fields = {}
                for value in node.get("fieldValues", {}).get("nodes", []):
                    name = (value.get("field") or {}).get("name")
                    for key in ("name", "text", "date", "number"):
                        if name and key in value:
                            fields[name] = value[key]
                            break
                items.append({
                    "id": node["id"],
                    "type": content.get("__typename", "DraftIssue"),
                    "repo": (content.get("repository") or {}).get("nameWithOwner"),
                    "number": content.get("number"),
                    "title": content.get("title", ""),
                    "url": content.get("url"),
                    "state": content.get("state"),
                    "body": content.get("body") or "",
                    "fields": fields,
                })
            if not page["pageInfo"]["hasNextPage"]:
                return items
            after = page["pageInfo"]["endCursor"]

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> bool:
        """Create the label if missing. Returns True when it was created."""
        if self.label_exists(repo, name):
            return False
        self.gh("label", "create", name, "--repo", repo, "--color", color, "--description", description)
        return True

    def label_exists(self, repo: str, name: str) -> bool:
        out = self.gh("label", "list", "--repo", repo, "--search", name, "--json", "name", "--limit", "100")
        return any(label["name"].lower() == name.lower() for label in json.loads(out or "[]"))

    def create_project(self, owner: str, title: str) -> int:
        out = self.gh("project", "create", "--owner", owner, "--title", title, "--format", "json")
        return int(json.loads(out)["number"])

    def create_field(self, owner: str, number: int, name: str, data_type: str, options: list[str] | None = None) -> None:
        args = ["project", "field-create", str(number), "--owner", owner, "--name", name, "--data-type", data_type]
        if options:
            args += ["--single-select-options", ",".join(options)]
        self.gh(*args)

    def replace_single_select_options(self, field_id: str, options: list[tuple[str, str]]) -> None:
        self.graphql(
            UPDATE_OPTIONS_MUTATION,
            {"field": field_id, "options": [{"name": n, "color": c, "description": ""} for n, c in options]},
        )

    def add_item(self, owner: str, number: int, url: str) -> str:
        out = self.gh("project", "item-add", str(number), "--owner", owner, "--url", url, "--format", "json")
        return json.loads(out)["id"]

    def set_field(self, owner: str, number: int, url: str, field_name: str, value: Any) -> None:
        args = ["project", "item-edit", str(number), "--owner", owner, "--url", url, "--field", field_name]
        if value is None or value == "":
            args.append("--clear")
        else:
            args += ["--value", str(value)]
        self.gh(*args)
