import os
import tempfile

# Keep tests away from real user state: the fallback queue in %TEMP%/AutoPulse/queue
# and the Codex user config in ~/.codex.
_sandbox = tempfile.mkdtemp(prefix="autopulse-test-")
os.environ["AUTOPULSE_FALLBACK_QUEUE"] = os.path.join(_sandbox, "fallback")
os.environ["CODEX_HOME"] = os.path.join(_sandbox, "codex-home")
