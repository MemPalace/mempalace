"""Fixed request flow used to capture and replay ACL-off HTTP bytes.

golden_flow.json is captured upstream output; its two u2014 escapes are upstream
text (the AAAK spec), not ours, and are left exactly as captured.
"""

import http.client
import json

TOKEN = "golden-legacy-token"

_ADD = "mempalace_add_drawer"
FLOW = [
    ("initialize", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
    ("ping", {"jsonrpc": "2.0", "id": 2, "method": "ping"}),
    ("tools_list", {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}),
    ("unknown_method", {"jsonrpc": "2.0", "id": 4, "method": "nope/nothing"}),
    (
        "unknown_tool",
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "mempalace_no_such_tool", "arguments": {}},
        },
    ),
    (
        "add_alpha",
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": _ADD,
                "arguments": {"wing": "alpha", "room": "notes", "content": "Alpha golden drawer."},
            },
        },
    ),
    (
        "add_beta",
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": _ADD,
                "arguments": {"wing": "beta", "room": "notes", "content": "Beta golden drawer."},
            },
        },
    ),
    (
        "list_wings",
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {"name": "mempalace_list_wings", "arguments": {}},
        },
    ),
    (
        "list_rooms",
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "mempalace_list_rooms", "arguments": {"wing": "alpha"}},
        },
    ),
    (
        "aaak_spec",
        {
            "jsonrpc": "2.0",
            "id": 10,
            "method": "tools/call",
            "params": {"name": "mempalace_get_aaak_spec", "arguments": {}},
        },
    ),
    (
        "bad_args",
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {"name": "mempalace_get_drawer", "arguments": {"nope": 1}},
        },
    ),
]


def post(port, body, token=TOKEN, path="/mcp"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        raw = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        conn.request("POST", path, body=raw, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8")
    finally:
        conn.close()


def run_flow(port, token=TOKEN):
    """name -> [status, body] for every request, plus a bad-token 401 and healthz."""
    out = {name: list(post(port, body, token=token)) for name, body in FLOW}
    status, body = post(port, FLOW[1][1], token="wrong")
    # http.server's error page markup differs between Python versions; keep only the stable line.
    out["bad_token"] = [status, "Error code: 401" if "<p>Error code: 401</p>" in body else body]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", "/healthz")
        resp = conn.getresponse()
        out["healthz"] = [resp.status, resp.read().decode()]
    finally:
        conn.close()
    return out
