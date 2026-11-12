"""A drawer that holds a stray source byte survives every way out.

``fsio.decode_source`` keeps a stray byte in UTF-8 text as a lone surrogate.
External MCP clients see it as U+FFFD (``stored_text.dumps_client``), and
``mempalace_get_drawer(include_source_bytes=True)`` returns the exact bytes.
Hops between MemPalace processes keep it exact as a ``\\udcXX`` escape
(``stored_text.dumps_internal``). Exports write the original byte back, and
services that only take valid Unicode get U+FFFD.
"""

import base64
import http.client
import io
import json
import os
import threading

import pytest

from mempalace.stored_text import EXACT_WIRE_HEADER, dumps_client, dumps_internal

# These drive a loopback hub with raw HTTP requests, so they serve it tokenless.
pytestmark = pytest.mark.usefixtures("tokenless_http_hub")

STRAY_BYTES = ("café zebrafinch " * 10).encode("utf-8") + b"stray\xffbyte"
STRAY_TEXT = "café zebrafinch " * 10 + "stray\udcffbyte"


def test_dumps_internal_keeps_a_lone_surrogate_exact():
    text = dumps_internal({"text": "café \udcff", "n": [1]})
    assert "café" in text and "\\udcff" in text
    text.encode("utf-8")
    assert json.loads(text) == {"text": "café \udcff", "n": [1]}


def test_dumps_client_shows_a_lone_surrogate_as_u_fffd():
    inner = json.dumps({"t": "\udcff"}, ensure_ascii=False)
    text = dumps_client({"text": "café \udcff", "inner": inner})
    assert "\\ud" not in text.lower()
    assert json.loads(text)["text"] == "café �"


def test_stdio_reply_shows_a_lone_surrogate_as_u_fffd():
    from mempalace import mcp_server

    def dispatch(request):
        content = [{"type": "text", "text": STRAY_TEXT}]
        return {"jsonrpc": "2.0", "id": request["id"], "result": {"content": content}}

    reply = mcp_server._stdio_reply('{"jsonrpc": "2.0", "id": 1, "method": "x"}', dispatch)
    assert "\\udcff" not in reply
    assert json.loads(reply)["result"]["content"][0]["text"] == STRAY_TEXT.replace("\udcff", "�")


def test_llm_request_body_sends_u_fffd_for_a_lone_surrogate():
    from mempalace.llm_client import json_request_body

    body = json_request_body({"messages": [{"content": "café \udcff"}]})
    assert json.loads(body.decode("utf-8")) == {"messages": [{"content": "café �"}]}


def test_daemon_responses_keep_a_lone_surrogate_exact():
    from mempalace.daemon import _json_response

    class Handler:
        wfile = io.BytesIO()

        def send_response(self, status):
            pass

        def send_header(self, name, value):
            pass

        def end_headers(self):
            pass

    handler = Handler()
    _json_response(handler, 200, {"result": STRAY_TEXT})
    assert json.loads(handler.wfile.getvalue())["result"] == STRAY_TEXT


@pytest.fixture
def stray_palace(tmp_path, palace_path):
    from mempalace import miner

    src = tmp_path / "src"
    src.mkdir()
    (src / "n.txt").write_bytes(STRAY_BYTES)
    miner.mine(str(src), palace_path, wing_override="proj", agent="t")
    return palace_path


@pytest.fixture
def stray_server(monkeypatch, config, kg, stray_palace):
    from mempalace import mcp_server
    from mempalace.palace import get_collection

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
    drawer_id = get_collection(stray_palace).get()["ids"][0]
    httpd = mcp_server._build_http_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield httpd.server_address[1], drawer_id
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_export_writes_the_stray_byte_back(tmp_path, stray_palace):
    from mempalace.exporter import export_palace

    out = tmp_path / "export"
    export_palace(stray_palace, str(out))
    written = b"".join(
        open(os.path.join(root, name), "rb").read()
        for root, _, names in os.walk(out)
        for name in names
        if name.endswith(".md")
    )
    assert b"stray\xffbyte" in written


def _call(port, name, arguments, headers=None):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    ).encode("utf-8")
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("POST", "/mcp", body, {"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _text(raw):
    payload = json.loads(raw)
    assert "error" not in payload, payload
    return payload["result"]["content"][0]["text"]


def test_http_search_and_get_drawer_show_u_fffd_to_a_client(stray_server):
    port, drawer_id = stray_server
    for name, arguments, holds_drawer in (
        ("mempalace_search", {"query": "zebrafinch", "limit": 3}, True),
        ("mempalace_search", {"query": "nomatchwordxyz", "limit": 3}, False),
        ("mempalace_get_drawer", {"drawer_id": drawer_id}, True),
    ):
        status, raw = _call(port, name, arguments)
        assert status == 200, (name, raw[:200])
        assert b"\\udc" not in raw.lower()
        if holds_drawer:
            assert "stray�byte" in _text(raw)


def test_http_answers_another_mempalace_process_exactly(stray_server):
    port, drawer_id = stray_server
    status, raw = _call(
        port, "mempalace_get_drawer", {"drawer_id": drawer_id}, {EXACT_WIRE_HEADER: "1"}
    )
    assert status == 200
    assert json.loads(_text(raw))["content"] == STRAY_TEXT


def test_hub_client_round_trips_a_stray_byte_exactly(stray_server):
    from mempalace.hub_client import HubClient

    port, drawer_id = stray_server
    hub = HubClient("/p", {"host": "127.0.0.1", "port": port})
    response = hub.call_tool("mempalace_get_drawer", {"drawer_id": drawer_id})
    assert json.loads(response["result"]["content"][0]["text"])["content"] == STRAY_TEXT


def test_get_drawer_returns_the_exact_source_bytes_on_request(stray_server):
    port, drawer_id = stray_server
    status, raw = _call(
        port, "mempalace_get_drawer", {"drawer_id": drawer_id, "include_source_bytes": True}
    )
    assert status == 200
    payload = json.loads(_text(raw))
    assert payload["metadata"]["source_encoding"] == "utf-8+surrogateescape"
    assert base64.b64decode(payload["source_bytes_base64"]) == STRAY_BYTES

    status, raw = _call(port, "mempalace_get_drawer", {"drawer_id": drawer_id})
    assert "source_bytes_base64" not in json.loads(_text(raw))


def test_sync_routes_carry_a_stray_byte_exactly(monkeypatch, stray_server):
    from mempalace import mcp_server

    port, _ = stray_server
    monkeypatch.setattr(mcp_server, "_mesh_peers_payload", lambda: {"note": "a\udcffb"})
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        conn.request("GET", "/sync/peers")
        raw = conn.getresponse().read()
    finally:
        conn.close()
    assert json.loads(raw) == {"note": "a\udcffb"}


def test_update_drawer_keeps_the_stray_byte_in_the_revision(monkeypatch, config, kg, stray_palace):
    from mempalace import mcp_server
    from mempalace.palace import get_collection
    from mempalace.revisions import load_drawer_revisions

    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
    drawer_id = get_collection(stray_palace).get()["ids"][0]

    result = mcp_server.tool_update_drawer(drawer_id, content="rewritten notes")
    assert result.get("success"), result
    (revision,) = load_drawer_revisions(stray_palace, drawer_id)
    assert revision["content"] == STRAY_TEXT


def test_update_drawer_drops_the_source_stamps_with_new_content(
    monkeypatch, tmp_path, config, kg, palace_path
):
    """Text the caller writes did not come from the source file, so the
    drawer stops claiming the file's encoding and parse steps."""
    from mempalace import mcp_server, miner
    from mempalace.palace import get_collection

    src = tmp_path / "src"
    src.mkdir()
    (src / "n.txt").write_bytes(b"\x93quoted\x94 notes " * 12)
    miner.mine(str(src), palace_path, wing_override="proj", agent="t")
    monkeypatch.setattr(mcp_server, "_config", config)
    monkeypatch.setattr(mcp_server, "_get_kg", lambda: kg)
    col = get_collection(palace_path)
    drawer_id = col.get()["ids"][0]
    assert col.get(ids=[drawer_id])["metadatas"][0]["source_encoding"] == "windows-1252"

    result = mcp_server.tool_update_drawer(drawer_id, content="new text ✨ from the user")
    assert result.get("success"), result

    payload = mcp_server.tool_get_drawer(drawer_id, include_source_bytes=True)
    assert payload["content"] == "new text ✨ from the user"
    assert "source_encoding" not in payload["metadata"]
    assert "source_bytes_base64" not in payload and "error" not in payload

    # A metadata-only update keeps a stamp that still describes the text.
    (src / "m.txt").write_bytes(b"\x93kept\x94 notes " * 12)
    miner.mine(str(src), palace_path, wing_override="proj", agent="t")
    rows = get_collection(palace_path).get()
    other = next(
        drawer for drawer, meta in zip(rows["ids"], rows["metadatas"]) if "source_encoding" in meta
    )
    assert mcp_server.tool_update_drawer(other, room="moved").get("success")
    meta = get_collection(palace_path).get(ids=[other])["metadatas"][0]
    assert meta["source_encoding"] == "windows-1252"


def test_source_bytes_never_replace_the_drawer_with_an_error():
    from mempalace import mcp_server

    payload = {"content": "emoji ✨", "metadata": {"source_encoding": "windows-1252"}}
    out = mcp_server._with_source_bytes(dict(payload))
    assert out["content"] == "emoji ✨"
    assert "source_bytes_base64" not in out
    assert "windows-1252" in out["source_bytes_note"]


def test_dropping_source_stamps_matches_how_the_backend_writes_metadata():
    from mempalace import mcp_server

    class Merging:
        upsert_merges_metadata = True

    meta = {"source_encoding": "windows-1252", "transforms": "x", "wing": "w"}
    mcp_server._drop_source_read_stamps(Merging(), meta)
    assert meta == {"source_encoding": None, "transforms": None, "wing": "w"}

    meta = {"source_encoding": "windows-1252", "wing": "w"}
    mcp_server._drop_source_read_stamps(object(), meta)
    assert meta == {"wing": "w"}
