"""Date provenance must distinguish imported content from its filing time (#2450)."""

import os
import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from mempalace import miner, searcher
from mempalace.backends.base import LexicalHit, LexicalResult
from mempalace.palace import get_collection

FILED_AT = "2026-09-06T12:00:00"
CONTENT_DATE = "2020-01-02"
TEXT = "Archived deployment decision: keep the original configuration."
DATE_FIELDS = (
    "created_at",
    "filed_at",
    "authored_at",
    "authored_at_source",
    "content_date",
    "content_date_source",
)


@pytest.fixture(params=["vector", "union", "sqlite"])
def date_search(request, monkeypatch, tmp_path):
    """Exercise each real result-building path without embedding downloads."""

    def run(dates, **kwargs):
        meta = {"source_file": "/archive/2020-01-02.md", "chunk_index": 0, **dates}
        if request.param == "sqlite":
            with sqlite3.connect(tmp_path / "chroma.sqlite3") as conn:
                conn.executescript(
                    """
                    CREATE VIRTUAL TABLE embedding_fulltext_search
                        USING fts5(string_value, tokenize='trigram');
                    CREATE TABLE embedding_metadata
                        (id INTEGER, key TEXT, string_value TEXT, int_value INTEGER);
                    CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT);
                    CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT);
                    CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT,
                        embedding_id TEXT, created_at TEXT);
                    INSERT INTO collections VALUES ('c1', 'mempalace_drawers');
                    INSERT INTO segments VALUES ('s1', 'c1');
                    INSERT INTO embeddings VALUES (1, 's1', 'archive', '2026-09-06');
                    """
                )
                conn.execute("INSERT INTO embedding_fulltext_search VALUES (?)", (TEXT,))
                conn.executemany(
                    "INSERT INTO embedding_metadata VALUES (1, ?, ?, NULL)",
                    [("chroma:document", TEXT), *meta.items()],
                )
            return searcher.search_memories(
                "deployment decision",
                str(tmp_path),
                vector_disabled=True,
                collection_name="mempalace_drawers",
                **kwargs,
            )

        col = MagicMock()
        col.metadata = {"hnsw:space": "cosine"}
        # Union must construct a genuinely lexical-only hit, not reuse a
        # vector hit that would conceal missing fields in the merge path.
        is_vector = request.param == "vector"
        col.query.return_value = {
            "ids": [["archive"] if is_vector else []],
            "documents": [[TEXT] if is_vector else []],
            "metadatas": [[meta] if is_vector else []],
            "distances": [[0.1] if is_vector else []],
        }
        col.lexical_search.return_value = LexicalResult(
            hits=[LexicalHit(id="archive", document=TEXT, metadata=meta, score=1.0)]
        )
        monkeypatch.setattr(searcher, "get_collection", lambda *a, **k: col)

        def no_closets(*args, **kwargs):
            raise FileNotFoundError("No closets in this synthetic palace")

        monkeypatch.setattr(searcher, "get_closets_collection", no_closets)
        return searcher.search_memories(
            "deployment decision", str(tmp_path), candidate_strategy=request.param, **kwargs
        )

    return run


@pytest.mark.parametrize(
    "metadata, expected",
    [
        pytest.param(
            {"filed_at": FILED_AT},
            (FILED_AT, FILED_AT, FILED_AT, "filed_at", None, "unknown"),
            id="filing-fallback-is-not-authorship",
        ),
        pytest.param(
            {
                "filed_at": FILED_AT,
                "authored_at": "2020-01-03T09:00:00",
                "content_date": CONTENT_DATE,
                "content_date_source": "frontmatter",
            },
            (
                FILED_AT,
                FILED_AT,
                "2020-01-03T09:00:00",
                "authored_at",
                CONTENT_DATE,
                "frontmatter",
            ),
            id="explicit-authorship-and-inferred-content-are-independent",
        ),
        pytest.param(
            {"filed_at": FILED_AT, "content_date": CONTENT_DATE},
            (FILED_AT, FILED_AT, FILED_AT, "filed_at", CONTENT_DATE, "unknown"),
            id="legacy-date-source-is-not-guessed-from-filename",
        ),
        pytest.param(
            {},
            ("unknown", "unknown", "unknown", "unknown", None, "unknown"),
            id="missing-dates",
        ),
        pytest.param(
            {
                "filed_at": "unknown",
                "authored_at": "unknown",
                "content_date": "unknown",
                "content_date_source": "filename",
            },
            ("unknown",) * 6,
            id="unknown-dates-do-not-gain-provenance",
        ),
        pytest.param(
            {"filed_at": FILED_AT, "authored_at": None, "content_date": None},
            (FILED_AT, FILED_AT, None, "unknown", None, "unknown"),
            id="explicit-null-does-not-change-legacy-fallback",
        ),
        pytest.param(
            {"filed_at": None, "authored_at": "", "content_date": ""},
            (None, None, "", "unknown", "", "unknown"),
            id="empty-values-preserved",
        ),
        pytest.param(
            {"filed_at": None},
            (None, None, None, "unknown", None, "unknown"),
            id="null-filing-fallback-preserved",
        ),
    ],
)
def test_search_date_provenance(date_search, metadata, expected):
    result = date_search(metadata)
    assert "error" not in result
    assert len(result["results"]) == 1
    hit = result["results"][0]
    assert tuple(hit[key] for key in DATE_FIELDS) == expected
    assert hit["text"] == TEXT


@pytest.mark.parametrize(
    "bounds, expected_count",
    [
        ({"since": FILED_AT}, 1),
        ({"before": FILED_AT}, 0),
        ({"since": CONTENT_DATE, "before": "2020-01-04"}, 0),
        ({"since": "2026-09-06", "before": "2026-09-07"}, 1),
    ],
)
def test_date_windows_still_filter_filing_time(date_search, bounds, expected_count):
    result = date_search(
        {
            "filed_at": FILED_AT,
            "authored_at": CONTENT_DATE,
            "content_date": CONTENT_DATE,
            "content_date_source": "filename",
        },
        **bounds,
    )
    assert "error" not in result
    assert len(result["results"]) == expected_count


@pytest.mark.parametrize("mining_path", ["project", "format"])
@pytest.mark.parametrize(
    "filename, prefix, source, date",
    [
        (
            "2020-01-02",
            "---\ndate: 2021-03-04\n---\nNotes from 2022-05-06.\n",
            "filename",
            CONTENT_DATE,
        ),
        (
            "untitled",
            "---\ndate: 2020-01-02\n---\nNotes from 2022-05-06.\n",
            "frontmatter",
            CONTENT_DATE,
        ),
        ("untitled", "Notes from 2020-01-02.\n", "body", CONTENT_DATE),
        ("untitled", "", "mtime", "2023-07-15"),
    ],
)
def test_mining_records_content_date_source(
    tmp_path, palace_path, mining_path, filename, prefix, source, date
):
    extension = ".md" if mining_path == "project" else ".pdf"
    path = tmp_path / (filename + extension)
    content = prefix + TEXT
    path.write_text(content, encoding="utf-8")
    mtime = datetime(2023, 7, 15, 12).timestamp()
    os.utime(path, (mtime, mtime))
    col = get_collection(palace_path, create=True)

    if mining_path == "project":
        added, _, _ = miner.process_file(
            path, tmp_path, col, "archive", [], "test", False, min_chunk_size=1
        )
    else:
        from mempalace.format_miner import _file_chunks_locked

        # Test the format miner's filing seam with already-extracted text;
        # no office-format parser or optional dependency is required.
        added, skipped = _file_chunks_locked(
            col,
            str(path),
            [{"content": content, "chunk_index": 0}],
            "archive",
            "documents",
            "test",
            source_mtime=mtime,
            content=content,
        )
        assert not skipped

    assert added == 1
    stored = col.get(include=["documents", "metadatas"])
    assert stored.documents == [content]
    meta = stored.metadatas[0]
    assert meta["content_date"] == date
    assert meta["content_date_source"] == source
    assert "authored_at" not in meta
    assert miner._extract_content_date(str(path), content) == date

    hit = searcher.search_memories("deployment decision", palace_path)["results"][0]
    assert hit["filed_at"] == meta["filed_at"] == hit["created_at"] == hit["authored_at"]
    assert hit["authored_at_source"] == "filed_at"
    assert hit["content_date"] == date
    assert hit["content_date_source"] == source


def test_unextractable_date_remains_absent(tmp_path):
    path = str(tmp_path / "missing.md")
    assert miner._extract_content_date_with_source(path, TEXT) == (None, "unknown")
    assert miner._extract_content_date(path, TEXT) is None
    meta = miner._build_drawer_metadata("w", "r", path, 0, "test", TEXT, None)
    assert "content_date" not in meta
    assert "content_date_source" not in meta


def test_metadata_builder_does_not_guess_source_for_legacy_callers():
    meta = miner._build_drawer_metadata(
        "w", "r", "/2020-01-02.md", 0, "test", TEXT, None, content_date=CONTENT_DATE
    )
    assert meta["content_date"] == CONTENT_DATE
    assert meta["content_date_source"] == "unknown"


def test_light_search_preserves_provenance_and_filing_recency(date_search):
    from mempalace.mcp_light_server import _enrich_search_results

    result = date_search(
        {"filed_at": FILED_AT, "content_date": CONTENT_DATE, "content_date_source": "filename"}
    )
    old_hit = result["results"][0]
    expected_dates = {key: old_hit[key] for key in DATE_FIELDS}
    result["results"].append(
        {
            "drawer_id": "recent-content-filed-earlier",
            "created_at": "2025-01-01",
            "filed_at": "2025-01-01",
            "content_date": "2024-01-01",
        }
    )
    enriched = _enrich_search_results(result)
    assert enriched["results"][0] is old_hit
    assert {key: old_hit[key] for key in DATE_FIELDS} == expected_dates
    assert old_hit["recency_rank"] == 1
    assert old_hit["is_latest_record"] is True
