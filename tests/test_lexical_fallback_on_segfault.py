"""Tests for the FTS5 lexical fallback when chromadb segfaults on open.

A corrupt HNSW segment makes chromadb segfault (SIGSEGV) while loading the
vector segment. SIGSEGV kills the interpreter and cannot be caught with
try/except, so ``search()`` probes the open in a throwaway subprocess via
``_chromadb_open_crashes``; when the probe reports the open is unsafe it
serves read-only FTS5/sqlite lexical results instead of taking the CLI down.

These tests mock the probe and the sqlite reader so they exercise the
routing and rendering without needing an actually-corrupt palace.
"""

import subprocess

import pytest

from mempalace import config, searcher
from mempalace.backends import chroma as chroma_backend


def _canned_lexical_result():
    return {
        "query": "pelissari",
        "filters": {"wing": None, "room": None},
        "total_before_filter": 1,
        "results": [
            {
                "text": "Pelissari runs SAP Solman mandante 100.",
                "wing": "cast",
                "room": "customers",
                "source_file": "pelissari.md",
                "bm25_score": 1.234,
            }
        ],
        "fallback": "bm25_only_via_sqlite",
    }


def test_search_serves_lexical_when_open_unsafe(monkeypatch, capsys):
    """Unsafe open -> lexical results with banner, no exception, no vector call."""
    monkeypatch.setattr(config, "get_configured_collection_name", lambda: "drawers")
    monkeypatch.setattr(searcher, "_chromadb_open_crashes", lambda *a, **k: True)

    seen = {}

    def fake_bm25(query, palace_path, **kwargs):
        seen.update(kwargs)
        seen["query"] = query
        return _canned_lexical_result()

    monkeypatch.setattr(searcher, "_bm25_only_via_sqlite", fake_bm25)
    # The vector path must not be reached on an unsafe open.
    monkeypatch.setattr(
        searcher,
        "_open_collection_or_explain",
        lambda *a, **k: pytest.fail("vector path taken despite unsafe open"),
    )

    searcher.search("pelissari", "/some/palace")

    out = capsys.readouterr().out
    assert "lexical mode — vector index unavailable" in out
    assert "Pelissari runs SAP Solman" in out
    assert seen["collection_name"] == "drawers"
    assert seen["query"] == "pelissari"


def test_search_lexical_fallback_reports_no_results(monkeypatch, capsys):
    """Unsafe open with an empty lexical hit set prints the no-results line."""
    monkeypatch.setattr(config, "get_configured_collection_name", lambda: "drawers")
    monkeypatch.setattr(searcher, "_chromadb_open_crashes", lambda *a, **k: True)
    monkeypatch.setattr(
        searcher,
        "_bm25_only_via_sqlite",
        lambda *a, **k: {"results": [], "fallback": "bm25_only_via_sqlite"},
    )

    searcher.search("zzqqxx9nonexistentterm", "/some/palace")

    assert "No results found" in capsys.readouterr().out


def test_healthy_palace_keeps_vector_path(monkeypatch):
    """A safe open never touches the lexical fallback; the vector path runs."""
    monkeypatch.setattr(config, "get_configured_collection_name", lambda: "drawers")
    monkeypatch.setattr(searcher, "_chromadb_open_crashes", lambda *a, **k: False)
    monkeypatch.setattr(
        searcher,
        "_bm25_only_via_sqlite",
        lambda *a, **k: pytest.fail("lexical fallback taken on a healthy palace"),
    )
    # Vector path opens the collection; return None so search raises instead of
    # querying a real chromadb — enough to prove the branch was taken.
    monkeypatch.setattr(searcher, "_open_collection_or_explain", lambda *a, **k: None)
    monkeypatch.setattr(searcher.os.path, "isdir", lambda p: True)

    with pytest.raises(searcher.SearchError):
        searcher.search("pelissari", "/some/palace")


@pytest.mark.parametrize(
    "returncode, expected",
    [(0, False), (1, False), (-11, True)],  # -11 = death by SIGSEGV
)
def test_chromadb_open_crashes_reads_subprocess_exit(monkeypatch, returncode, expected):
    """Only a signal death is unsafe; a clean non-zero exit is a catchable
    exception that the in-process open re-raises with its own diagnostic."""
    monkeypatch.setattr(
        searcher.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, returncode),
    )
    assert searcher._chromadb_open_crashes("/p", "drawers") is expected


def test_chromadb_open_crashes_true_on_timeout(monkeypatch):
    """A hung open (TimeoutExpired) is treated as unsafe."""

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=15)

    monkeypatch.setattr(searcher.subprocess, "run", boom)
    assert searcher._chromadb_open_crashes("/p", "drawers") is True


# ── Probe verdict cache ────────────────────────────────────────────────


@pytest.fixture
def probe_spy(monkeypatch):
    """Count subprocess probes and control the segment signature."""
    state = {
        "signature": ("seg-1", (("data_level0.bin", 7, 100, 4096),)),
        "calls": 0,
        "returncode": 0,
    }

    def fake_run(*a, **k):
        state["calls"] += 1
        return subprocess.CompletedProcess(a, state["returncode"])

    monkeypatch.setattr(searcher.subprocess, "run", fake_run)
    monkeypatch.setattr(
        chroma_backend,
        "hnsw_segment_signature",
        lambda *a, **k: state["signature"],
    )
    searcher._open_probe_cache.clear()
    yield state
    searcher._open_probe_cache.clear()


def test_open_probe_cached_while_segment_unchanged(probe_spy):
    """A healthy palace pays the probe once, then hits the cache."""
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 1


def test_open_probe_reprobes_when_segment_changes(probe_spy):
    """A changed segment file invalidates the cached verdict."""
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 1

    probe_spy["signature"] = ("seg-1", (("data_level0.bin", 7, 200, 8192),))
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 2


def test_open_probe_caches_unsafe_verdict(probe_spy):
    """An unsafe verdict is cached too — no re-probe while the segment is untouched."""
    probe_spy["returncode"] = -11
    assert searcher._chromadb_open_crashes("/p", "drawers") is True
    assert searcher._chromadb_open_crashes("/p", "drawers") is True
    assert probe_spy["calls"] == 1


def test_open_probe_uncached_without_signature(monkeypatch, probe_spy):
    """No resolvable segment means no cache key: probe every call."""
    probe_spy["signature"] = None
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 2


def test_open_probe_cache_ages_out(monkeypatch, probe_spy):
    """The max-age ceiling forces a re-probe even with an unchanged signature."""
    now = {"t": 1000.0}
    monkeypatch.setattr(searcher.time, "monotonic", lambda: now["t"])

    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 1

    now["t"] += searcher._OPEN_PROBE_CACHE_MAX_AGE_SECONDS + 1
    assert searcher._chromadb_open_crashes("/p", "drawers") is False
    assert probe_spy["calls"] == 2
