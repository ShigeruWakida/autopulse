"""Process abstraction around the `gh` CLI so GitHub access can be faked in tests."""

import os
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from auto_pulse.errors import AuthError, AutoPulseError, GhError, TransientError
from auto_pulse.redact import redact


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    def run(self, args: list[str], input: str | None = None) -> Result: ...


class SubprocessRunner:
    def __init__(self, executable: str = "gh", timeout: float = 60):
        self.executable = executable
        self.timeout = timeout

    def run(self, args: list[str], input: str | None = None) -> Result:
        env = os.environ.copy()
        env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
        try:
            proc = subprocess.run(
                [self.executable, *args],
                input=input,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AuthError("gh CLI が見つかりません（winget install --id GitHub.cli）") from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"gh がタイムアウトしました（{self.timeout}s）") from exc
        return Result(proc.returncode, proc.stdout, proc.stderr)


_TRANSIENT = re.compile(
    r"rate limit|http 429|http 5\d\d|could not resolve host|connection (refused|reset)|"
    r"timed? ?out|tls handshake|eof|no such host|network is unreachable|abuse detection"
)
_AUTH = re.compile(
    r"not logged in|gh auth login|http 401|bad credentials|requires authentication|"
    r"missing required scopes|has not been granted the required scopes"
)


def classify(result: Result) -> AutoPulseError:
    text = (result.stderr or result.stdout or "").strip()
    lower = text.lower()
    message = redact(text)[:500] or f"gh exited with {result.returncode}"
    if _AUTH.search(lower):
        return AuthError(message)
    if _TRANSIENT.search(lower):
        return TransientError(message)
    return GhError(message)


def check(result: Result) -> Result:
    if result.returncode != 0:
        raise classify(result)
    return result
