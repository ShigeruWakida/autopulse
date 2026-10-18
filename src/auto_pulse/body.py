"""The AutoPulse-managed block inside an issue body, and Acceptance Criteria parsing.

Only the text between the start/end markers is ever rewritten. The block carries a
hash of its own content so a human edit inside it is detected and left alone.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

START = "<!-- autopulse:start -->"
END = "<!-- autopulse:end -->"
_HASH = re.compile(r"<!-- autopulse:hash=([0-9a-f]{12}) -->")
_META = re.compile(r"<!-- autopulse:meta=(\{.*?\}) -->")
_ITEM = re.compile(r"^- \*\*(.+?):\*\* ?(.*)$")
KEYS = ["Status", "Agent", "Last Activity", "Next Action", "Blockers", "Evidence"]

_AC_HEADING = re.compile(r"^#{1,6}\s*(acceptance criteria|完了条件)\b", re.IGNORECASE)
_HEADING = re.compile(r"^#{1,6}\s")
_CHECKBOX = re.compile(r"^\s*[-*]\s+\[([ xX])\]\s+(.*)$")


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in re.sub(r"\r+\n", "\n", text).split("\n")).strip()


def _digest(content: str) -> str:
    return hashlib.sha256(_normalize(content).encode("utf-8")).hexdigest()[:12]


@dataclass
class Managed:
    present: bool = False
    values: dict[str, str] = field(default_factory=dict)
    intact: bool = True  # False when a human edited inside the block (or markers are broken)
    meta: dict = field(default_factory=dict)  # hidden bookkeeping, e.g. a held human Next Action


def _locate(body: str) -> tuple[int, int] | None:
    start = body.find(START)
    if start < 0:
        return None
    end = body.find(END, start)
    if end < 0:
        return (start, -1)
    return (start, end + len(END))


def parse(body: str) -> Managed:
    span = _locate(body)
    if span is None:
        return Managed()
    if span[1] < 0 or body.count(START) != 1 or body.count(END) != 1:
        return Managed(present=True, intact=False)
    inner = body[span[0] + len(START) : span[1] - len(END)]
    hash_match = _HASH.search(inner)
    content = _HASH.sub("", inner)
    meta = {}
    meta_match = _META.search(content)
    if meta_match:
        try:
            meta = json.loads(meta_match.group(1))
        except json.JSONDecodeError:
            meta = {}
    values = {}
    for line in _normalize(content).split("\n"):
        m = _ITEM.match(line.strip())
        if m:
            values[m.group(1)] = m.group(2).strip()
    intact = bool(hash_match) and hash_match.group(1) == _digest(content)
    return Managed(present=True, values=values, intact=intact, meta=meta)


def render(values: dict[str, str], meta: dict | None = None) -> str:
    lines = ["### AutoPulse"]
    if meta:
        lines.insert(0, f"<!-- autopulse:meta={json.dumps(meta, ensure_ascii=False, sort_keys=True)} -->")
    for key in KEYS:
        value = values.get(key)
        if value:
            lines.append(f"- **{key}:** {_one_line(value)}")
    content = "\n".join(lines)
    return f"{START}\n<!-- autopulse:hash={_digest(content)} -->\n{content}\n{END}"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def apply(body: str, values: dict[str, str], meta: dict | None = None) -> str:
    """Return body with the managed block replaced (or appended). Text outside keeps its content;
    only line endings are normalized to LF (GitHub returns CRLF for web-edited bodies)."""
    body = re.sub(r"\r+\n", "\n", body)
    block = render(values, meta)
    span = _locate(body)
    if span is None:
        base = body.rstrip()
        return f"{base}\n\n{block}\n" if base else f"{block}\n"
    return body[: span[0]] + block + body[span[1] :]


def outside(body: str) -> str:
    span = _locate(body)
    if span is None or span[1] < 0:
        return body
    return body[: span[0]] + body[span[1] :]


@dataclass
class Criterion:
    checked: bool
    text: str


def acceptance_criteria(body: str) -> list[Criterion] | None:
    """Checkbox items under an 'Acceptance Criteria' heading. None when there is no such heading."""
    lines = outside(body).replace("\r\n", "\n").split("\n")
    found = False
    items: list[Criterion] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if _AC_HEADING.match(stripped):
            found = in_section = True
            continue
        if in_section and _HEADING.match(stripped):
            in_section = False
        if in_section:
            m = _CHECKBOX.match(line)
            if m:
                items.append(Criterion(checked=m.group(1) in "xX", text=m.group(2).strip()))
    return items if found else None
