"""The identifier MemPalace sends on outgoing HTTP requests.

Cloudflare-fronted endpoints (Workers AI, AI Gateway, Together, OpenRouter, ...)
commonly run a WAF "User Agent Blocking" rule against Python's stdlib default
``Python-urllib/<py-version>`` and answer a bare 403 before auth (issue #1570).
An explicit identifier passes those checks and gives operators something
searchable in their logs.

Call sites import ``USER_AGENT`` rather than formatting their own string, so the
LLM client, the embedding function, the qdrant backend, the Wikipedia lookup and
the release check cannot drift into spelling it differently.
"""

from .version import __version__

USER_AGENT = f"mempalace/{__version__}"
