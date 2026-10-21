"""Integration tests for the authored_at backfill migration (scripts/)."""

import importlib.util
import uuid
from pathlib import Path

import chromadb

# The migration ships as a script, not a package module; load it directly.
_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_authored_at.py"
_spec = importlib.util.spec_from_file_location("backfill_authored_at", _SCRIPT)
backfill_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill_mod)


def _collection():
    # Unique name per call: EphemeralClient shares one in-memory instance across the
    # process, so a fixed collection name would leak drawers between tests.
    client = chromadb.EphemeralClient()
    return client.create_collection(f"drawers_{uuid.uuid4().hex}")


def _add(col, drawer_id, source_file, authored_at=None):
    meta = {"ingest_mode": "convos", "source_file": source_file, "filed_at": "2026-06-27T00:00:00"}
    if authored_at is not None:
        meta["authored_at"] = authored_at
    col.add(ids=[drawer_id], documents=["hello"], metadatas=[meta], embeddings=[[0.1, 0.2, 0.3]])


def _transcript(dir_path, name, *timestamps):
    dir_path.mkdir(parents=True, exist_ok=True)
    f = dir_path / name
    f.write_text("".join(f'{{"timestamp": "{ts}"}}\n' for ts in timestamps))
    return f


def test_backfill_sets_latest_timestamp(tmp_path):
    sessions = tmp_path / "claude"
    _transcript(sessions, "abc.jsonl", "2026-06-10T08:00:00.000Z", "2026-06-12T09:00:00.000Z")
    col = _collection()
    # Stored source_file uses an old mount prefix; resolution is by basename.
    _add(col, "d1", "/old/mount/abc.jsonl")

    stats = backfill_mod.backfill_authored_at(col, [str(sessions)], apply=True)

    assert stats["scanned"] == 1
    assert stats["updated"] == 1
    got = col.get(ids=["d1"], include=["metadatas"])["metadatas"][0]
    assert got["authored_at"] == "2026-06-12T09:00:00.000Z"


def test_dry_run_writes_nothing(tmp_path):
    sessions = tmp_path / "claude"
    _transcript(sessions, "abc.jsonl", "2026-06-12T09:00:00.000Z")
    col = _collection()
    _add(col, "d1", "/old/mount/abc.jsonl")

    stats = backfill_mod.backfill_authored_at(col, [str(sessions)], apply=False)

    assert stats["updated"] == 1  # would update
    assert "authored_at" not in col.get(ids=["d1"], include=["metadatas"])["metadatas"][0]


def test_idempotent_second_run_updates_nothing(tmp_path):
    sessions = tmp_path / "claude"
    _transcript(sessions, "abc.jsonl", "2026-06-12T09:00:00.000Z")
    col = _collection()
    _add(col, "d1", "/old/mount/abc.jsonl")

    backfill_mod.backfill_authored_at(col, [str(sessions)], apply=True)
    stats2 = backfill_mod.backfill_authored_at(col, [str(sessions)], apply=True)

    assert stats2["updated"] == 0


def test_unresolved_transcript_is_left_alone(tmp_path):
    sessions = tmp_path / "claude"
    sessions.mkdir()
    col = _collection()
    _add(col, "d1", "/old/mount/missing.jsonl")  # no file on disk

    stats = backfill_mod.backfill_authored_at(col, [str(sessions)], apply=True)

    assert stats["updated"] == 0
    assert stats["unresolved_files"] == 1
    assert "authored_at" not in col.get(ids=["d1"], include=["metadatas"])["metadatas"][0]
