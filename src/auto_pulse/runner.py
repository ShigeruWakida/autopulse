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
            # Bytes, not text mode: on Windows text mode turns "\r\n" from GitHub into "\r\r\n".
            proc = subprocess.run(
                [self.executable, *args],
                input=input.encode("utf-8") if input is not None else None,
                capture_output=True,
                timeout=self.timeout,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AuthError("gh CLI が見つかりません（winget install --id GitHub.cli）") from exc
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"gh がタイムアウトしました（{self.timeout}s）") from exc
        return Result(
            proc.returncode,
            proc.stdout.decode("utf-8", errors="replace"),
            proc.stderr.decode("utf-8", errors="replace"),
        )


_RATE_LIMIT = re.compile(r"rate limit|http 429|http 5\d\d|abuse detection")
_AUTH = re.compile(
    r"not logged in|gh auth login|http 401|bad credentials|requires authentication|"
    r"missing required scopes|has not been granted the required scopes"
)
# Only a definite answer from GitHub is permanent. Anything else (proxy, DNS, "connectex ...
# actively refused", timeouts, unknown output) is retried, so an event is never dropped.
_PERMANENT = re.compile(r"http 4\d\d|could not resolve to|graphql: |not found|validation failed|unprocessable")


def classify(result: Result) -> AutoPulseError:
    text = (result.stderr or result.stdout or "").strip()
    lower = text.lower()
    message = redact(text)[:500] or f"gh exited with {result.returncode}"
    if _AUTH.search(lower):
        return AuthError(message)
    if _RATE_LIMIT.search(lower):
        return TransientError(message)
    if _PERMANENT.search(lower):
        return GhError(message)
    return TransientError(message)


def check(result: Result) -> Result:
    if result.returncode != 0:
        raise classify(result)
    return result
