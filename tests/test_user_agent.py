"""One spelling of the identifier MemPalace sends on outgoing HTTP requests.

The Wikipedia lookup is exercised here because every test in
``tests/test_entity_registry.py`` patches ``_wikipedia_lookup`` itself, so
nothing else watches the header that function actually sends.
"""

import json
import re

from mempalace import entity_registry
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


class _FakeWikipediaResponse:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps({"type": "standard", "title": "Ada", "extract": "A person."}).encode()


def test_wikipedia_lookup_sends_the_shared_identifier(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["ua"] = req.get_header("User-agent")
        captured["url"] = req.full_url
        return _FakeWikipediaResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    entity_registry._wikipedia_lookup("Ada")

    assert captured["url"].startswith("https://en.wikipedia.org/api/rest_v1/page/summary/")
    assert captured["ua"] == USER_AGENT
