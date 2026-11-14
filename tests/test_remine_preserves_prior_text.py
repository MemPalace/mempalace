"""A re-mine, a sync or another miner never destroys conversation or diary text.

Conversations, diaries and manual entries are memory the palace is the only
copy of. Before a re-mine overwrites or drops such a drawer, its exact text is
journaled to ``drawer_revisions.jsonl``; a miner prunes only the drawers it
filed itself, never another producer's drawer stored under the same path; and
``sync --apply`` keeps preserved drawers whose source is gone or ignored.
Project-file drawers index the file as it is now and are still replaced.
"""

import os
from pathlib import Path

import chromadb
import pytest

from mempalace import retention
from mempalace.palace import (
    PriorTextArchiveFailed,
    StaleDrawerPurgeFailed,
    get_collection,
    write_file_drawers,
)
from mempalace.revisions import append_drawer_revisions, load_drawer_revisions


def _touch(path):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def _rows(palace, source_file):
    got = get_collection(palace).get(
        where={"source_file": str(source_file)}, include=["documents", "metadatas"]
    )
    return list(zip(got["ids"], got["documents"], got["metadatas"]))


def _turns(*answers):
    return (
        "\n\n".join(f"> question {i} " + "asked " * 30 + "\n" + a for i, a in enumerate(answers))
        + "\n"
    )


def _answer(word):
    return f"{word} " * 60


class _Convo:
    def __init__(self, tmp_path, name="chat.txt"):
        from mempalace import convo_miner

        self.convo_miner = convo_miner
        self.src = (tmp_path / "chats").resolve()
        self.src.mkdir(exist_ok=True)
        self.doc = self.src / name
        self.palace = str(tmp_path / "palace")

    def write(self, *answers):
        self.doc.write_text(_turns(*answers), encoding="utf-8")

    def mine(self):
        self.convo_miner.mine_convos(str(self.src), self.palace, wing="chats")

    def rows(self):
        return [r for r in _rows(self.palace, self.doc) if r[2].get("ingest_mode") == "convos"]


# -- classification -------------------------------------------------------------


@pytest.mark.parametrize(
    "meta, producer, klass",
    [
        ({"ingest_mode": "convos", "extract_mode": "exchange"}, "convo", retention.PRESERVE),
        ({"ingest_mode": "registry"}, "convo_registry", None),
        ({"ingest_mode": "sweep"}, "sweep", retention.PRESERVE),
        ({"ingest_mode": "extract", "extract_mode": "format"}, "format", retention.INDEX_CURRENT),
        ({"source_session": "daily_diary", "chunk_total": 3}, "diary", retention.PRESERVE),
        ({"type": "diary_entry"}, "diary_write", retention.PRESERVE),
        ({"adapter_name": "x"}, "adapter", None),
        ({"normalize_version": 2, "id_recipe": "v3"}, "project", retention.INDEX_CURRENT),
        ({"chunk_total": 4}, "project", retention.INDEX_CURRENT),
        ({"id_recipe": "v3", "added_by": "mcp"}, "manual", retention.PRESERVE),
        ({"added_by": "miner"}, None, None),
        ({"ingest_mode": "something-new"}, "other", None),
    ],
)
def test_each_producer_is_recognised_from_the_metadata_it_writes(meta, producer, klass):
    assert retention.drawer_producer(meta) == producer
    assert retention.retention_class(meta) == klass


# -- conversations ------------------------------------------------------------------


def test_a_rewritten_transcript_journals_every_replaced_and_dropped_text(tmp_path):
    convo = _Convo(tmp_path)
    convo.write(_answer("first"), _answer("second"), _answer("third"))
    convo.mine()
    before = {drawer_id: doc for drawer_id, doc, _ in convo.rows()}
    assert len(before) == 3

    convo.write(_answer("rewritten"))
    _touch(convo.doc)
    convo.mine()

    after = convo.rows()
    assert len(after) == 1 and "rewritten" in after[0][1]
    journal = load_drawer_revisions(convo.palace)
    assert {r["drawer_id"]: r["content"] for r in journal} == before
    reasons = {r["drawer_id"]: r["metadata"]["revision_reason"] for r in journal}
    assert sorted(reasons.values()) == ["remine_overwrite", "remine_prune", "remine_prune"]
    assert all(r["metadata"]["source_file"] == str(convo.doc) for r in journal)


def test_an_unchanged_transcript_journals_nothing(tmp_path):
    convo = _Convo(tmp_path)
    convo.write(_answer("same"), _answer("words"))
    convo.mine()
    _touch(convo.doc)
    convo.mine()

    assert len(convo.rows()) == 2
    assert load_drawer_revisions(convo.palace) == []


def test_a_grown_transcript_journals_only_the_text_it_replaced(tmp_path):
    convo = _Convo(tmp_path)
    convo.write(_answer("one"))
    convo.mine()
    convo.write(_answer("one"), _answer("two"))
    _touch(convo.doc)
    convo.mine()

    assert len(convo.rows()) == 2
    assert load_drawer_revisions(convo.palace) == []


def test_a_failed_journal_append_replaces_nothing_and_the_next_mine_converges(
    tmp_path, monkeypatch
):
    from mempalace import revisions
    from mempalace.palace import MineFileErrors

    convo = _Convo(tmp_path)
    convo.write(_answer("kept"), _answer("words"))
    convo.mine()
    before = sorted(doc for _, doc, _ in convo.rows())

    def full_disk(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(revisions, "append_drawer_revisions", full_disk)
    convo.write(_answer("replacement"))
    _touch(convo.doc)
    with pytest.raises(MineFileErrors):
        convo.mine()
    assert sorted(doc for _, doc, _ in convo.rows()) == before

    monkeypatch.undo()
    convo.mine()
    assert all("replacement" in doc for _, doc, _ in convo.rows())
    assert sorted(r["content"] for r in load_drawer_revisions(convo.palace)) == before


def test_a_preserving_refile_without_a_palace_path_refuses_to_overwrite(tmp_path):
    palace = str(tmp_path / "palace")
    col = get_collection(palace)
    meta = {
        "source_file": "chat.txt",
        "wing": "chats",
        "chunk_index": 0,
        "ingest_mode": "convos",
        "extract_mode": "exchange",
    }
    write_file_drawers(
        col,
        "chat.txt",
        [("c0", "original words", meta)],
        owner="convo",
        extract_mode="exchange",
        palace_path=palace,
    )

    with pytest.raises(PriorTextArchiveFailed) as raised:
        write_file_drawers(
            col, "chat.txt", [("c0", "new words", meta)], owner="convo", extract_mode="exchange"
        )
    assert isinstance(raised.value, StaleDrawerPurgeFailed)
    assert col.get(ids=["c0"])["documents"] == ["original words"]


def test_an_unknown_owner_is_refused(tmp_path):
    col = get_collection(str(tmp_path / "palace"))
    with pytest.raises(ValueError):
        write_file_drawers(col, "x.md", [], owner="nobody")


# -- one path, several producers --------------------------------------------------------


def test_a_project_mine_over_a_transcript_keeps_its_conversation_drawers(tmp_path):
    from mempalace import miner

    convo = _Convo(tmp_path, name="session.md")
    convo.write(_answer("decided"), _answer("because"))
    convo.mine()
    conversation = sorted(doc for _, doc, _ in convo.rows())

    _touch(convo.doc)
    miner.mine(str(convo.src), convo.palace, wing_override="chats", agent="test")
    convo.doc.write_text(_turns(_answer("decided")), encoding="utf-8")
    _touch(convo.doc)
    miner.mine(str(convo.src), convo.palace, wing_override="chats", agent="test")

    assert sorted(doc for _, doc, _ in convo.rows()) == conversation
    project = [r for r in _rows(convo.palace, convo.doc) if "ingest_mode" not in r[2]]
    assert project, "the project miner indexed the file as well"
    assert all("because" not in doc for _, doc, _ in project)


def test_a_conversation_remine_keeps_the_project_index_of_the_same_path(tmp_path):
    from mempalace import miner

    convo = _Convo(tmp_path, name="session.md")
    convo.write(_answer("alpha"), _answer("beta"))
    miner.mine(str(convo.src), convo.palace, wing_override="chats", agent="test")
    project = sorted(r[0] for r in _rows(convo.palace, convo.doc))
    assert project

    convo.mine()
    _touch(convo.doc)
    convo.mine()

    ids = {r[0] for r in _rows(convo.palace, convo.doc)}
    assert set(project) <= ids


def test_conversation_drawers_are_not_proof_the_project_index_is_current(tmp_path):
    """The project miner re-indexes an edited file even when a convo mine got there first."""
    from mempalace import miner

    convo = _Convo(tmp_path, name="session.md")
    convo.write(_answer("alpha"), _answer("beta"))
    miner.mine(str(convo.src), convo.palace, wing_override="chats", agent="test")

    convo.write(_answer("gamma"))
    _touch(convo.doc)
    convo.mine()
    miner.mine(str(convo.src), convo.palace, wing_override="chats", agent="test")

    project = [doc for _, doc, meta in _rows(convo.palace, convo.doc) if "ingest_mode" not in meta]
    assert project and all("gamma" in doc for doc in project)


def test_a_project_drawer_from_before_normalize_version_is_still_replaced(tmp_path):
    from mempalace import miner

    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_text("Current notes. " + "verbatim words " * 40, encoding="utf-8")
    palace = str(tmp_path / "palace")
    miner.mine(str(src), palace, wing_override="proj", agent="test")
    get_collection(palace).upsert(
        ids=["drawer_legacy_v1"],
        documents=["OLD STALE TEXT"],
        metadatas=[
            {
                "wing": "proj",
                "room": "general",
                "source_file": str(doc),
                "chunk_index": 0,
                "added_by": "mempalace",
                "filed_at": "2026-04-01T00:00:00",
                "source_mtime": 1.0,
            }
        ],
    )

    doc.write_text("Edited notes. " + "verbatim words " * 40, encoding="utf-8")
    _touch(doc)
    miner.mine(str(src), palace, wing_override="proj", agent="test")

    assert "OLD STALE TEXT" not in [d for _, d, _ in _rows(palace, doc)]


def test_deleting_the_current_drawer_keeps_remine_history_of_its_slot(tmp_path):
    """A re-mine revision belongs to the source, not to the drawer now at that id."""
    from mempalace.revisions import purge_drawer_revisions

    palace = str(tmp_path)
    append_drawer_revisions(
        palace,
        [
            (
                "slot3",
                "the exchange /compact replaced",
                {"source_file": "s.jsonl", "revision_reason": "remine_overwrite"},
            ),
            ("slot3", "a manual edit's prior text", {"source_file": "s.jsonl"}),
        ],
    )

    assert purge_drawer_revisions(palace, drawer_ids=["slot3"]) == 1
    assert [r["content"] for r in load_drawer_revisions(palace)] == [
        "the exchange /compact replaced"
    ]
    assert purge_drawer_revisions(palace, source_file="s.jsonl") == 1
    assert load_drawer_revisions(palace) == []


# -- diaries ------------------------------------------------------------------------------


def _diary_entry(n, body):
    return f"## 10:{n:02d} — Entry {n}\n\n{body}\n"


def test_a_rewritten_diary_journals_the_entries_it_replaced_and_dropped(tmp_path):
    from mempalace.diary_ingest import ingest_diaries

    diary_dir = tmp_path / "diaries"
    diary_dir.mkdir()
    palace = str(tmp_path / "palace")
    path = diary_dir / "2026-04-13.md"
    first = "Entry one says the launch moved to Friday."
    second = "Entry two records who agreed to it."
    path.write_text(
        "# 2026-04-13\n\n" + _diary_entry(1, first) + "\n" + _diary_entry(2, second),
        encoding="utf-8",
    )
    ingest_diaries(str(diary_dir), palace)

    path.write_text(
        "# 2026-04-13\n\n" + _diary_entry(1, "Entry one, edited later."), encoding="utf-8"
    )
    ingest_diaries(str(diary_dir), palace)

    current = [doc for _, doc, _ in _rows(palace, path)]
    assert len(current) == 1 and "edited later" in current[0]
    journaled = " ".join(r["content"] for r in load_drawer_revisions(palace))
    assert first in journaled and second in journaled


# -- sync ---------------------------------------------------------------------------------


def test_sync_apply_keeps_conversation_memory_whose_transcript_is_gone(tmp_path):
    from mempalace.sync import sync_palace

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text("*.log\n")
    (repo / "keep.md").write_text("still here\n")
    (repo / "ignored.log").write_text("ignored\n")
    gone = repo / "gone.md"
    palace = str(tmp_path / "palace")

    def meta(path, **extra):
        return {"wing": "w", "room": "r", "source_file": str(path), "chunk_index": 0, **extra}

    project = {"normalize_version": 2, "chunk_total": 1}
    conversation = {"ingest_mode": "convos", "extract_mode": "exchange"}
    client = chromadb.PersistentClient(path=palace)
    col = client.get_or_create_collection("mempalace_drawers", metadata={"hnsw:space": "cosine"})
    col.add(
        ids=["keep_index", "gone_index", "gone_chat", "ignored_index", "ignored_chat"],
        documents=[f"doc {i}" for i in range(5)],
        embeddings=[[float(i + 1), 0.0, 0.0] for i in range(5)],
        metadatas=[
            meta(repo / "keep.md", **project),
            meta(gone, **project),
            meta(gone, **conversation),
            meta(repo / "ignored.log", **project),
            meta(repo / "ignored.log", **conversation),
        ],
    )
    del col, client

    report = sync_palace(palace_path=palace, project_dirs=[str(repo)], dry_run=False)

    assert report["preserved"] == 2
    assert report["preserved_by_source"] == {str(gone): 1, str(repo / "ignored.log"): 1}
    assert report["missing"] == 1 and report["gitignored"] == 1
    client = chromadb.PersistentClient(path=palace)
    col = client.get_collection("mempalace_drawers")
    assert set(col.get(include=[])["ids"]) == {"keep_index", "gone_chat", "ignored_chat"}
    del col, client


# -- the journal ----------------------------------------------------------------------------


def test_a_batch_of_revisions_is_one_append_and_one_fsync(tmp_path, monkeypatch):
    calls = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real_fsync(fd)))

    ids = append_drawer_revisions(
        str(tmp_path), [("a", "first text", {"k": 1}), ("b", "second\x00text", None)]
    )

    assert len(ids) == 2 and len(set(ids)) == 2
    assert len(calls) == 1
    rows = load_drawer_revisions(str(tmp_path))
    assert [(r["drawer_id"], r["content"]) for r in rows] == [
        ("a", "first text"),
        ("b", "second\x00text"),
    ]
    assert Path(tmp_path, "drawer_revisions.jsonl").read_bytes().count(b"\n") == 2


def test_an_empty_batch_writes_nothing(tmp_path):
    assert append_drawer_revisions(str(tmp_path / "palace"), []) == []
    assert not (tmp_path / "palace").exists()
