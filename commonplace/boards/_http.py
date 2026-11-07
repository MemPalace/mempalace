"""Private contract-compatible HTTP helpers until shared helpers are merged."""

import json
from urllib.parse import parse_qsl, unquote, urlsplit


class HTTPError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


def read_json(h) -> dict:
    try:
        length = int(h.headers.get("Content-Length", "0"))
    except ValueError:
        h.close_connection = True
        raise HTTPError(400, "invalid content length") from None
    if length < 0 or length > 1024 * 1024:
        h.close_connection = True
        raise HTTPError(413, "request too large")
    try:
        value = json.loads(h.rfile.read(length).decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPError(400, "invalid JSON") from None
    if not isinstance(value, dict):
        raise HTTPError(400, "expected a JSON object")
    return value


def send_text(h, status, text, content_type, headers=None):
    body = text.encode("utf-8")
    h.send_response(status)
    h.send_header("Content-Type", content_type)
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    for name, value in (headers or {}).items():
        h.send_header(name, value)
    h.end_headers()
    h.wfile.write(body)


def send_json(h, status, obj, headers=None):
    send_text(
        h, status, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8", headers
    )


def require_principal(gateway, h):
    principal = gateway.principal_for(h)
    if principal is None:
        send_json(h, 401, {"error": "unauthorized"})
    return principal


def path_parts(h, prefix):
    path = urlsplit(h.path).path
    return [unquote(part) for part in path[len(prefix) :].strip("/").split("/") if part]


def query(h):
    return dict(parse_qsl(urlsplit(h.path).query, keep_blank_values=True))
