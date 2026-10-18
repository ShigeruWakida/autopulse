import os
import tempfile

# Keep tests away from the real fallback queue in %TEMP%/AutoPulse/queue.
os.environ["AUTOPULSE_FALLBACK_QUEUE"] = os.path.join(tempfile.mkdtemp(prefix="autopulse-test-"), "fallback")
