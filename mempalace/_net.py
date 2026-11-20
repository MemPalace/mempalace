"""One ``urlopen`` for every request that carries a credential.

``urllib.request.urlopen`` follows redirects, and its redirect handler copies
every header but the content ones onto the new request: ``Authorization``
and API-key headers included, whatever host the ``Location`` names. A sync
peer, an LLM or embedding endpoint, a Qdrant server or a hub that answered
``302 Location: http://elsewhere/`` would be handing the credential meant
for it to ``elsewhere``. Nothing MemPalace talks to legitimately redirects a
credentialed call, so this opener follows none: a 3xx is raised as the
``HTTPError`` it is, naming the target that was not followed.

Also the bind step the HTTP servers share, which skips the reverse DNS lookup
``http.server`` would do on the bind address.
"""

from __future__ import annotations

import socket
import socketserver
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


def bind_without_reverse_dns(server: socketserver.TCPServer) -> None:
    """``server_bind`` for an ``HTTPServer`` that keeps the literal host.

    ``http.server.HTTPServer.server_bind`` sets ``server_name`` through
    ``socket.getfqdn(host)``, a reverse DNS lookup of the bind address. On a
    host with slow or absent reverse DNS for that address the lookup blocks
    until the resolver gives up (35 s on one hosted CI runner), which looks
    exactly like the server never coming up. Nothing reads the name except
    CGI handling, so bind as ``TCPServer`` does and name the server after the
    literal host.
    """
    socketserver.TCPServer.server_bind(server)
    host, port = server.server_address[:2]
    server.server_name = host
    server.server_port = port
