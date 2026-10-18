"""Error types and process exit codes."""

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    ERROR = 1  # unexpected / permanent failure
    USAGE = 2  # bad arguments (argparse also uses 2)
    QUEUED = 3  # event saved locally, GitHub not updated yet (retry with `sync`)
    REFUSED = 4  # policy refused or downgraded (e.g. Done without verification)
    CONFLICT = 5  # concurrent/human edit detected; current values were kept
    SETUP = 6  # configuration, authentication or permission problem


class AutoPulseError(Exception):
    exit_code = ExitCode.ERROR
    retryable = False


class TransientError(AutoPulseError):
    """Network trouble, rate limit, 5xx: keep the event and retry later."""

    exit_code = ExitCode.QUEUED
    retryable = True


class AuthError(TransientError):
    """gh missing or not logged in. Retryable once the user fixes it."""


class GhError(AutoPulseError):
    """gh returned an error that retrying will not fix."""


class ConfigError(AutoPulseError):
    exit_code = ExitCode.SETUP


class PolicyRefused(AutoPulseError):
    exit_code = ExitCode.REFUSED
