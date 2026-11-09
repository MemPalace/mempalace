"""One ``urlopen`` for every request that carries a credential.

``urllib.request.urlopen`` follows redirects, and its redirect handler copies
every header but the content ones onto the new request: ``Authorization``
and API-key headers included, whatever host the ``Location`` names. A sync
peer, an LLM or embedding endpoint, a Qdrant server or a hub that answered
``302 Location: http://elsewhere/`` would be handing the credential meant
for it to ``elsewhere``. Nothing MemPalace talks to legitimately redirects a
credentialed call, so this opener follows none: a 3xx is raised as the
``HTTPError`` it is, naming the target that was not followed.
"""

from __future__ import annotations

import socket
import urllib.request
from urllib.error import HTTPError


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Raise on every redirect instead of re-sending the request elsewhere."""

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("Location") or headers.get("URI") or ""
        raise HTTPError(
            req.full_url,
            code,
            f"{msg} (redirect to {location!r} not followed: the request carries credentials)",
            headers,
            fp,
        )

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


_OPENER = urllib.request.build_opener(NoRedirectHandler)


def urlopen(url, data=None, timeout=socket._GLOBAL_DEFAULT_TIMEOUT):
    """``urllib.request.urlopen`` that never follows a redirect.

    Same arguments and return value; a 3xx reply raises ``HTTPError``.
    """
    return _OPENER.open(url, data, timeout)
