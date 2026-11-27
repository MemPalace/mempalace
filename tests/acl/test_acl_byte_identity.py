"""With no grants file the HTTP bytes equal the ones captured before the ACL work.

golden_flow.json was captured from upstream/develop before any ACL change. When an
unrelated upstream change alters a tool schema, regenerate it from a checkout that
has no ACL code: ``ACL_CAPTURE=1 pytest tests/acl/test_acl_byte_identity.py``.
"""

import json
import os
import threading

import pytest
from _acl_golden import TOKEN, run_flow
from _mcp_server_helpers import _patch_mcp_server

from mempalace import mcp_server as mcp

GOLDEN = os.environ.get("ACL_GOLDEN_FILE") or os.path.join(
    os.path.dirname(__file__), "golden_flow.json"
)
pytestmark = pytest.mark.acl_integration


@pytest.fixture
def served(monkeypatch, config, kg):
    _patch_mcp_server(monkeypatch, config, kg)
    monkeypatch.setenv("MEMPALACE_MCP_HTTP_TOKEN", TOKEN)
    httpd = mcp._build_http_server("127.0.0.1", 0)
    thread = threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_acl_off_bytes_match_golden(served):
    got = run_flow(served)
    if os.environ.get("ACL_CAPTURE") == "1":
        with open(GOLDEN, "w") as fh:
            json.dump(got, fh, indent=1, sort_keys=True)
        return
    with open(GOLDEN) as fh:
        want = json.load(fh)
    assert got == want
