"""MemPalace — Give your AI a memory. No API key required."""

# --- local patch: silence chromadb telemetry noise -------------------------
# chromadb 0.6.3 calls the pre-3.x posthog API (capture(id, event, props)),
# but posthog 7.x takes one positional arg. Every call raises TypeError and
# chromadb logs it as "Failed to send telemetry event ...". Nothing is
# actually sent — the noise is the only symptom.
#   1. opt out properly (sets posthog.disabled), unless the user set it.
#   2. silence the logger that prints the failure.
import logging as _logging
import os as _os

_os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
_logging.getLogger("chromadb.telemetry").setLevel(_logging.CRITICAL)
# ---------------------------------------------------------------------------

from .cli import main  # noqa: E402 - telemetry opt-out must precede chromadb imports
from .version import __version__  # noqa: E402 - keep package imports together

__all__ = ["main", "__version__"]
