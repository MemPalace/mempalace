import threading

import pytest
from _acl_helpers import LEGACY, write_grants
from _mcp_server_helpers import _patch_mcp_server

from mempalace import acl, mcp_server as mcp


@pytest.fixture(autouse=True)
def _acl_off_after():
    yield
    acl.disable()


@pytest.fixture
def serve_with(monkeypatch, config, kg, tmp_path):
    """Start the real HTTP server with a grants file; returns (port, httpd)."""
    started = []

    def start(keys, **top):
        _patch_mcp_server(monkeypatch, config, kg)
        monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", LEGACY)
        monkeypatch.setenv("MEMPALACE_MCP_GRANTS_FILE", write_grants(tmp_path, keys, **top))
        httpd = mcp._build_http_server("127.0.0.1", 0)
        thread = threading.Thread(
            target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        started.append((httpd, thread))
        return httpd.server_address[1], httpd

    yield start
    for httpd, thread in started:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
