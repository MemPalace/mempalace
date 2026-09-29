"""One passage copied into several files is one search result, not several.

Backups, autosaves, and re-exports put the same drawer text under different
source files. Each copy used to take a result slot of its own.
"""

from mempalace.searcher import _fold_copies_across_sources, search_memories


def _source(hit):
    return hit.get("source_path")


def _ref(hit):
    return {"drawer_id": hit["drawer_id"], "source_path": hit["source_path"]}


# At least _FOLD_MIN_CHARS long: identical wording this long is a copy, not chance.
SAME = "same passage copied into several files by backups and autosaves, " * 2


def test_fold_keeps_the_first_copy_and_lists_the_others():
    hits = [
        {"drawer_id": "a", "source_path": "/orig/chat.md", "text": SAME},
        {"drawer_id": "b", "source_path": "/backup/chat.md", "text": SAME},
        {"drawer_id": "c", "source_path": "/other/notes.md", "text": "distinct passage"},
        {"drawer_id": "d", "source_path": "/autosave/chat.md", "text": SAME},
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
        {"drawer_id": "a", "source_path": "/chat.md", "text": SAME},
        {"drawer_id": "b", "source_path": "/chat.md", "text": SAME},
        {"drawer_id": "c", "source_path": None, "text": SAME},
    ]
    assert _fold_copies_across_sources(hits, _source, _ref) == hits


def test_fold_leaves_short_identical_text_alone():
    """Two unrelated sources both saying "Yes." are not copies of each other."""
    hits = [
        {"drawer_id": "a", "source_path": "/meeting-a.md", "text": "Yes."},
        {"drawer_id": "b", "source_path": "/meeting-b.md", "text": "Yes."},
    ]
    assert _fold_copies_across_sources(hits, _source, _ref) == hits


def test_fold_keeps_a_files_repeats_whatever_the_ranking_order():
    """A file that holds the passage twice shows it twice, whether its hits
    rank before or after another file's copy."""
    a = {"drawer_id": "a", "source_path": "/a.md", "text": SAME}
    b1 = {"drawer_id": "b1", "source_path": "/b.md", "text": SAME}
    b2 = {"drawer_id": "b2", "source_path": "/b.md", "text": SAME}
    for order in ([a, b1, b2], [b1, a, b2], [b1, b2, a]):
        hits = [dict(h) for h in order]
        folded = _fold_copies_across_sources(hits, _source, _ref)
        assert len(folded) == 2, [h["drawer_id"] for h in folded]
        assert sum(len(h.get("also_in", [])) for h in folded) == 1


def test_search_returns_distinct_passages_and_names_the_copies(palace_path, collection):
    copy_text = (
        "The lantern stays lit on the third floor until the last guest leaves, "
        "and whoever closes up writes the time in the book by the door."
    )
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
    assert all("chunk_index" in ref for ref in copy_hit["also_in"])
    assert sorted(ref["source_path"] for ref in copy_hit["also_in"]) == sorted(
        {"/orig/log.md", "/backup/log.md", "/autosave/log.md"} - {copy_hit["source_path"]}
    )


def test_cli_search_prints_the_other_copies(palace_path, collection, capsys):
    from mempalace.searcher import search

    text = (
        "Checkpoint the palace before every migration and keep the backup "
        "until the new version has run cleanly for a full week of use."
    )
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
