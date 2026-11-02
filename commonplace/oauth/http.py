"""Small, bounded HTTP helpers for the OAuth and sign-in endpoints."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_BODY = 65536


class OAuthError(Exception):
    def __init__(self, error: str, description: str = "", status: int = 400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(error)


def pairs(items) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise OAuthError("invalid_request", "Duplicate parameters")
        result[key] = value
    return result


def query(path: str) -> dict:
    if len(path) > 16384:
        raise OAuthError("invalid_request", "Request too large")
    try:
        return pairs(parse_qsl(urlsplit(path).query, keep_blank_values=True, max_num_fields=40))
    except ValueError:
        raise OAuthError("invalid_request") from None


def body(h, *, as_json: bool = False) -> dict:
    media = (h.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
    expected = "application/json" if as_json else "application/x-www-form-urlencoded"
    if media != expected or h.headers.get("Transfer-Encoding"):
        raise OAuthError("invalid_request", "Unsupported request body", 415)
    try:
        length = int(h.headers.get("Content-Length", "0"))
    except ValueError:
        length = -1
    if not 0 < length <= MAX_BODY:
        raise OAuthError("invalid_request", "Invalid body size")
    try:
        raw = h.rfile.read(length).decode("utf-8")
        data = (
            json.loads(raw, object_pairs_hook=pairs)
            if as_json
            else pairs(parse_qsl(raw, keep_blank_values=True, max_num_fields=40))
        )
    except (ValueError, UnicodeError):
        raise OAuthError("invalid_request", "Malformed request body") from None
    if not isinstance(data, dict):
        raise OAuthError("invalid_request")
    return data


def send(
    h,
    status: int,
    payload: bytes = b"",
    *,
    media="application/json",
    headers=(),
    referrer_policy="no-referrer",
) -> None:
    h.send_response(status)
    h.send_header("Content-Type", media)
    h.send_header("Content-Length", str(len(payload)))
    h.send_header("Cache-Control", "no-store")
    h.send_header("Pragma", "no-cache")
    h.send_header("Referrer-Policy", referrer_policy)
    h.send_header("X-Content-Type-Options", "nosniff")
    for key, value in headers:
        h.send_header(key, value)
    # A rejected body must not become another HTTP request on a keepalive connection.
    h.send_header("Connection", "close")
    h.close_connection = True
    h.end_headers()
    h.wfile.write(payload)


def send_json(h, status: int, payload: dict) -> None:
    send(h, status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def send_error(h, error: OAuthError) -> None:
    data = {"error": error.error}
    if error.description:
        data["error_description"] = error.description
    send_json(h, error.status, data)


def redirect(h, location: str, *, status=302, headers=()) -> None:
    send(h, status, headers=(("Location", location), *headers))


def callback(uri: str, values: dict) -> str:
    parts = urlsplit(uri)
    query_items = parse_qsl(parts.query, keep_blank_values=True) + list(values.items())
    return urlunsplit(parts._replace(query=urlencode(query_items)))
