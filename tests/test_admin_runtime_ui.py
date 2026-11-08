"""The runtime and owner queue panels use the existing bounded Admin lifecycle."""

import shutil
import subprocess
from pathlib import Path

from tests._commonplace_helpers import hub_fixture as hub_fixture
from tests.test_commonplace_web import request


def run(hub, case):
    status, _, body = request(hub, "/app/admin.js")
    assert status == 200
    node = shutil.which("node")
    if not node:
        raise RuntimeError("Node required")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("admin_runtime_ui.mjs")), case],
        input=body.decode(),
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode not in (0, 42):
        raise RuntimeError(result.stderr[-3000:])
    assert result.returncode == 0, result.stderr


def test_needs_me_projection(hub):
    run(hub, "queue")


def test_partial_sources_pagination_and_get_only(hub):
    run(hub, "partial")


def test_runtime_labels_explain_connection_and_activity(hub):
    run(hub, "labels")


def test_next_page_source_error_keeps_rows_and_retry_cursor(hub):
    run(hub, "page-error")
