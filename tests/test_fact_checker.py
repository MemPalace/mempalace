"""
test_fact_checker.py — Regression + integration tests for fact_checker.

Covers every detection path + the three bugs the original PR silently
hid behind ``except Exception: pass``:

  * ``kg.query()`` doesn't exist — code must use ``query_entity``.
  * ``KnowledgeGraph(palace_path=...)`` is not a valid kwarg — code
    must pass ``db_path``.
  * O(n²) edit-distance over the full registry — must filter to names
    actually mentioned in the text.

Also pins the three feature contracts:
  * similar_name  — "Mila" vs "Milla" in a registry with both.
  * relationship_mismatch — "Bob is Alice's brother" vs KG "husband".
  * stale_fact   — claim matches a triple whose valid_to is in the past.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mempalace.fact_checker import (
    _check_entity_confusion,
    _edit_distance,
    _extract_claims,
    _flatten_names,
    check_text,
)
from mempalace.knowledge_graph import KnowledgeGraph


# ── claim extraction ─────────────────────────────────────────────────


class TestExtractClaims:
    def test_parses_x_is_ys_z(self):
        claims = _extract_claims("Bob is Alice's brother")
        assert len(claims) == 1
        assert claims[0] == {
            "subject": "Bob",
            "predicate": "brother",
            "object": "Alice",
            "span": "Bob is Alice's brother",
        }

    def test_parses_xs_z_is_y(self):
        claims = _extract_claims("Alice's brother is Bob")
        assert len(claims) == 1
        assert claims[0]["subject"] == "Bob"
        assert claims[0]["predicate"] == "brother"
        assert claims[0]["object"] == "Alice"

    def test_ignores_sentences_without_possessive_role(self):
        assert _extract_claims("Bob drove to the store today") == []
        assert _extract_claims("Just some prose without relationships") == []

    def test_multiple_claims_in_one_text(self):
        claims = _extract_claims("Bob is Alice's brother. Carol is Dave's sister.")
        subjects = {c["subject"] for c in claims}
        assert subjects == {"Bob", "Carol"}


# ── entity confusion ─────────────────────────────────────────────────


class TestEntityConfusion:
    def test_flags_near_name_when_only_one_mentioned(self):
        registry = {"people": ["Milla", "Mila"]}
        issues = _check_entity_confusion("I spoke with Mila today.", registry)
        # "Mila" mentioned, "Milla" not — registry has both at edit-distance 1,
        # flag the possible confusion.
        assert len(issues) == 1
        assert issues[0]["type"] == "similar_name"
        assert set(issues[0]["names"]) == {"Mila", "Milla"}
        assert issues[0]["distance"] == 1

    def test_no_false_positive_when_both_names_mentioned(self):
        """Regression: a text discussing both Mila and Milla is fine —
        the user clearly knows they're different. Don't nag."""
        registry = {"people": ["Milla", "Mila"]}
        issues = _check_entity_confusion("Mila and Milla met for lunch.", registry)
        assert issues == []

    def test_no_issues_when_registry_empty(self):
        assert _check_entity_confusion("Bob said hi", {}) == []
        assert _check_entity_confusion("Bob said hi", {"people": []}) == []

    def test_no_issues_when_no_mentioned_names(self):
        registry = {"people": ["Zelda", "Link", "Sheik"]}
        assert _check_entity_confusion("nothing relevant here", registry) == []

    def test_registry_dict_shape_is_supported(self):
        # Some registries store {"people": {"Alice": {...meta}}}; we still
        # need to surface the keys as candidate names.
        registry = {"people": {"Milla": {"role": "creator"}, "Mila": {}}}
        issues = _check_entity_confusion("I messaged Mila yesterday", registry)
        assert any("Milla" in (i["names"] or []) for i in issues)


class TestEditDistance:
    def test_basic_distances(self):
        assert _edit_distance("kitten", "sitting") == 3
        assert _edit_distance("mila", "milla") == 1
        assert _edit_distance("abc", "abc") == 0

    def test_empty_strings(self):
        assert _edit_distance("", "") == 0
        assert _edit_distance("abc", "") == 3
        assert _edit_distance("", "abc") == 3

    def test_performance_bounded_by_mentioned_names(self):
        """Regression: an earlier implementation did O(n²) pairwise
        edit-distance over every registry entry on every check_text call.
        With 100 names and zero mentions, the call must return in a blink
        because no edit-distance comparison should even start."""
        import time

        # 500 random names, none of which appear in the text.
        registry = {"people": [f"Zelda{i:03d}" for i in range(500)]}
        text = "completely irrelevant prose with no registered names at all"

        start = time.perf_counter()
        issues = _check_entity_confusion(text, registry)
        elapsed = time.perf_counter() - start

        assert issues == []
        # Even an unoptimized implementation should beat this by orders
        # of magnitude once we've filtered to mentioned names (which is
        # 0 here) — if it's still doing O(n²), we'll blow past.
        assert elapsed < 0.2, f"entity confusion took {elapsed:.3f}s on empty mentions"


# ── _flatten_names helper ────────────────────────────────────────────


class TestFlattenNames:
    def test_handles_list_categories(self):
        assert _flatten_names({"people": ["Ada", "Bob"]}) == {"Ada", "Bob"}

    def test_handles_dict_categories(self):
        assert _flatten_names({"people": {"Ada": {}, "Bob": {}}}) == {"Ada", "Bob"}

    def test_skips_falsy_entries(self):
        assert _flatten_names({"people": ["Ada", "", None, "Bob"]}) == {"Ada", "Bob"}


# ── KG integration (uses a real tmp SQLite palace) ───────────────────


@pytest.fixture
def palace_with_kg(tmp_path):
    """Palace directory with a real KG pre-seeded with a few triples.

    The KG file lives at ``<palace>/knowledge_graph.sqlite3`` — same
    convention used by the MCP server. Fact-checker must find it via
    that path, not via a bogus ``palace_path`` kwarg.
    """
    palace = tmp_path / "palace"
    palace.mkdir()
    db = str(palace / "knowledge_graph.sqlite3")
    kg = KnowledgeGraph(db_path=db)
    yield palace, kg


class TestKGContradictions:
    def test_kg_init_uses_db_path_not_palace_path_kwarg(self):
        """Regression: the original code passed ``palace_path=`` to a
        constructor whose only kwarg is ``db_path``. That raised
        TypeError — silently swallowed — and the KG path became dead
        code. This test pins the correct call signature."""
        # Simply construct via the correct signature; raising means the
        # KG constructor has changed in a way that fact_checker must too.
        kg = KnowledgeGraph(db_path=":memory:")
        # query_entity must exist (this is the method fact_checker calls).
        assert callable(getattr(kg, "query_entity", None))
        # The API that fact_checker used to call does NOT exist.
        assert not hasattr(kg, "query")

    def test_relationship_mismatch_detected(self, palace_with_kg):
        """The feature's headline example: text says brother, KG says husband."""
        palace, kg = palace_with_kg
        kg.add_triple("Bob", "husband_of", "Alice", valid_from="2020-01-01")

        issues = check_text("Bob is Alice's husband_of", str(palace))
        # Exact-predicate + same object → no mismatch.
        assert all(i["type"] != "relationship_mismatch" for i in issues)

        issues = check_text("Bob is Alice's brother", str(palace))
        mismatches = [i for i in issues if i["type"] == "relationship_mismatch"]
        assert mismatches, "should flag text/KG mismatch for same (subject, object)"
        m = mismatches[0]
        assert m["entity"] == "Bob"
        assert m["claim"]["predicate"] == "brother"
        assert m["kg_fact"]["predicate"] == "husband_of"

    def test_no_false_positive_when_kg_has_no_facts_about_subject(self, palace_with_kg):
        palace, _ = palace_with_kg
        # KG is empty → no mismatch should fire.
        assert check_text("Bob is Alice's brother", str(palace)) == []

    def test_stale_fact_detected(self, palace_with_kg):
        palace, kg = palace_with_kg
        # An old relationship that was superseded in 2023. Using a
        # possessive-shape claim so the narrow claim-extraction regex
        # actually reaches the stale-fact branch.
        kg.add_triple(
            "Bob",
            "brother",
            "Alice",
            valid_from="2010-01-01",
            valid_to="2023-06-01",
        )
        issues = check_text("Bob is Alice's brother", str(palace))
        stale = [i for i in issues if i["type"] == "stale_fact"]
        assert stale, "should flag closed-window fact as stale"
        assert stale[0]["entity"] == "Bob"
        assert stale[0]["valid_to"].startswith("2023")

    def test_current_fact_same_triple_is_not_flagged(self, palace_with_kg):
        palace, kg = palace_with_kg
        kg.add_triple("Bob", "brother", "Alice", valid_from="2010-01-01")
        issues = check_text("Bob is Alice's brother", str(palace))
        assert issues == []

    def test_missing_palace_does_not_crash(self, tmp_path):
        """Brand-new palace (no KG file yet) — check_text must return []
        rather than raising or hanging."""
        nonexistent = str(tmp_path / "never_created")
        assert check_text("Bob is Alice's brother", nonexistent) == []


# ── end-to-end check_text contract ───────────────────────────────────


class TestCheckTextContract:
    def test_empty_text_returns_empty_list(self, tmp_path):
        assert check_text("", str(tmp_path / "palace")) == []

    def test_registry_confusion_path_isolated_from_kg(self, tmp_path, monkeypatch):
        """If the registry file is present but the KG is missing, the
        similar-name path must still fire. Prior implementations had
        such entangled state that one failure killed both paths."""
        # Bypass the real registry by pointing cache at a temp file.
        registry = tmp_path / "known_entities.json"
        registry.write_text(json.dumps({"people": ["Milla", "Mila"]}))
        from mempalace import miner

        monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
        miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})

        issues = check_text("Chatted with Mila.", str(tmp_path / "nonexistent_palace"))
        assert any(i["type"] == "similar_name" for i in issues)


# ── CLI ──────────────────────────────────────────────────────────────


class TestCLI:
    def test_exits_nonzero_when_issues_found(self, tmp_path, monkeypatch, capsys):
        """The CLI exit code is how shell scripts / hooks know to act —
        pin it explicitly."""
        registry = tmp_path / "known_entities.json"
        registry.write_text(json.dumps({"people": ["Milla", "Mila"]}))
        from mempalace import fact_checker, miner

        monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
        miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})

        # Simulate argv: "Mila said hi"
        monkeypatch.setattr(
            "sys.argv",
            ["fact_checker", "Mila said hi", "--palace", str(tmp_path / "palace")],
        )
        with pytest.raises(SystemExit) as excinfo:
            # Re-exec the __main__ block via runpy.
            import runpy

            runpy.run_module("mempalace.fact_checker", run_name="__main__")
        # Issues found → exit code 1.
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "similar_name" in out
        # Silence unused import warning.
        _ = (MagicMock, patch, fact_checker)

    def test_reconfigures_stdio_to_utf8_on_windows(self):
        """Windows fact_checker --stdin must decode payload as UTF-8.

        Without this, Python defaults stdio to the system ANSI codepage
        (cp1252/cp1251/cp950), which mojibakes non-ASCII text before
        pattern parsing sees it.
        """
        import io
        import sys

        from mempalace.fact_checker import _reconfigure_stdio_utf8_on_windows

        class _ReconfigurableStringIO(io.StringIO):
            def __init__(self, initial_value=""):
                super().__init__(initial_value)
                self.reconfigure_calls = []

            def reconfigure(self, **kwargs):
                self.reconfigure_calls.append(kwargs)

        stdin = _ReconfigurableStringIO()
        stdout = _ReconfigurableStringIO()
        stderr = _ReconfigurableStringIO()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(sys, "stdin", stdin),
            patch.object(sys, "stdout", stdout),
            patch.object(sys, "stderr", stderr),
        ):
            _reconfigure_stdio_utf8_on_windows()

        # Per-stream errors policy: stdin uses surrogateescape so a stray
        # malformed byte from a redirected file does not crash the read,
        # stdout/stderr use replace so an extracted fact carrying a
        # surrogate half does not crash mid-print.
        assert stdin.reconfigure_calls == [{"encoding": "utf-8", "errors": "surrogateescape"}]
        assert stdout.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]
        assert stderr.reconfigure_calls == [{"encoding": "utf-8", "errors": "replace"}]

    def test_reconfigure_stdio_is_noop_off_windows(self):
        """Linux/macOS already default to UTF-8 stdio -- helper must not touch streams."""
        import io
        import sys

        from mempalace.fact_checker import _reconfigure_stdio_utf8_on_windows

        class _ReconfigurableStringIO(io.StringIO):
            def __init__(self):
                super().__init__()
                self.reconfigure_calls = []

            def reconfigure(self, **kwargs):
                self.reconfigure_calls.append(kwargs)

        stdin = _ReconfigurableStringIO()
        with (
            patch.object(sys, "platform", "linux"),
            patch.object(sys, "stdin", stdin),
        ):
            _reconfigure_stdio_utf8_on_windows()

        assert stdin.reconfigure_calls == []
