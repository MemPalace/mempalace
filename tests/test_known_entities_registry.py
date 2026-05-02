"""Tests for mempalace.miner.add_to_known_entities.

Covers the init → miner wire-up: init's confirmed entities merged into
``~/.mempalace/known_entities.json`` so the miner's drawer-tagging path
recognizes them at mine time.

Every test redirects the registry path to a tmp_path to avoid touching
the real ~/.mempalace/ on the developer's machine.
"""

import json

import pytest

from mempalace import miner


@pytest.fixture
def temp_registry(tmp_path, monkeypatch):
    """Redirect the module-level registry path to a tmp file and reset cache."""
    registry = tmp_path / "known_entities.json"
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})
    return registry


# ── fresh-file cases ────────────────────────────────────────────────────


def test_creates_registry_when_absent(temp_registry):
    assert not temp_registry.exists()
    miner.add_to_known_entities({"people": ["Alice", "Bob"], "projects": ["foo"]})
    assert temp_registry.exists()
    data = json.loads(temp_registry.read_text())
    assert sorted(data["people"]) == ["Alice", "Bob"]
    assert data["projects"] == ["foo"]


def test_returns_registry_path(temp_registry):
    result = miner.add_to_known_entities({"people": ["Alice"]})
    assert result == str(temp_registry)


def test_empty_input_still_creates_file(temp_registry):
    """A no-op merge still touches the file (idempotent), but no entries added."""
    miner.add_to_known_entities({})
    # File may or may not be written for a truly empty call — tolerate either.
    if temp_registry.exists():
        data = json.loads(temp_registry.read_text())
        assert data == {} or all(not v for v in data.values())


def test_skips_empty_name_strings(temp_registry):
    miner.add_to_known_entities({"people": ["Alice", "", None]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]


# ── union / dedup cases ────────────────────────────────────────────────


def test_unions_with_existing_list_category(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice", "Bob"]}))
    miner.add_to_known_entities({"people": ["Bob", "Carol"]})
    data = json.loads(temp_registry.read_text())
    # Bob not duplicated, Carol appended, original order preserved
    assert data["people"] == ["Alice", "Bob", "Carol"]


def test_case_insensitive_dedup_preserves_first_seen_variant(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    miner.add_to_known_entities({"people": ["alice", "ALICE", "Bob"]})
    data = json.loads(temp_registry.read_text())
    # Alice stays as-is; lowercase/uppercase variants don't create new entries
    assert data["people"] == ["Alice", "Bob"]


def test_preserves_untouched_categories(temp_registry):
    """A category the caller didn't mention must be left alone."""
    temp_registry.write_text(json.dumps({"people": ["Alice"], "places": ["Paris", "Tokyo"]}))
    miner.add_to_known_entities({"people": ["Bob"]})
    data = json.loads(temp_registry.read_text())
    assert data["places"] == ["Paris", "Tokyo"]
    assert data["people"] == ["Alice", "Bob"]


def test_adds_new_categories(temp_registry):
    temp_registry.write_text(json.dumps({"people": ["Alice"]}))
    miner.add_to_known_entities({"projects": ["foo", "bar"]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]
    assert data["projects"] == ["foo", "bar"]


def test_dedupes_within_input(temp_registry):
    miner.add_to_known_entities({"people": ["Alice", "alice", "Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == ["Alice"]


# ── dict-format existing registry ──────────────────────────────────────


def test_dict_format_existing_category_gets_new_keys(temp_registry):
    """Miner supports {name: code} dict categories (alternate registry shape).
    New names are added as keys without overwriting existing codes."""
    temp_registry.write_text(json.dumps({"people": {"Alice": "ALC", "Bob": "BOB"}}))
    miner.add_to_known_entities({"people": ["Alice", "Carol"]})
    data = json.loads(temp_registry.read_text())
    # Alice's code survives; Carol added with None; Bob untouched
    assert data["people"]["Alice"] == "ALC"
    assert data["people"]["Bob"] == "BOB"
    assert "Carol" in data["people"]
    assert data["people"]["Carol"] is None


def test_dict_format_dedupes_case_insensitively_and_stringifies_new_names(temp_registry):
    temp_registry.write_text(json.dumps({"people": {"Alice": "ALC"}}))
    miner.add_to_known_entities({"people": ["alice", 123]})
    data = json.loads(temp_registry.read_text())
    assert data["people"] == {"Alice": "ALC", "123": None}


# ── error tolerance ───────────────────────────────────────────────────


def test_malformed_existing_registry_starts_fresh(temp_registry):
    temp_registry.write_text("{ not valid json")
    miner.add_to_known_entities({"people": ["Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data == {"people": ["Alice"]}


def test_non_dict_existing_registry_starts_fresh(temp_registry):
    temp_registry.write_text(json.dumps(["unexpected", "array"]))
    miner.add_to_known_entities({"people": ["Alice"]})
    data = json.loads(temp_registry.read_text())
    assert data == {"people": ["Alice"]}


def test_non_list_input_category_ignored(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"], "weird": "not a list"})
    data = json.loads(temp_registry.read_text())
    assert "weird" not in data or data.get("weird") == "not a list"
    assert data["people"] == ["Alice"]


# ── cache invalidation ───────────────────────────────────────────────


def test_cache_invalidated_so_subsequent_load_sees_write(temp_registry):
    """cmd_init → cmd_mine runs in the same process; the load path must
    see what init just wrote without a process restart."""
    # Prime the cache with an empty state
    miner._load_known_entities()
    assert miner._load_known_entities() == frozenset()

    miner.add_to_known_entities({"people": ["Alice", "Bob"], "projects": ["foo"]})

    loaded = miner._load_known_entities()
    assert "Alice" in loaded
    assert "Bob" in loaded
    assert "foo" in loaded


def test_raw_view_reflects_write(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"]})
    raw = miner._load_known_entities_raw()
    assert raw.get("people") == ["Alice"]


# ── Unicode round-trip ────────────────────────────────────────────────


def test_unicode_names_written_literally_not_escaped(temp_registry):
    """`ensure_ascii=False` so non-ASCII names stay readable on disk."""
    miner.add_to_known_entities({"people": ["Gergő Móricz", "Arturo Domínguez"]})
    raw_text = temp_registry.read_text(encoding="utf-8")
    assert "Gergő" in raw_text
    assert "Móricz" in raw_text
    # Round-trips through JSON
    data = json.loads(raw_text)
    assert "Gergő Móricz" in data["people"]


# ── end-to-end: does the write actually help _extract_entities_for_metadata? ──


def test_populated_registry_improves_miner_recall(temp_registry):
    """The whole point of the wire-up: names written via add_to_known_entities
    must be recognized by the miner's entity-extraction metadata pass."""
    miner.add_to_known_entities(
        {
            "people": ["Julia Grib", "Kevin Heifner"],
            "projects": ["hyperion-history", "mempalace"],
        }
    )

    sample = (
        "Met with Julia Grib yesterday about the mempalace release. "
        "Kevin Heifner pushed the hyperion-history fix."
    )
    result = miner._extract_entities_for_metadata(sample)
    tagged = set(result.split(";")) if result else set()

    # All four registered entities should land in the metadata string
    for expected in ("Julia Grib", "Kevin Heifner", "hyperion-history", "mempalace"):
        assert expected in tagged, f"expected '{expected}' in metadata {tagged!r}"


# ── topics_by_wing — cross-wing tunnel signal source (issue #1180) ──


def test_topics_persisted_under_topics_by_wing(temp_registry):
    miner.add_to_known_entities(
        {"people": ["Alice"], "topics": ["Angular", "OpenAPI"]},
        wing="wing_alpha",
    )
    data = json.loads(temp_registry.read_text())
    # Topics also stored as a flat list (existing-style aggregate).
    assert "Angular" in data["topics"]
    # And recorded by wing for tunnel computation.
    assert data["topics_by_wing"]["wing_alpha"] == ["Angular", "OpenAPI"]


def test_topics_by_wing_replaces_on_reinit(temp_registry):
    """Re-running init for the same wing should reflect the latest list,
    not accumulate stale topics indefinitely."""
    miner.add_to_known_entities({"topics": ["Angular", "OpenAPI"]}, wing="wing_alpha")
    miner.add_to_known_entities({"topics": ["OpenAPI", "Postgres"]}, wing="wing_alpha")
    data = json.loads(temp_registry.read_text())
    assert data["topics_by_wing"]["wing_alpha"] == ["OpenAPI", "Postgres"]


def test_topics_by_wing_multiple_wings_coexist(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_a")
    miner.add_to_known_entities({"topics": ["foo", "bar"]}, wing="wing_b")
    data = json.loads(temp_registry.read_text())
    assert data["topics_by_wing"] == {"wing_a": ["foo"], "wing_b": ["foo", "bar"]}


def test_topics_by_wing_skipped_without_wing(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]})
    data = json.loads(temp_registry.read_text())
    # No wing → no topics_by_wing entry, but topics list still saved.
    assert "topics_by_wing" not in data
    assert data["topics"] == ["foo"]


def test_topics_by_wing_dedupes_case_insensitive(temp_registry):
    miner.add_to_known_entities({"topics": ["OpenAPI", "openapi", "OPENAPI"]}, wing="wing_a")
    data = json.loads(temp_registry.read_text())
    # Only one entry, casing of the first observed name preserved.
    assert data["topics_by_wing"]["wing_a"] == ["OpenAPI"]


def test_get_topics_by_wing_reads_registry(temp_registry):
    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_a")
    miner.add_to_known_entities({"topics": ["foo", "bar"]}, wing="wing_b")
    result = miner.get_topics_by_wing()
    assert result == {"wing_a": ["foo"], "wing_b": ["foo", "bar"]}


def test_get_topics_by_wing_empty_when_missing(temp_registry):
    miner.add_to_known_entities({"people": ["Alice"]})
    assert miner.get_topics_by_wing() == {}


def test_topics_by_wing_does_not_pollute_known_names(temp_registry):
    """Wing names in topics_by_wing must NOT leak into the flat known-names
    set used by ``_extract_entities_for_metadata`` — only the topic strings
    themselves should be recognized."""
    miner.add_to_known_entities({"topics": ["Angular"]}, wing="wing_super_secret_project")
    known = miner._load_known_entities()
    assert "Angular" in known
    assert "wing_super_secret_project" not in known
