"""One spelling of the identifier MemPalace sends on outgoing HTTP requests."""

import re

from mempalace.user_agent import USER_AGENT
from mempalace.version import __version__


def test_user_agent_is_a_versioned_mempalace_identifier():
    """Format-pin: WAF allowlists and operator log greps key off the
    ``mempalace/<semver>`` shape, and ``tests/test_qdrant_user_agent.py`` pins the
    same spelling from the other side. A refactor that drops the version segment,
    or grows a suffix on one site only, must fail loudly here."""
    assert re.fullmatch(r"mempalace/\d+\.\d+\.\d+", USER_AGENT), (
        f"USER_AGENT format mismatch: {USER_AGENT!r}"
    )
    assert __version__ in USER_AGENT
