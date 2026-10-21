"""Malformed recovery journals must never mutate Palace state."""

import json

from test_rooms_pending_snapshot_contract import _resume, _seed


def test_duplicate_journal_members_rejected_before_mutation(tmp_path):
    marker = _seed(tmp_path)
    data = json.loads(marker.read_text())
    data.pop("closet_moves")
    raw = (
        json.dumps(data)[:-1]
        + ', "closet_moves": {"a": ["source", "general", "technical"], "a": ["source", "technical", "releases"], "b": ["source", "technical", "releases"]}}'
    )
    marker.write_text(raw)
    outcome = _resume(tmp_path)
    assert outcome["error"] is not None, outcome
    assert outcome["after"] == outcome["before"]
    assert marker.read_text() == raw
