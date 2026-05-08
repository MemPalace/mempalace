"""Tests for the hybrid closet+drawer retrieval in search_memories.

The hybrid path queries drawers directly (the floor) AND closets, applying a
rank-based boost to drawers whose source_file appears in top closet hits.
This avoids the "weak-closets regression" where low-signal closets (from
regex extraction on narrative content) could hide drawers that direct
search would have found.
"""

from mempalace.palace import (
    get_closets_collection,
    get_collection,
    upsert_closet_lines,
)
from mempalace.searcher import search_memories


def _seed_drawers(palace_path):
    """Insert 4 short drawers with deterministic content."""
    col = get_collection(palace_path, create=True)
    col.upsert(
        ids=["D1", "D2", "D3", "D4"],
        documents=[
            "We switched the auth service to use JWT tokens with a 24h expiry.",
            "Database migration to PostgreSQL 15 completed last Tuesday.",
            "The frontend team is debating whether to adopt TanStack Query.",
            "Kafka consumer rebalance timeout set to 45 seconds after incident.",
        ],
        metadatas=[
            {"wing": "backend", "room": "auth", "source_file": "fixture_D1.md"},
            {"wing": "backend", "room": "db", "source_file": "fixture_D2.md"},
            {"wing": "frontend", "room": "state", "source_file": "fixture_D3.md"},
            {"wing": "backend", "room": "queue", "source_file": "fixture_D4.md"},
        ],
    )


def _seed_strong_closet_for(palace_path, drawer_id, source_file, topics):
    """Insert a closet whose content strongly overlaps the query keywords."""
    col = get_closets_collection(palace_path)
    lines = [f"{t}||→{drawer_id}" for t in topics]
    upsert_closet_lines(
        col,
        closet_id_base=f"closet_{drawer_id}",
        lines=lines,
        metadata={
            "wing": "backend",
            "room": "auth",
            "source_file": source_file,
            "generated_by": "test",
        },
    )


# ── core invariant: closets can only HELP, never HIDE ─────────────────────


class TestHybridInvariant:
    def test_no_closets_degrades_to_direct_drawer_search(self, tmp_path):
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        # No closets created.
        result = search_memories("Kafka rebalance timeout", palace, n_results=3)
        ids = [h["source_file"] for h in result["results"]]
        assert ids, "should return results"
        assert "fixture_D4.md" in ids, "direct drawer search alone should surface the Kafka drawer"

    def test_weak_closets_do_not_hide_direct_drawer_hits(self, tmp_path):
        """A closet that points at a wrong drawer must NOT suppress the
        drawer that direct search would have ranked first."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        # Seed a misleading closet: it matches a generic phrase but points at D3.
        _seed_strong_closet_for(
            palace,
            drawer_id="D3",
            source_file="fixture_D3.md",
            topics=["Kafka queue tuning", "consumer rebalance config"],
        )
        result = search_memories("Kafka consumer rebalance timeout", palace, n_results=5)
        ids = [h["source_file"] for h in result["results"]]
        assert "fixture_D4.md" in ids, (
            "D4 must appear — direct drawer search alone would rank it first. "
            "Closet pointing to D3 should only boost D3, never hide D4."
        )

    def test_closet_boost_lifts_matching_drawer(self, tmp_path):
        """When a closet agrees with direct search, the matching drawer
        should be boosted to rank 1."""
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        _seed_strong_closet_for(
            palace,
            drawer_id="D1",
            source_file="fixture_D1.md",
            topics=["JWT auth tokens", "session expiry", "authentication service"],
        )
        result = search_memories("JWT auth tokens expiry", palace, n_results=3)
        ids = [h["source_file"] for h in result["results"]]
        assert ids[0] == "fixture_D1.md"
        top = result["results"][0]
        assert top["matched_via"] == "drawer+closet"
        assert top["closet_boost"] > 0


# ── closet_boost metadata ────────────────────────────────────────────────


class TestClosetMetadata:
    def test_closet_preview_exposed_when_boosted(self, tmp_path):
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        _seed_strong_closet_for(
            palace,
            drawer_id="D1",
            source_file="fixture_D1.md",
            topics=["JWT auth tokens", "24h expiry", "authentication"],
        )
        result = search_memories("JWT authentication", palace, n_results=2)
        top = result["results"][0]
        assert top["source_file"] == "fixture_D1.md"
        assert "closet_preview" in top

    def test_drawer_only_hits_have_no_closet_preview(self, tmp_path):
        palace = str(tmp_path / "palace")
        _seed_drawers(palace)
        # No closets
        result = search_memories("TanStack Query", palace, n_results=2)
        assert result["results"]
        for h in result["results"]:
            assert h["matched_via"] == "drawer"
            assert "closet_preview" not in h
            assert h["closet_boost"] == 0.0
