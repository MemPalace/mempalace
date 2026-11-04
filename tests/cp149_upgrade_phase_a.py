"""Create synthetic pre-upgrade snapshots, parts and a legacy capture."""

import json
import os
from pathlib import Path

from commonplace.handoff.transcripts import parse
from tests.commonplace_sync_fixtures import (
    accepted,
    actors,
    appended,
    hub as hub,
    item,
    parts,
    raw,
    records,
    reset_reached as reset_reached,
    sha,
)


def test_prepare_before_upgrade(hub):
    directory = Path(os.environ["CP149_UPGRADE_DIR"])
    directory.mkdir(parents=True, exist_ok=True)
    h = hub(root=directory / "hub")
    p, capture, grant = actors(h)
    F, K = records("F"), records("K")
    accepted(h.imp("claude-code", [item("F", raw(F))], token=grant))
    submissions = parts("K", raw(K), len(raw(K[:1])))
    h.at(901)
    accepted(h.imp("claude-code", [submissions[0]], token=grant))
    h.at(1802)
    accepted(h.imp("claude-code", [submissions[1]], token=grant))
    h.at(2703)
    captured = appended(h.push(capture, "claude-code", "F", raw(F)))
    listing = h.imports(p)
    assert {r["thread_id"]: r["stored_messages"] for r in listing} == {
        "F": len(parse(raw(F).decode())),
        "K": len(parse(raw(K).decode())) + 3,
    }
    hashes = {}
    for tid in [h.import_thread(p, "F"), h.import_thread(p, "K"), captured["stored_thread_id"]]:
        rows = h.message_rows(p, tid)
        hashes[tid] = [len(rows), sha(json.dumps(rows, sort_keys=True).encode())]
    assert len(hashes) == 3
    fields = ("stored_thread_id", "source", "thread_id", "conflict_of", "stored_messages")
    metadata = {
        "final_clock": 2703,
        "email": p.email,
        "len_F": len(raw(F)),
        "sha_F": sha(raw(F)),
        "capture_thread": captured["stored_thread_id"],
        "row_hashes": hashes,
        "imports": [{key: row[key] for key in fields} for row in listing],
    }
    (directory / "phase-a.json").write_text(json.dumps(metadata, sort_keys=True) + "\n")
    token_path = directory / "phase-a.capture-token"
    fd = os.open(token_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(capture)
