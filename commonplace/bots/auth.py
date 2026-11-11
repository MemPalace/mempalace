"""Who may use ``/api/bots``: a signed-in person, and nobody else.

An agent token or machine credential is answered 404, exactly like a route that does not exist.
"""

from commonplace.http_util import send_json


def person(gateway, h):
    """The signed-in person's principal, or None after the reply (401 anonymous, 404 otherwise)."""
    principal = gateway.principal_for(h)
    if principal is None:
        send_json(h, 401, {"error": "Sign in"})
        return None
    if not principal.person:
        send_json(h, 404, {"error": "Not found", "code": "not_found"})
        return None
    return principal
