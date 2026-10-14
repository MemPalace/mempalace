import http.client
import json
import threading
import pytest
from mempalace import mcp_server as mcp


@pytest.fixture
def http_server():
    server = mcp._build_http_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)


@pytest.mark.parametrize(
    "name",
    [
        [],
        {},
        ["mempalace_status"],
        {"tool": "mempalace_status"},
        None,
        42,
        False,
        "",
        " \n\t",
        "\x00",
        "../../etc/passwd",
    ],
)
def test_http_invalid_name_has_same_jsonrpc_response_as_core(http_server, name):
    request = {
        "jsonrpc": "2.0",
        "id": 91,
        "method": "tools/call",
        "params": {"name": name, "arguments": {}},
    }
    expected = mcp.handle_request(request)
    assert expected["error"]["code"] in (-32602, -32601)
    conn = http.client.HTTPConnection("127.0.0.1", http_server, timeout=3)
    try:
        conn.request(
            "POST", "/mcp", json.dumps(request), headers={"Content-Type": "application/json"}
        )
        response = conn.getresponse()
        actual = json.loads(response.read())
    finally:
        conn.close()
    assert response.status == 200
    assert actual == expected
