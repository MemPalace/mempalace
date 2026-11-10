"""Canonical CLI output remains stable for raw and authorized structured hits."""

import pytest

from mempalace.searcher import render_cli_search


@pytest.mark.parametrize("vector_disabled", [False, True])
def test_gateway_flat_row_ignores_unrelated_metadata(vector_disabled):
    from commonplace.gateway import _render_scoped_cli_search

    row = {
        "wing": "private_chats",
        "room": "thr_0123456789abcdef",
        "source_file": "right.md",
        "source_path": "/notes/right.md",
        "text": "Imported words",
        "metadata": {"source_thread": "thr_0123456789abcdef", "import_view_sha256": "digest"},
    }
    result = {"query": "Imported", "results": [row], "vector_disabled": vector_disabled}
    output = _render_scoped_cli_search(result, {"query": "Imported"})["cli_output"]
    expected = """
============================================================
  Results for: "Imported"
============================================================

  [1] private_chats / thr_0123456789abcdef
      Source: right.md
      Match:  bm25=0.0  (lexical candidate)

      Imported words

  --------------------------------------------------------

"""
    if vector_disabled:
        expected = (
            "\n  NOTICE: vector search disabled — HNSW index has diverged from SQLite.\n"
            "          Showing BM25-only results. Run `mempalace repair` to restore vector search.\n\n"
            + expected.replace("\n=", "=", 1).replace("(lexical candidate)", "(vector disabled)")
        )
    assert output == expected, "Flat row metadata replaced the authorized location"
    assert (
        render_cli_search("Imported", [row], flat=True, vector_disabled=vector_disabled) == expected
    )


def test_canonical_cli_hit_golden():
    flat = {
        "wing": "shared",
        "room": "notes",
        "source_file": "note.md",
        "source_path": "/tmp/note.md",
        "text": "\nOrdinary words\nsecond line\n",
        "distance": 0.368,
        "similarity": 0.632,
        "bm25_score": 1.445,
        "also_in": [{"source_file": "copy.md"}],
    }
    raw = {key: value for key, value in flat.items() if key not in ("source_path", "similarity")}
    raw["metadata"] = {"wing": "shared", "room": "notes", "source_file": "/tmp/note.md"}
    expected = """
============================================================
  Results for: "Ordinary"
  Wing: shared
  Room: notes
  Since: 2026-01-01
  Before: 2026-02-01
============================================================

  [1] shared / notes
      Source: note.md
      Also in: copy.md
      Match:  cosine_sim=0.632  bm25=1.445

      Ordinary words
      second line

  --------------------------------------------------------

"""
    args = dict(wing="shared", room="notes", since="2026-01-01", before="2026-02-01")
    assert render_cli_search("Ordinary", [raw], **args) == expected
    assert render_cli_search("Ordinary", [flat], **args) == expected


def test_canonical_cli_empty_golden():
    assert render_cli_search("Ordinary", []) == '\n  No results found for: "Ordinary"\n'


@pytest.mark.parametrize("vector_disabled", [False, True])
def test_canonical_cli_lexical_match_and_empty_source(vector_disabled):
    hit = {
        "wing": "shared",
        "room": "notes",
        "source_file": "?",
        "source_path": "",
        "text": "  Ordinary words  ",
        "distance": None,
        "bm25_score": 1.25,
    }
    output = render_cli_search("Ordinary", [hit], vector_disabled=vector_disabled)
    if vector_disabled:
        assert "Showing BM25-only results" in output
        assert "Match:  bm25=1.25  (vector disabled)" in output
    else:
        assert "      Source: \n" in output
        assert "Match:  bm25=1.25  (lexical candidate)" in output
    assert "      Ordinary words\n" in output
