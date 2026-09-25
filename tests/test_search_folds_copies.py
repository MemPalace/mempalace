"""One passage copied into several files is one search result, not several.

Backups, autosaves, and re-exports put the same drawer text under different
source files. Each copy used to take a result slot of its own.
"""

from mempalace.searcher import _fold_copies_across_sources, search_memories


def _source(hit):
    return hit.get("source_path")


def _ref(hit):
    return {"drawer_id": hit["drawer_id"], "source_path": hit["source_path"]}


def test_fold_keeps_the_first_copy_and_lists_the_others():
    hits = [
        {"drawer_id": "a", "source_path": "/orig/chat.md", "text": "same passage"},
        {"drawer_id": "b", "source_path": "/backup/chat.md", "text": "same passage"},
        {"drawer_id": "c", "source_path": "/other/notes.md", "text": "distinct passage"},
        {"drawer_id": "d", "source_path": "/autosave/chat.md", "text": "same passage"},
    ]
    folded = _fold_copies_across_sources(hits, _source, _ref)
    assert [h["drawer_id"] for h in folded] == ["a", "c"]
    assert folded[0]["also_in"] == [
        {"drawer_id": "b", "source_path": "/backup/chat.md"},
        {"drawer_id": "d", "source_path": "/autosave/chat.md"},
    ]
    assert "also_in" not in folded[1]


def test_fold_leaves_repeats_within_one_file_as_separate_hits():
    hits = [
        {"drawer_id": "a", "source_path": "/chat.md", "text": "ok, ship it"},
        {"drawer_id": "b", "source_path": "/chat.md", "text": "ok, ship it"},
        {"drawer_id": "c", "source_path": None, "text": "ok, ship it"},
    ]
    assert _fold_copies_across_sources(hits, _source, _ref) == hits


def test_search_returns_distinct_passages_and_names_the_copies(palace_path, collection):
    copy_text = "The lantern stays lit on the third floor until the last guest leaves."
    collection.add(
        ids=["orig", "backup", "autosave", "other1", "other2"],
        documents=[
            copy_text,
            copy_text,
            copy_text,
            "The lantern needs new oil every week before the guests arrive.",
            "Guests on the third floor asked about the lantern schedule.",
        ],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "/orig/log.md"},
            {"wing": "w", "room": "r", "source_file": "/backup/log.md"},
            {"wing": "w", "room": "r", "source_file": "/autosave/log.md"},
            {"wing": "w", "room": "r", "source_file": "/notes/oil.md"},
            {"wing": "w", "room": "r", "source_file": "/notes/guests.md"},
        ],
    )
    out = search_memories("lantern third floor guests", palace_path, n_results=3)
    texts = [hit["text"] for hit in out["results"]]
    assert len(texts) == 3 and len(set(texts)) == 3, texts
    copy_hit = next(hit for hit in out["results"] if hit["text"] == copy_text)
    assert sorted(ref["source_path"] for ref in copy_hit["also_in"]) == sorted(
        {"/orig/log.md", "/backup/log.md", "/autosave/log.md"} - {copy_hit["source_path"]}
    )


def test_cli_search_prints_the_other_copies(palace_path, collection, capsys):
    from mempalace.searcher import search

    text = "Checkpoint the palace before every migration and keep the backup."
    collection.add(
        ids=["orig", "backup"],
        documents=[text, text],
        metadatas=[
            {"wing": "w", "room": "r", "source_file": "/orig/migrations.md"},
            {"wing": "w", "room": "r", "source_file": "/backup/migrations.md"},
        ],
    )
    search("checkpoint before migration", palace_path, n_results=5)
    out = capsys.readouterr().out
    assert out.count(text) == 1
    assert "Also in: migrations.md" in out
