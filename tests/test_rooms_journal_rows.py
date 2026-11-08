"""Reject duplicate and malformed closet rows before recovery writes."""

import json
from pathlib import Path

import pytest

from mempalace.config import MempalaceConfig
from mempalace.rooms import load_pending_apply, pending_apply_path


@pytest.mark.parametrize(
    "closets",
    [
        [["source", "old", "first"], ["source", "old", "second"]],
        [["source", "old", "first"], ["malformed"]],
        [["source", "old", "first"], ["source", "old", "first"]],
        None,
        {"source": "not a row list"},
    ],
)
def test_corrupt_closet_rows_refused_before_recovery(tmp_path, closets):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    journal = Path(pending_apply_path(cfg, "alpha"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps({"closets": closets, "closet_moves": {}})
    journal.write_text(raw)
    with pytest.raises(ValueError):
        load_pending_apply(cfg, "alpha")
    assert journal.read_text() == raw


def test_valid_closet_journal_still_loads(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path / "palace"))
    journal = Path(pending_apply_path(cfg, "alpha"))
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps({"closets": [["source", "old", "first"]], "closet_moves": {}}))
    assert load_pending_apply(cfg, "alpha")[0] == {("source", "old"): "first"}
