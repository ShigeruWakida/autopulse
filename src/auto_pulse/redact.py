"""Mask secrets before anything is printed, logged or stored in the queue."""

import re

_PATTERNS = [
    (re.compile(r"\b(gh[pousr]_)[A-Za-z0-9_]{10,}"), r"\1****"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{10,}"), "github_pat_****"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "sk-****"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AKIA****"),
    (re.compile(r"(?i)\b(bearer|token)(\s+)[A-Za-z0-9._\-]{16,}"), r"\1\2****"),
    (
        re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|access[_-]?token|token)(\s*[:=]\s*)[^\s'\"]+"),
        r"\1\2****",
    ),
]


def redact(text: str | None) -> str:
    if not text:
        return text or ""
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text
