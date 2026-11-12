"""End-to-end PUBLIC reader recovery after a real interrupted conversation process.

This tests a writer killed after one persisted new chunk and a *fresh* reader
process. It does not assert atomic snapshots under concurrent publication.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from mempalace.convo_miner import mine_convos


def _write_session(path, label):
    turns = []
    for index in range(3):
        turns.append(
            f"> {label} question {index} "
            + "asked " * 30
            + "\n"
            + f"{label} answer {index} "
            + "verbatim " * 45
        )
    path.write_text("\n\n".join(turns) + "\n", encoding="utf-8")


def _child(code, *args):
    return subprocess.run(
        [sys.executable, "-c", code, *map(str, args)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=150,
    )


def test_killed_writer_does_not_expose_uncommitted_generation_to_fresh_public_reader(tmp_path):
    source_dir = tmp_path / "sessions"
    source_dir.mkdir()
    session = source_dir / "session.txt"
    palace_path = tmp_path / "palace"

    _write_session(session, "A")
    mine_convos(str(source_dir), str(palace_path), wing="sessions", extract_mode="exchange")

    _write_session(session, "B")
    stat = session.stat()
    os.utime(session, (stat.st_atime, stat.st_mtime + 10))

    # Terminate the entire process *after* Chroma commits its first current
    # upsert. Neither the remaining new chunks nor metadata finalization run.
    writer = """
import os
import sys
import mempalace.palace as palace
from mempalace.convo_miner import mine_convos
from mempalace.palace import get_collection

source_dir, palace_path = sys.argv[1:3]
palace.DRAWER_UPSERT_BATCH_SIZE = 1
collection = get_collection(palace_path)
original_upsert = type(collection).upsert

def terminate_after_first_current(self, *args, **kwargs):
    ids = kwargs.get("ids") or []
    if ids and not all("_hist_" in row_id for row_id in ids):
        original_upsert(self, *args, **kwargs)
        os._exit(73)
    return original_upsert(self, *args, **kwargs)

type(collection).upsert = terminate_after_first_current
mine_convos(source_dir, palace_path, wing="sessions", extract_mode="exchange")
sys.exit(74)  # Injection never fired.
"""
    killed = _child(writer, source_dir, palace_path)
    assert killed.returncode == 73, (
        f"writer did not terminate in the injected crash window: "
        f"{killed.returncode}\n{killed.stdout}\n{killed.stderr}"
    )

    # All public APIs are called by a new interpreter, with no inherited
    # in-memory Chroma client or cached read metadata from the writer.
    reader = """
import json
import sys
from mempalace import mcp_server
from mempalace.config import MempalaceConfig
from mempalace.palace import get_collection
from mempalace.searcher import search_memories

source_file, palace_path = sys.argv[1:3]
mcp_server._config = MempalaceConfig(palace_path=palace_path)
mcp_server._get_collection = lambda *args, **kwargs: get_collection(palace_path)
collection = get_collection(palace_path)
raw = collection.get(where={"source_file": source_file}, include=["documents", "metadatas"])
unfinished = [
    row_id
    for row_id, doc, meta in zip(raw["ids"], raw["documents"], raw["metadatas"])
    if "B question" in doc and not (meta or {}).get("historical_version")
]
listing = mcp_server.tool_list_drawers(limit=100)
bulk = mcp_server.tool_get_drawers(unfinished)
a_search = search_memories("A question", palace_path, source_file=source_file, n_results=3)
b_search = search_memories("B question", palace_path, source_file=source_file, n_results=1)
print("__CRASH_READER__" + json.dumps({
    "uncommitted_ids": unfinished,
    "single": [mcp_server.tool_get_drawer(row_id) for row_id in unfinished],
    "bulk": bulk,
    "listing": listing,
    "a_search": a_search,
    "b_search": b_search,
}))
"""
    checked = _child(reader, session, palace_path)
    assert checked.returncode == 0, checked.stdout + "\n" + checked.stderr
    marker = "__CRASH_READER__"
    assert marker in checked.stdout, checked.stdout
    snapshot = json.loads(checked.stdout.split(marker, 1)[1].splitlines()[0])

    unpublished = snapshot["uncommitted_ids"]
    assert unpublished, "the killed writer never persisted an uncommitted B chunk"
    assert all("error" in item for item in snapshot["single"])
    assert snapshot["bulk"]["errors"] == len(unpublished)
    assert all("error" in item for item in snapshot["bulk"]["results"])

    listing = snapshot["listing"]
    assert "error" not in listing, listing
    previews = [
        row.get("content_preview") or ""
        for row in listing["drawers"]
        if (row.get("metadata") or {}).get("source_file") == session.name
    ]
    assert len(previews) >= 3, "the durable A history disappeared after the crash"
    assert all("A question" in text for text in previews)
    for key in ("a_search", "b_search"):
        assert "error" not in snapshot[key], snapshot[key]
    assert any("A question" in row["text"] for row in snapshot["a_search"]["results"])
    assert all("B question" not in row["text"] for row in snapshot["b_search"]["results"])
