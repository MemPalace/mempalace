"""Browser-free chat state and device seen-id contracts."""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    "none",
    "waiting",
    "working",
    "deadline",
    "no_answer",
    "acknowledged",
    "older_agent",
    "quick_reply",
    "non_work",
    "latest_request",
    "private_status",
    "people_unread",
    "agents_unread",
    "answers_unread",
    "handoffs_unread",
    "receipts_unread",
    "seen_boundaries",
    "duplicates",
    "baseline",
    "baseline_cursors",
    "ids_only",
    "reload",
    "isolation",
    "clear_board",
    "clear_user",
    "storage_denied",
    "malformed_store",
    "pure",
]


@pytest.mark.parametrize("case", CASES)
def test_chatstate_contract(case):
    module = ROOT / "commonplace/web/static/chatstate.js"
    assert module.is_file(), "Chat state module missing"  # RED
    result = subprocess.run(
        ["node", str(Path(__file__).with_name("workbench_chatstate.mjs")), case],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_g01_backend_attention_contract():
    from commonplace.chat.types import ACK_WAIT_S, HANDOFF_CODES

    assert ACK_WAIT_S == 300
    assert set(HANDOFF_CODES) == {"filed", "file_failed", "claimed", "no_access"}


def test_g02_reply_page_is_person_scoped():
    source = (ROOT / "commonplace/chat/routes.py").read_text()
    assert "for_user=principal.user_id" in source
    assert 'payload["me"] = {"user": principal.user_id' in source
