"""Upgrade proof: adoption preserves old rows and continues old stream cursors."""

import json
import os
from pathlib import Path

from tests.commonplace_sync_fixtures import (
    appended,
    counts,
    hub as hub,
    raw,
    records,
    reset_reached as reset_reached,
    sha,
)


def test_adopt_after_upgrade(hub):
    directory = Path(os.environ["CP149_UPGRADE_DIR"])
    before = json.loads((directory / "phase-a.json").read_text())
    token_path = directory / "phase-a.capture-token"
    try:
        old_token = token_path.read_text()
        h = hub(root=directory / "hub")
        h.at(before["final_clock"] + 901)
        p = h.signup(before["email"])
        token, _ = h.pat(p, client="claude-code")
        F, K = records("F"), records("K")
        G, L = records("F", 1, "g"), records("K", 1, "l")
        continued = appended(
            h.push(old_token, "claude-code", "F", raw(G), before["len_F"], before["sha_F"])
        )
        assert continued["stored_thread_id"] == before["capture_thread"]
        assert continued["offset"] == len(raw(F + G))
        cursor = h.get("/api/capture?source=claude-code&thread_id=F", old_token)
        assert cursor.status == 200
        assert cursor.json["offset"] == len(raw(F + G))
        h.at(before["final_clock"] + 911)
        new_f = appended(h.push(token, "claude-code", "F", raw(F + G)))
        h.at(before["final_clock"] + 921)
        new_k = appended(h.push(token, "claude-code", "K", raw(K + L)))
        o = h.oracle(p, F + K + G + L)
        assert counts(o, G + L + K) == [1] * len(G + L + K)
        assert counts(o, F) == [2] * len(F)
        assert h.read(p, new_f["stored_thread_id"]) == raw(F + G)
        assert h.read(p, new_k["stored_thread_id"]) == raw(K + L)
        listing_response = h.sessions(p)
        assert listing_response.status == 200
        assert len(listing_response.json["sessions"]) == 2
        assert sum(row["legacy"] for row in listing_response.json["sessions"]) == 1
        feed = h.feed(token)
        assert feed.status == 200
        assert feed.json["seq"] == 0
        assert set(feed.json["state"]) == {"capture_enabled", "interval_seconds", "next_window_at"}
        assert h.read(p, h.import_thread(p, "F")) == raw(F + G)
        assert h.read(p, h.import_thread(p, "K")) == raw(K + L)
        assert h.read(p, before["capture_thread"]) == raw(F + G)
        fields = ("stored_thread_id", "source", "thread_id", "conflict_of", "stored_messages")
        listing = [{key: row[key] for key in fields} for row in h.imports(p)]
        assert sorted(listing, key=lambda r: r["stored_thread_id"]) == sorted(
            before["imports"], key=lambda r: r["stored_thread_id"]
        )
        hashes = {}
        for tid, (n_rows, digest) in before["row_hashes"].items():
            rows = h.message_rows(p, tid)[:n_rows]
            hashes[tid] = [len(rows), sha(json.dumps(rows, sort_keys=True).encode())]
            assert hashes[tid] == [n_rows, digest]
        assert set(hashes) == set(before["row_hashes"])
        assert len(hashes) == 3
    finally:
        token_path.unlink(missing_ok=True)
