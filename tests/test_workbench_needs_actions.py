"""Needs me keeps every queued action reachable: direct plan review, Accept and stale-card recovery."""

import subprocess
from pathlib import Path

import pytest

from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_approval_routing import home
from tests.test_commonplace_board_authority import fresh as fresh
from tests.test_commonplace_boards import api

TESTS = Path(__file__).parent


@pytest.mark.parametrize(
    "case",
    [
        "plan_direct",
        "proposal_reject_only",
        "plan_reject_only",
        "shell_actions",
        "answer_error_stale",
        "answer_error_same_card",
    ],
)
def test_needs_detail_actions(case):
    result = subprocess.run(
        ["node", str(TESTS / "workbench_needs.mjs"), case],
        capture_output=True,
        text=True,
        cwd=TESTS.parent,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr  # RED


def test_done_review_offers_accept(hub, fresh):
    # An explicit owner import stands in for a finished card that waits for the owner's review.
    card = fresh.owner.seed_card(
        board="work", title="Synthetic finished work", body="Done?", status="review"
    )
    code, report = api(hub, fresh.cookie, "GET", "/api/needs-me")
    assert code == 200, report
    item = next(c for c in report["done_review"] if c["number"] == card["number"])
    assert item["actions"] == ["accept", "answer"]
    html = home(fresh, card)
    accept = (
        f'data-action="need-accept" data-card="{card["number"]}" data-version="{item["version"]}"'
    )
    assert accept in html, "Accept missing from the Needs me detail"  # RED
