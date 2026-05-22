"""
test_closets.py — Tests for the closet (searchable index) layer and the
features that ride on top of it: mine_lock serialization, entity metadata,
hybrid BM25+vector search, and diary ingest.

Coverage map:
  * mine_lock — acquire/release, blocks concurrent acquisition.
  * build_closet_lines — pointer-line shape, header pickup, entity stoplist
    (regression for "When/After/The"), real-name survival, fallback line.
  * upsert_closet_lines — pure overwrite (regression for the append bug),
    char-limit packing without splitting a line.
  * purge_file_closets — scoped to source_file.
  * Project-miner end-to-end rebuild — re-mining with fewer topics fully
    purges leftover numbered closets from a larger prior run.
  * _extract_drawer_ids_from_closet — pointer parsing + dedup.
  * search_memories hybrid path — drawer query always the floor,
    closets boost matching source_file, matched_via reflects both signals,
    no whole-file glue, max_distance enforcement.
  * Entity metadata — extracted, stoplist applied, registry cached by mtime.
  * Real BM25 — real IDF over candidate corpus, hybrid rerank.
  * Diary ingest — drawers + closets created, incremental skips, state
    file lives outside the diary dir, wing-prefixed drawer IDs prevent
    cross-diary collisions, force=True purges leftover closets.
"""

import hashlib
import json
import multiprocessing
import os
import tempfile
import threading
import time

import pytest
import yaml

from mempalace.miner import (
    _extract_entities_for_metadata,
    _load_known_entities,
    mine,
)
from mempalace.palace import (
    CLOSET_CHAR_LIMIT,
    build_closet_lines,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)
from mempalace.palace_graph import (
    create_tunnel,
    delete_tunnel,
    follow_tunnels,
    list_tunnels,
)
from mempalace.searcher import (
    _bm25_scores,
    _expand_with_neighbors,
    _extract_drawer_ids_from_closet,
    _hybrid_rank,
    search_memories,
)


# ── mine_lock ────────────────────────────────────────────────────────────


def _lock_worker(target: str, name: str, hold_seconds: float, log_path: str) -> None:
    """Worker for multiprocessing-spawn concurrency test. Writes its
    critical-section enter/exit timestamps to ``log_path`` so the test
    can verify the sections did not overlap in time."""
    import time as _time

    from mempalace.palace import mine_lock as _mine_lock

    with _mine_lock(target):
        t_enter = _time.time()
        _time.sleep(hold_seconds)
        t_exit = _time.time()
        # Append atomically so concurrent writers don't stomp each other.
        with open(log_path, "a") as f:
            f.write(f"{name} {t_enter} {t_exit}\n")
            f.flush()


class TestMineLock:
    def test_lock_acquires_and_releases(self, tmp_path):
        target = str(tmp_path / "lock_target.txt")
        with mine_lock(target):
            lock_dir = os.path.expanduser("~/.mempalace/locks")
            assert os.path.isdir(lock_dir)
        # Re-acquire after release should succeed instantly.
        start = time.time()
        with mine_lock(target):
            pass
        assert time.time() - start < 1.0

    def test_lock_blocks_concurrent_access(self, tmp_path):
        """The lock's contract is inter-*process* (multi-agent), not
        inter-thread. Use multiprocessing so the test reflects the real
        use case and is portable: on macOS/BSD ``fcntl.flock`` is
        per-process, so two threads would both acquire — a thread-based
        test would flake there even when the lock is correct.

        Verify mutual exclusion by the effect the critical section
        actually has — each worker records its enter/exit timestamps
        under the lock, and the test asserts the two intervals do not
        overlap. This is robust to spawn-overhead timing, unlike
        "second worker waited at least N seconds" which flakes when CI
        spawn latency eats into the hold window.
        """
        target = str(tmp_path / "concurrent_lock.txt")
        log_path = str(tmp_path / "critical_section.log")
        # Spawn so the same code path runs on every OS (macOS 3.8+ and
        # Windows already default to spawn; Linux is fork by default).
        ctx = multiprocessing.get_context("spawn")

        # Each worker holds the lock for HOLD seconds. With real mutual
        # exclusion, the two [enter, exit] intervals must be disjoint.
        HOLD = 0.3
        p1 = ctx.Process(target=_lock_worker, args=(target, "a", HOLD, log_path))
        p2 = ctx.Process(target=_lock_worker, args=(target, "b", HOLD, log_path))
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        assert p1.exitcode == 0, f"p1 exited non-zero: {p1.exitcode}"
        assert p2.exitcode == 0, f"p2 exited non-zero: {p2.exitcode}"

        # Parse the log: "<name> <enter_ts> <exit_ts>".
        intervals = []
        with open(log_path) as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) == 3:
                    intervals.append((parts[0], float(parts[1]), float(parts[2])))
        assert len(intervals) == 2, f"expected two critical sections, got {intervals}"

        # Sort by entry time and verify the second entry is after the first exit.
        intervals.sort(key=lambda iv: iv[1])
        (_, enter_a, exit_a), (_, enter_b, exit_b) = intervals
        assert enter_a < exit_a <= enter_b < exit_b, (
            f"critical sections overlapped — lock failed to serialize: {intervals}"
        )


# ── build_closet_lines ─────────────────────────────────────────────────


class TestBuildClosetLines:
    def test_emits_pointer_line_shape(self):
        content = (
            "# Auth rewrite\n\n"
            "Decided we need to migrate to passkeys. "
            "Built the prototype with WebAuthn. "
            "Reviewed the API surface."
        )
        lines = build_closet_lines(
            "/proj/auth.md",
            ["drawer_proj_backend_aaa", "drawer_proj_backend_bbb"],
            content,
            wing="proj",
            room="backend",
        )
        assert lines, "should always emit at least one line"
        for line in lines:
            assert "→" in line, f"line missing pointer arrow: {line!r}"
            parts = line.split("|")
            assert len(parts) == 3, f"expected topic|entities|→refs, got {line!r}"
            assert parts[2].startswith("→")

    def test_extracts_section_headers_as_topics(self):
        content = "# First Header\nbody\n## Second Header\nmore body"
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r")
        joined = "\n".join(lines).lower()
        assert "first header" in joined
        assert "second header" in joined

    def test_entity_stoplist_filters_sentence_starters(self):
        # "When", "After", "The" repeat 3+ times — old code would index them
        # as entities. Stoplist drops them.
        content = (
            "When the pipeline ran, the result was good. "
            "When the user logged in, the token was issued. "
            "After the migration, the latency dropped. "
            "After the rollback, the latency rose. "
            "The new flow is stable. The audit cleared."
        )
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r")
        entity_segments = [line.split("|")[1] for line in lines]
        for seg in entity_segments:
            tokens = set(seg.split(";")) if seg else set()
            assert "When" not in tokens
            assert "After" not in tokens
            assert "The" not in tokens

    def test_real_proper_nouns_survive_stoplist(self):
        content = (
            "Igor reviewed the diff. Milla wrote the spec. "
            "Igor pushed the fix. Milla approved the PR. "
            "Igor and Milla shipped together."
        )
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r")
        joined_entities = ";".join(line.split("|")[1] for line in lines)
        assert "Igor" in joined_entities
        assert "Milla" in joined_entities

    def test_emits_fallback_line_when_nothing_extractable(self):
        content = "lorem ipsum dolor sit amet consectetur adipiscing elit"
        lines = build_closet_lines("/x/notes.txt", ["d1"], content, "wing", "room")
        assert len(lines) == 1
        assert "wing/room/notes" in lines[0]
        assert "→d1" in lines[0]

    def test_pointer_references_first_three_drawers(self):
        ids = [f"drawer_{i}" for i in range(10)]
        lines = build_closet_lines("/x.md", ids, "# A\n# B", "w", "r")
        assert all("→drawer_0,drawer_1,drawer_2" in line for line in lines)

    # ── Tier 6a — date + line-range pointer segment ────────────────────────

    def test_includes_date_line_segment_when_metas_provided(self):
        """When drawer_metas carry filed_at + line_start/line_end, each pointer
        line gains a 4th pipe-separated segment of shape ``YYYY-MM-DD:Lstart-Lend``
        between the entities and the ``→drawer_ids`` segment.
        """
        content = (
            "# Auth rewrite\n\nDecided we need to migrate to passkeys. "
            "Built the prototype with WebAuthn. Reviewed the API surface."
        )
        metas = [
            {
                "wing": "proj",
                "room": "backend",
                "source_file": "/proj/auth.md",
                "filed_at": "2026-05-21T22:30:00.123456",
                "line_start": 42,
                "line_end": 78,
            },
            {
                "wing": "proj",
                "room": "backend",
                "source_file": "/proj/auth.md",
                "filed_at": "2026-05-21T22:30:00.123456",
                "line_start": 79,
                "line_end": 110,
            },
        ]
        lines = build_closet_lines(
            "/proj/auth.md",
            ["drawer_a", "drawer_b"],
            content,
            wing="proj",
            room="backend",
            drawer_metas=metas,
        )
        assert lines
        for line in lines:
            parts = line.split("|")
            assert len(parts) == 4, f"expected 4 segments, got {line!r}"
            date_line_seg = parts[2]
            assert date_line_seg == "2026-05-21:L42-L78", (
                f"date+line segment wrong: {date_line_seg!r}"
            )
            assert parts[3].startswith("→")

    def test_falls_back_to_3_segment_format_when_metas_missing(self):
        """Backward compat — no drawer_metas → old 3-segment pointer shape."""
        content = "# Header\n\nBuilt the feature. Tested it."
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r")
        for line in lines:
            assert len(line.split("|")) == 3, f"expected 3-segment legacy format, got {line!r}"

    def test_falls_back_to_3_segment_format_when_metas_lack_line_keys(self):
        """Backward compat — metas present but no line_start/line_end → 3 segments.

        Drawers filed before Tier 6a landed lack line range keys. Closets
        built from those drawers must emit the legacy 3-segment form, not a
        broken pointer with ``None`` or empty values.
        """
        content = "# Header\n\nBuilt the feature. Tested it."
        metas = [
            {
                "wing": "w",
                "room": "r",
                "source_file": "/x.md",
                "filed_at": "2026-05-21T10:00:00",
                # line_start / line_end intentionally absent
            }
        ]
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r", drawer_metas=metas)
        for line in lines:
            assert len(line.split("|")) == 3, (
                f"meta without line keys should fall back to 3-seg; got {line!r}"
            )

    def test_date_segment_uses_filed_at_date_portion_only(self):
        """The date portion is the YYYY-MM-DD prefix of filed_at, not the
        full ISO timestamp. Closet pointers stay compact and grep-friendly.
        """
        content = "# Topic\n\nDid the work. Reviewed it. Shipped it."
        metas = [
            {
                "wing": "w",
                "room": "r",
                "source_file": "/x.md",
                "filed_at": "2026-05-21T22:30:00.123456+00:00",
                "line_start": 1,
                "line_end": 12,
            }
        ]
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r", drawer_metas=metas)
        for line in lines:
            seg = line.split("|")[2]
            assert seg.startswith("2026-05-21:L"), f"date prefix wrong in {seg!r}"
            assert "T" not in seg, f"raw timestamp leaked into closet pointer: {seg!r}"

    def test_content_date_preferred_over_filed_at(self):
        """When meta carries content_date, the closet pointer's date segment
        reflects content-time (the date the content is FROM), not
        ingestion-time (the date the chunk was mined). Critical for legacy
        content where ingestion-time would be misleading.
        """
        content = "# Topic\n\nDid the work. Reviewed it. Shipped it."
        metas = [
            {
                "wing": "w",
                "room": "r",
                "source_file": "/x.md",
                # filed_at says "we mined this last week"
                "filed_at": "2026-05-21T22:30:00.123456+00:00",
                # content_date says "the content itself is from Nov 8 2024"
                "content_date": "2024-11-08",
                "line_start": 42,
                "line_end": 78,
            }
        ]
        lines = build_closet_lines("/x.md", ["d1"], content, "w", "r", drawer_metas=metas)
        assert lines
        for line in lines:
            parts = line.split("|")
            assert len(parts) == 4, f"expected 4 segments: {line!r}"
            assert parts[2] == "2024-11-08:L42-L78", (
                f"closet date segment did not prefer content_date: {parts[2]!r}"
            )


# ── upsert_closet_lines ───────────────────────────────────────────────


class TestUpsertClosetLines:
    def test_overwrites_existing_closet_does_not_append(self, palace_path):
        col = get_closets_collection(palace_path)
        base = "closet_test_room_abc"
        meta = {"wing": "test", "room": "room", "source_file": "/x.md"}

        upsert_closet_lines(col, base, ["alpha|;|→d1", "beta|;|→d2", "gamma|;|→d3"], meta)
        first = col.get(ids=[f"{base}_01"])
        assert "alpha" in first["documents"][0]

        # Second mine — entirely different lines. Must replace, not append.
        upsert_closet_lines(col, base, ["delta|;|→d4", "epsilon|;|→d5"], meta)
        second = col.get(ids=[f"{base}_01"])
        doc = second["documents"][0]
        assert "delta" in doc
        assert "epsilon" in doc
        assert "alpha" not in doc, "old closet line leaked into rebuild"
        assert "beta" not in doc

    def test_packs_into_multiple_closets_without_splitting_lines(self, palace_path):
        col = get_closets_collection(palace_path)
        base = "closet_pack_room_def"
        meta = {"wing": "test", "room": "room", "source_file": "/y.md"}

        line = "x" * 600  # well under CLOSET_CHAR_LIMIT
        n_written = upsert_closet_lines(col, base, [line, line, line, line], meta)
        # 4 lines @ 601 chars each = 2404 — should pack into 2 closets
        assert n_written == 2

        for i in range(1, n_written + 1):
            doc = col.get(ids=[f"{base}_{i:02d}"])["documents"][0]
            for chunk in doc.split("\n"):
                assert len(chunk) == 600, f"line was truncated in closet {i}"
            assert len(doc) <= CLOSET_CHAR_LIMIT


# ── purge_file_closets ────────────────────────────────────────────────


class TestPurgeFileClosets:
    def test_deletes_only_the_targeted_source(self, palace_path):
        col = get_closets_collection(palace_path)
        col.upsert(
            ids=["closet_a_01", "closet_b_01"],
            documents=["a|;|→d1", "b|;|→d2"],
            metadatas=[
                {"source_file": "/keep.md", "wing": "w", "room": "r"},
                {"source_file": "/drop.md", "wing": "w", "room": "r"},
            ],
        )
        purge_file_closets(col, "/drop.md")
        remaining_ids = set(col.get()["ids"])
        assert "closet_a_01" in remaining_ids
        assert "closet_b_01" not in remaining_ids


# ── project miner: closet rebuild end-to-end ──────────────────────────


class TestMinerClosetRebuild:
    def test_remine_replaces_closets_completely(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / "mempalace.yaml").write_text(
            yaml.dump({"wing": "proj", "rooms": [{"name": "general", "description": "x"}]})
        )
        target = project / "doc.md"

        # First mine — long content produces multiple numbered closets.
        first_topics = "\n\n".join(f"# Topic {i}\n" + ("filler text " * 30) for i in range(15))
        target.write_text(first_topics)
        palace = tmp_path / "palace"
        mine(str(project), str(palace), wing_override="proj", agent="test")

        col = get_closets_collection(str(palace))
        first_pass = col.get(where={"source_file": str(target)})
        assert first_pass["ids"], "first mine should have written closets"
        first_ids = set(first_pass["ids"])
        assert any("topic 0" in (d or "").lower() for d in first_pass["documents"])

        # Touch mtime + shrink content so the rebuild produces fewer closets.
        target.write_text("# Only Topic Now\n" + ("short body " * 5))
        new_mtime = os.path.getmtime(target) + 60
        os.utime(target, (new_mtime, new_mtime))
        time.sleep(0.01)

        mine(str(project), str(palace), wing_override="proj", agent="test")

        col = get_closets_collection(str(palace))
        second_pass = col.get(where={"source_file": str(target)})
        second_docs = "\n".join(second_pass["documents"]).lower()
        assert "only topic now" in second_docs
        for i in range(15):
            assert f"topic {i}\n" not in second_docs, (
                f"stale 'Topic {i}' from first mine survived the rebuild"
            )
        # Numbered closets that existed only in the larger first run must be gone.
        leftover = first_ids - set(second_pass["ids"])
        for stale_id in leftover:
            assert not col.get(ids=[stale_id])["ids"], (
                f"orphan closet {stale_id} from larger first run survived purge"
            )

    def test_production_miner_emits_4_segment_pointers_with_content_date(self, tmp_path):
        """Regression for PR #1584 Igor review Issue #1.

        Before the wiring fix, ``build_closet_lines`` learned the
        ``drawer_metas`` kwarg but no production caller passed it — so the
        Tier 6a 4-segment pointer (``topic|entities|YYYY-MM-DD:Lstart-Lend|
        →drawers``) was emitted by tests only, not by real mines. This
        test pins the end-to-end wiring: a real ``mine()`` of a file with
        a recognizable date in its filename must produce closet documents
        containing the 4-segment pointer with that filename-derived date.
        """
        project = tmp_path / "proj"
        project.mkdir()
        (project / "mempalace.yaml").write_text(
            yaml.dump({"wing": "proj", "rooms": [{"name": "general", "description": "x"}]})
        )
        # Filename carries an unambiguous content date.
        target = project / "2024-11-08-conversation.md"
        target.write_text(
            "# Brands of dog food\n\n"
            "We talked about which brands work best for Osian's dog.\n"
            "Decided to try a few organic options this month.\n" + ("Filler line.\n" * 20)
        )

        palace = tmp_path / "palace"
        mine(str(project), str(palace), wing_override="proj", agent="test")

        col = get_closets_collection(str(palace))
        result = col.get(where={"source_file": str(target)})
        assert result["ids"], "production mine must write closets"

        # At least one closet document must contain the 4-segment Tier 6a
        # pointer for the date this file is from (2024-11-08).
        joined = "\n".join(result["documents"] or [])
        assert "2024-11-08:L" in joined, (
            f"production miner did not emit Tier 6a 4-segment pointer; closet documents: {joined!r}"
        )
        # Each non-empty pointer line carrying a date locator must have 4
        # pipe-separated segments — proving build_closet_lines was called
        # with drawer_metas (regression for Issue #1).
        for doc in result["documents"] or []:
            for line in (doc or "").splitlines():
                if "2024-11-08:L" in line and "→" in line:
                    parts = line.split("|")
                    assert len(parts) == 4, (
                        f"closet line with date locator must be 4 segments, "
                        f"got {len(parts)}: {line!r}"
                    )


# ── _extract_drawer_ids_from_closet ───────────────────────────────────


class TestExtractDrawerIds:
    def test_parses_single_pointer(self):
        assert _extract_drawer_ids_from_closet("topic|;|→drawer_x") == ["drawer_x"]

    def test_parses_multiple_pointers_per_line(self):
        line = "topic|ent|→drawer_a,drawer_b,drawer_c"
        assert _extract_drawer_ids_from_closet(line) == ["drawer_a", "drawer_b", "drawer_c"]

    def test_dedupes_across_lines(self):
        doc = "one|;|→drawer_a,drawer_b\ntwo|;|→drawer_b,drawer_c"
        assert _extract_drawer_ids_from_closet(doc) == ["drawer_a", "drawer_b", "drawer_c"]

    def test_empty_doc_returns_empty(self):
        assert _extract_drawer_ids_from_closet("") == []
        assert _extract_drawer_ids_from_closet("no arrows here") == []


# ── search_memories closet-first path ────────────────────────────────


class TestSearchMemoriesHybrid:
    def test_pure_drawer_when_no_closets(self, palace_path, seeded_collection):
        """Palaces without closets return results via direct drawer search —
        every hit must advertise that the closet signal was absent."""
        result = search_memories("JWT authentication", palace_path)
        assert result["results"], "should still find drawer hits"
        for hit in result["results"]:
            assert hit.get("matched_via") == "drawer"
            assert hit.get("closet_boost") == 0.0
            assert "closet_preview" not in hit

    def test_closet_boost_marks_hit_as_drawer_plus_closet(self, palace_path, seeded_collection):
        """When a closet agrees with direct search on source_file, the
        matching drawer's ``matched_via`` switches to ``drawer+closet`` and
        ``closet_preview`` exposes the hydrated index line."""
        closets = get_closets_collection(palace_path)
        # Seed the closet against the same source_file the drawer uses so
        # the boost lookup keys align.
        closets.upsert(
            ids=["closet_proj_backend_aaa_01"],
            documents=["JWT auth tokens|;|→drawer_proj_backend_aaa"],
            metadatas=[{"wing": "project", "room": "backend", "source_file": "auth.py"}],
        )

        result = search_memories("JWT authentication", palace_path)
        assert result["results"], "hybrid search should still return results"
        # The JWT-bearing drawer should surface with closet agreement.
        boosted = [h for h in result["results"] if h["matched_via"] == "drawer+closet"]
        assert boosted, "closet agreement should promote the matching source"
        top = boosted[0]
        assert "JWT" in top["text"]
        assert top["closet_boost"] > 0
        assert "→drawer_proj_backend_aaa" in top["closet_preview"]

    def test_max_distance_filters_hybrid_hits(self, palace_path, seeded_collection):
        closets = get_closets_collection(palace_path)
        closets.upsert(
            ids=["closet_proj_backend_aaa_01"],
            documents=["JWT auth tokens|;|→drawer_proj_backend_aaa"],
            metadatas=[{"wing": "project", "room": "backend", "source_file": "auth.py"}],
        )
        result = search_memories(
            "completely unrelated query about quantum gardening",
            palace_path,
            max_distance=0.001,
        )
        for hit in result["results"]:
            assert hit["distance"] <= 0.001


# ── entity metadata ──────────────────────────────────────────────────


class TestEntityMetadata:
    def test_extracts_capitalized_names(self):
        text = "Ben reviewed the code. Ben approved it. Igor flagged two issues. Igor fixed them."
        entities = _extract_entities_for_metadata(text)
        assert "Ben" in entities
        assert "Igor" in entities

    def test_empty_for_no_entities(self):
        text = "this is all lowercase with no proper nouns at all"
        assert _extract_entities_for_metadata(text) == ""

    def test_semicolon_separated(self):
        text = "Alice and Bob met Charlie. Alice said hello. Bob agreed. Charlie laughed."
        entities = _extract_entities_for_metadata(text)
        assert ";" in entities

    def test_stoplist_filters_sentence_starters(self):
        # Same regression as the closet entity test — "When/After/The" must
        # not become entities just because they're capitalized 2+ times.
        text = (
            "When the build broke, the team paged. "
            "When the fix landed, the alarm cleared. "
            "After the rollback, the queue drained. "
            "After the deploy, the latency normalized."
        )
        entities = _extract_entities_for_metadata(text)
        tokens = set(entities.split(";")) if entities else set()
        assert "When" not in tokens
        assert "After" not in tokens
        assert "The" not in tokens

    def test_capped_list_never_truncates_a_name(self):
        # 30 distinct repeated proper nouns — extraction should cap the list
        # before joining so a name never gets cut in half.
        # Use morphologically distinct stems so the [A-Z][a-z]+ regex sees
        # each as its own token.
        names = [
            "Anna",
            "Brian",
            "Carol",
            "David",
            "Elena",
            "Frank",
            "Grace",
            "Harold",
            "Iris",
            "Julian",
            "Kira",
            "Liam",
            "Maya",
            "Noah",
            "Oscar",
            "Penny",
            "Quinn",
            "Rosa",
            "Sergei",
            "Tara",
            "Umar",
            "Vera",
            "Walter",
            "Xander",
            "Yvonne",
            "Zachary",
            "Amelia",
            "Boris",
            "Clara",
            "Dmitri",
        ]
        text = " ".join(f"{n} met {n}." for n in names)
        entities = _extract_entities_for_metadata(text)
        extracted = [n for n in entities.split(";") if n]
        assert extracted, "should have extracted some entities"
        for name in extracted:
            assert name in names, f"truncation produced a partial token: {name!r}"

    def test_known_registry_is_cached_by_mtime(self, monkeypatch, tmp_path):
        # Point the registry at a temp file we control, exercise the cache.
        registry = tmp_path / "known_entities.json"
        registry.write_text(json.dumps({"people": ["Zelda"]}))
        from mempalace import miner

        monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
        miner._ENTITY_REGISTRY_CACHE["mtime"] = None
        miner._ENTITY_REGISTRY_CACHE["names"] = frozenset()

        first = _load_known_entities()
        assert "Zelda" in first

        # Second call without changing mtime: must reuse cache, not re-read.
        read_count = {"n": 0}
        original_open = open

        def counting_open(path, *a, **kw):
            if str(path) == str(registry):
                read_count["n"] += 1
            return original_open(path, *a, **kw)

        monkeypatch.setattr("builtins.open", counting_open)
        _load_known_entities()
        assert read_count["n"] == 0, "registry should not be re-read when mtime unchanged"

        # Bump mtime → cache must invalidate.
        new_mtime = os.path.getmtime(registry) + 5
        os.utime(registry, (new_mtime, new_mtime))
        registry.write_text(json.dumps({"people": ["Zelda", "Link"]}))
        os.utime(registry, (new_mtime, new_mtime))
        names = _load_known_entities()
        assert "Link" in names


# ── BM25 hybrid search (real IDF over candidate corpus) ──────────────


class TestBM25:
    def test_scores_positive_for_matching_doc(self):
        scores = _bm25_scores(
            "database migration",
            ["We migrated the database to Postgres.", "unrelated cookery tips"],
        )
        assert scores[0] > 0
        assert scores[1] == 0.0

    def test_scores_zero_when_no_overlap(self):
        scores = _bm25_scores("quantum physics", ["We built a web app in React"])
        assert scores == [0.0]

    def test_idf_downweights_terms_present_in_every_doc(self):
        # "database" appears in every candidate → low IDF → low contribution.
        # "vacuum" is unique to one → high IDF → that doc dominates.
        scores = _bm25_scores(
            "database vacuum",
            [
                "database backup nightly schedule",
                "database vacuum scheduled weekly",
                "database failover plan",
            ],
        )
        assert scores[1] == max(scores), "doc with the rare query term should win on IDF"

    def test_empty_inputs_return_zeros(self):
        assert _bm25_scores("", ["hello world"]) == [0.0]
        assert _bm25_scores("query here", []) == []
        assert _bm25_scores("query", [""]) == [0.0]

    def test_hybrid_rank_promotes_keyword_match(self):
        results = [
            {"text": "database schema design for Postgres", "distance": 0.5},
            {"text": "unrelated topic about cooking", "distance": 0.3},
        ]
        ranked = _hybrid_rank(results, "database Postgres schema")
        # The keyword-rich result outranks the closer-vector but irrelevant one.
        assert "database" in ranked[0]["text"]
        # bm25_score field is exposed for debugging.
        assert "bm25_score" in ranked[0]
        # No internal scoring leak.
        assert "_hybrid_score" not in ranked[0]

    def test_hybrid_rank_absolute_normalization(self):
        # Adding a much-worse result to the candidate set must NOT reshuffle
        # the top two — proves we're using absolute (1 - dist) and not
        # dist / max_dist normalization.
        base = [
            {"text": "alpha alpha alpha", "distance": 0.1},
            {"text": "beta beta beta", "distance": 0.4},
        ]
        ranked_short = _hybrid_rank([dict(r) for r in base], "alpha")
        with_outlier = base + [{"text": "gamma gamma gamma", "distance": 1.9}]
        ranked_long = _hybrid_rank([dict(r) for r in with_outlier], "alpha")
        assert ranked_short[0]["text"] == ranked_long[0]["text"]
        assert ranked_short[1]["text"] == ranked_long[1]["text"]


# ── diary ingest ─────────────────────────────────────────────────────


class TestDiaryIngest:
    def test_ingest_creates_drawers_and_closets(self, tmp_path):
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n## 10:00 PDT — Test\n\nBuilt the auth system.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        result = ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        assert result["days_updated"] >= 1
        assert get_collection(str(palace_dir)).count() >= 1

    def test_ingest_skips_unchanged_on_second_run(self, tmp_path):
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n## 10:00 — Test\n\nContent here that's long enough.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        result = ingest_diaries(str(diary_dir), str(palace_dir))
        assert result["days_updated"] == 0

    def test_ingest_detects_same_size_content_edit(self, tmp_path):
        # Regression #925: the prior skip-check compared byte length only, so
        # any in-place edit preserving total length (typo fix "teh"→"the",
        # word swap, character reorder) was silently dropped. Content-hash
        # check must catch the change AND rebuild the searchable closet so
        # the index does not stay stale while the drawer updates.
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        diary_file = diary_dir / "2026-04-13.md"
        # Original has the typo "Teh"; the edit fixes it to "The" — same length.
        original = "# 2026-04-13\n\n## 10:00 — Test\n\nTeh elaborate jakarta postgres bug.\n"
        edited = "# 2026-04-13\n\n## 10:00 — Test\n\nThe elaborate jakarta postgres bug.\n"
        assert len(original) == len(edited), "test setup: edited content must be same length"
        diary_file.write_text(original)
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        diary_file.write_text(edited)
        result = ingest_diaries(str(diary_dir), str(palace_dir))
        assert result["days_updated"] == 1, "same-size content edit must trigger re-ingest"

        # Drawer must hold the corrected text.
        drawers = get_collection(str(palace_dir)).get(where={"source_file": str(diary_file)})
        joined_drawers = "\n".join(drawers["documents"])
        assert "The elaborate" in joined_drawers
        assert "Teh elaborate" not in joined_drawers, "drawer still holds pre-edit content"

        # And the closet (search index) must reflect the edit too — not just the
        # drawer. Otherwise searches would surface stale text.
        closets = get_closets_collection(str(palace_dir)).get(
            where={"source_file": str(diary_file)}
        )
        joined_closets = "\n".join(closets["documents"])
        assert "Teh elaborate" not in joined_closets, "closet index still holds stale content"

    def test_legacy_state_backfills_content_hash(self, tmp_path):
        # Upgraded users can carry legacy state entries without ``content_hash``.
        # Same-size skip is preserved for that one run, but the hash must be
        # recorded so the strict check engages on subsequent runs.
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        diary_file = diary_dir / "2026-04-13.md"
        # Write explicit UTF-8 so the round-trip matches how diary_ingest reads.
        # Windows' default text-mode encoding is cp1252; without this the em
        # dash would round-trip lossy and the hash assertion below would fail.
        text = "# 2026-04-13\n\n## 10:00 — Test\n\nUnchanged body content here.\n"
        diary_file.write_text(text, encoding="utf-8")
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import _state_file_for, ingest_diaries

        # Simulate a legacy state file: only size + entry_count, no content_hash.
        state_file = _state_file_for(str(palace_dir), diary_dir.resolve())
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps(
                {
                    f"diary|{diary_file.name}": {
                        "size": len(text),
                        "entry_count": 1,
                        "ingested_at": "2026-04-12T00:00:00+00:00",
                    }
                }
            )
        )

        # Run with no force — size matches, so this should skip ingest.
        result = ingest_diaries(str(diary_dir), str(palace_dir))
        assert result["days_updated"] == 0

        # Hash must have been backfilled into state for the next run's strict check.
        persisted = json.loads(state_file.read_text())
        entry = persisted[f"diary|{diary_file.name}"]
        assert "content_hash" in entry, "legacy skip path must record the hash"
        assert entry["content_hash"] == hashlib.sha256(text.encode("utf-8")).hexdigest()

    def test_state_file_lives_outside_diary_dir(self, tmp_path):
        # Regression: the original implementation wrote
        # ``.diary_ingest_state.json`` *inside* the user's diary directory,
        # polluting their content folder. State must live under
        # ``~/.mempalace/state/`` instead.
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n## 10:00 — Test\n\nBody content here long enough.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import _state_file_for, ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)

        # No state file inside the user's diary dir.
        for entry in diary_dir.iterdir():
            assert "diary_ingest" not in entry.name, (
                f"state file leaked into user diary dir: {entry}"
            )

        # State file does exist under ~/.mempalace/state/.
        state_path = _state_file_for(str(palace_dir), diary_dir.resolve())
        assert state_path.exists()
        # Platform-neutral path check: compare parents rather than a hardcoded
        # separator string that would fail on Windows (``\.mempalace\state\``).
        assert state_path.parent.name == "state"
        assert state_path.parent.parent.name == ".mempalace"

    def test_wing_prefixed_drawer_id_prevents_cross_diary_collision(self, tmp_path):
        # Regression: the original implementation used
        # ``drawer_diary_{date_str}`` regardless of wing — two diaries with
        # the same date in different wings would clobber each other.
        date_md = "# 2026-04-13\n\n## 10:00 — entry\n\nThis is the day's content.\n"

        # Two separate diary dirs, ingested into the same palace under
        # different wings. Each must produce a distinct drawer.
        personal_dir = tmp_path / "personal"
        personal_dir.mkdir()
        (personal_dir / "2026-04-13.md").write_text(date_md + "Personal-only marker.\n")

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "2026-04-13.md").write_text(date_md + "Work-only marker.\n")

        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import _diary_drawer_id_entry, ingest_diaries

        ingest_diaries(str(personal_dir), str(palace_dir), wing="personal", force=True)
        ingest_diaries(str(work_dir), str(palace_dir), wing="work", force=True)

        col = get_collection(str(palace_dir))
        # Post-#1539: per-entry drawers. Each single-entry diary produces
        # one drawer keyed by (wing, date, entry_idx=0, chunk_idx=0). The
        # wing component still keeps work vs personal collisions apart.
        personal_id = _diary_drawer_id_entry("personal", "2026-04-13", 0, 0)
        work_id = _diary_drawer_id_entry("work", "2026-04-13", 0, 0)
        assert personal_id != work_id

        personal = col.get(ids=[personal_id])
        work = col.get(ids=[work_id])
        assert personal["ids"] == [personal_id]
        assert work["ids"] == [work_id]
        assert "Personal-only marker." in personal["documents"][0]
        assert "Work-only marker." in work["documents"][0]

    # ── #1539: per-entry drawers, oversized-entry chunking ─────────

    def test_diary_multiple_entries_one_drawer_each(self, tmp_path):
        """Regression for #1539: each ``##`` entry must become its own
        drawer rather than the whole file being one document. Pre-fix
        behaviour: a single file-level drawer for any number of entries.
        Post-fix: one drawer per entry."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n"
            "## 10:00 — first\n\nfirst body content here, well above min size.\n\n"
            "## 11:00 — second\n\nsecond body content with enough length here.\n\n"
            "## 12:00 — third\n\nthird body content with sufficient text in it.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        count = get_collection(str(palace_dir)).count()
        assert count == 3, f"expected one drawer per ## entry (3 entries); got {count} drawers"

    def test_large_diary_with_oversized_entry_chunks_within_entry(self, tmp_path):
        """Regression for #1539: when a single ``##`` entry exceeds
        chunk_size, the ingester must chunk that entry into bounded
        drawers rather than uploading the whole entry as one oversized
        document (which crashes the embedding model in production)."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        # Middle entry body is 1500 chars — above CHUNK_SIZE=800 but below
        # the embedding model's hard token limit. This trips the size-cap
        # assertion on develop without crashing the test infrastructure.
        big_body = "X" * 1500
        (diary_dir / "2026-04-13.md").write_text(
            f"# 2026-04-13\n\n"
            f"## 10:00 — first\n\nfirst body content here, well above min size.\n\n"
            f"## 11:00 — big\n\n{big_body}\n\n"
            f"## 12:00 — last\n\nlast body content with sufficient text in it.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        col = get_collection(str(palace_dir))
        drawers = col.get()
        docs = drawers["documents"]

        max_len = max(len(d) for d in docs)
        assert max_len <= 800, (
            f"no drawer document may exceed CHUNK_SIZE=800; got max_len={max_len}"
        )
        # 3 entries with the middle entry split into >= 2 chunks → >= 4 drawers
        assert len(docs) >= 4, (
            f"oversized middle entry must produce multiple drawers; got {len(docs)} total drawers"
        )

    def test_incremental_appends_new_entry_only(self, tmp_path):
        """Regression for #1539: incremental ingest must add exactly the
        delta when one new entry is appended (not re-rewrite all)."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        diary_file = diary_dir / "2026-04-13.md"
        diary_file.write_text(
            "# 2026-04-13\n\n"
            "## 10:00 — first\n\nfirst body content, sufficiently long.\n\n"
            "## 11:00 — second\n\nsecond body content with text in it.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        initial = get_collection(str(palace_dir)).count()
        assert initial == 2, f"baseline: 2 entries → 2 drawers; got {initial}"

        diary_file.write_text(
            diary_file.read_text()
            + "\n## 12:00 — third\n\nthird body, newly added on second run.\n"
        )
        ingest_diaries(str(diary_dir), str(palace_dir))
        final = get_collection(str(palace_dir)).count()
        assert final == 3, f"after appending 1 entry: 3 drawers total; got {final}"

    def test_entry_count_watermark_matches_drawer_count(self, tmp_path):
        """Regression for #1539: persisted ``entry_count`` watermark
        must equal the number of entries split from the file (and
        therefore the number of drawers under the new schema)."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n"
            "## 10:00 — a\n\nbody one with enough text to count.\n\n"
            "## 11:00 — b\n\nbody two with enough text to count.\n\n"
            "## 12:00 — c\n\nbody three with enough text to count.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import _state_file_for, _split_entries, ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)

        state = json.loads(_state_file_for(str(palace_dir), diary_dir.resolve()).read_text())
        key = "diary|2026-04-13.md"
        assert state[key]["entry_count"] == 3, (
            f"watermark must equal entry count; got {state[key]['entry_count']}"
        )

        text = (diary_dir / "2026-04-13.md").read_text()
        assert len(_split_entries(text)) == state[key]["entry_count"]

    def test_diary_chunk_index_is_global_across_entries(self, tmp_path):
        """Regression for #1539 review: ``chunk_index`` must be a global
        counter across the file (not per-entry) so the searcher's
        ``_expand_with_neighbors`` (which queries by source_file +
        chunk_index range) can stitch sibling chunks regardless of
        entry boundary. Pre-fix the metadata key was ``entry_chunk_index``
        and search neighbor expansion silently fell back to the matched
        drawer alone for any diary hit."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        # 3 entries; middle is oversized (1500 chars > CHUNK_SIZE=800).
        big_body = "X" * 1500
        (diary_dir / "2026-04-13.md").write_text(
            f"# 2026-04-13\n\n"
            f"## 10:00 — a\n\nfirst body content with enough length here.\n\n"
            f"## 11:00 — b\n\n{big_body}\n\n"
            f"## 12:00 — c\n\nthird body content with enough length here.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        col = get_collection(str(palace_dir))
        all_drawers = col.get()

        indices = sorted(m["chunk_index"] for m in all_drawers["metadatas"])
        # Global counter: 0, 1, 2, ... contiguous across all entries.
        assert indices == list(range(len(indices))), (
            f"chunk_index must be contiguous 0..N-1 globally; got {indices}"
        )
        # All chunks have the same source_file (filter target for neighbor
        # expansion). Verify the searcher-required pair (source_file +
        # chunk_index) is consistent on every drawer.
        sources = {m["source_file"] for m in all_drawers["metadatas"]}
        assert len(sources) == 1, f"all drawers must share one source_file; got {sources}"

    def test_diary_entry_deletion_purges_orphan_drawers(self, tmp_path):
        """Regression for #1539 review: if a diary shrinks (entries
        deleted), the full-rebuild step must purge prior-pass drawers
        for that ``source_file`` before re-writing — otherwise trailing
        drawers from the longer prior pass remain as orphans and
        pollute search results forever."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        diary_file = diary_dir / "2026-04-13.md"
        diary_file.write_text(
            "# 2026-04-13\n\n"
            "## 10:00 — first\n\nfirst body content with enough length here.\n\n"
            "## 11:00 — second\n\nsecond body content with enough length here.\n\n"
            "## 12:00 — third\n\nthird body content with enough length here.\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        col = get_collection(str(palace_dir))
        assert col.count() == 3, f"baseline: 3 entries → 3 drawers; got {col.count()}"

        # Shrink the file to 1 entry. Hash changes → content_changed=True
        # → full_rebuild=True → purge step must remove the trailing 2 drawers.
        diary_file.write_text(
            "# 2026-04-13\n\n## 10:00 — first\n\nfirst body content with enough length here.\n"
        )
        ingest_diaries(str(diary_dir), str(palace_dir))
        final = col.count()
        assert final == 1, (
            f"after shrinking 3 entries → 1 entry: exactly 1 drawer; "
            f"got {final} (orphans from prior pass not purged)"
        )

    def test_diary_header_only_entry_produces_drawer(self, tmp_path):
        """A ``##`` header with no body still produces a drawer carrying
        the header text. Catches a regression where ``if body`` would
        collapse the entry."""
        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-04-13.md").write_text(
            "# 2026-04-13\n\n"
            "## 10:00 — note one with sufficient length to pass min size\n\n"
            "## 11:00 — note two with sufficient length to pass min size\n"
        )
        palace_dir = tmp_path / "palace"

        from mempalace.diary_ingest import ingest_diaries

        ingest_diaries(str(diary_dir), str(palace_dir), force=True)
        col = get_collection(str(palace_dir))
        assert col.count() == 2, (
            f"expected 2 drawers (one per header-only entry); got {col.count()}"
        )
        docs = col.get()["documents"]
        # Each drawer holds the header line itself.
        joined = "\n".join(docs)
        assert "note one" in joined
        assert "note two" in joined

    def test_diary_atomic_batched_upsert_on_failure(self, tmp_path, monkeypatch):
        """A mid-pass embedding failure must leave the collection empty
        rather than half-written. Without batched-atomic upsert, an
        earlier per-loop upsert would partially commit and the state
        file would skip the file on re-run, silently losing entries.

        The fixture mixes normal entries with one oversized entry so
        the failed batch exercises both the single-entry-per-drawer
        branch and the per-entry character-chunk fallback."""
        from mempalace import diary_ingest

        diary_dir = tmp_path / "diaries"
        diary_dir.mkdir()
        (diary_dir / "2026-05-18.md").write_text(
            "# 2026-05-18\n\n"
            "## 10:00 — entry one with sufficient body to clear min size threshold\n"
            "More body text for entry one with enough length.\n\n"
            "## 11:00 — entry two with oversized body that forces per-entry chunking\n"
            + "A"
            * 3000
            + "\n\n"
            "## 12:00 — entry three with body added for adequate test fixture size\n"
            "More body text for entry three to make it realistic.\n"
        )
        palace_dir = tmp_path / "palace"

        class _RaisingCollection:
            def __init__(self, real):
                self._real = real
                self.upsert_call_count = 0

            def __getattr__(self, name):
                return getattr(self._real, name)

            def upsert(self, **kwargs):
                self.upsert_call_count += 1
                raise RuntimeError("simulated embedding model failure")

        real_get_collection = diary_ingest.get_collection
        raising_holder = {}

        def _wrap_get_collection(path):
            real = real_get_collection(path)
            wrapped = _RaisingCollection(real)
            raising_holder["col"] = wrapped
            return wrapped

        monkeypatch.setattr(diary_ingest, "get_collection", _wrap_get_collection)

        with pytest.raises(RuntimeError, match="simulated embedding model failure"):
            diary_ingest.ingest_diaries(str(diary_dir), str(palace_dir), force=True)

        # Exactly one batched upsert attempt — not three per-loop calls.
        # Fixture has a single diary file, so one batch per file == one
        # total. With a multi-file fixture this would be N batches.
        assert raising_holder["col"].upsert_call_count == 1, (
            "diary ingest must accumulate one batch per file; "
            f"got {raising_holder['col'].upsert_call_count} upsert calls"
        )

        # Re-open the real collection (untouched by the failed write) and
        # confirm nothing landed for this source file.
        real_col = real_get_collection(str(palace_dir))
        rows = real_col.get(where={"source_file": str(diary_dir / "2026-05-18.md")})
        assert rows["ids"] == [], (
            f"failed batched upsert must leave no partial drawers; got {rows['ids']}"
        )


# ── cross-wing tunnels ───────────────────────────────────────────────


class TestTunnels:
    """Tunnels are explicit cross-wing connections stored in
    ``<palace_path_parent>/tunnels.json``. Each test points the resolver at
    a fresh tmp file so tests don't cross-contaminate or touch the user's
    real tunnels."""

    def setup_method(self):
        import mempalace.palace_graph as pg

        self._pg = pg
        self._orig_get = pg._get_tunnel_file
        self._orig_legacy = pg._legacy_tunnel_file
        self._tmpdir = tempfile.mkdtemp()
        self._tunnel_path = os.path.join(self._tmpdir, "tunnels.json")
        pg._get_tunnel_file = lambda *a, **kw: self._tunnel_path
        pg._legacy_tunnel_file = lambda: self._tunnel_path + ".legacy"
        # Neutralize the endpoint-existence check added for #1468 so these
        # legacy tests (which predate validation) don't get rejected when
        # an earlier test module has bound _get_collection to a real backend.
        self._orig_get_col = pg._get_collection
        pg._get_collection = lambda *a, **kw: None

    def teardown_method(self):
        self._pg._get_tunnel_file = self._orig_get
        self._pg._legacy_tunnel_file = self._orig_legacy
        self._pg._get_collection = self._orig_get_col
        import shutil

        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_create_tunnel(self):
        t = create_tunnel("wing_api", "auth", "wing_db", "users", label="auth uses users table")
        assert t["id"]
        assert t["source"]["wing"] == "wing_api"
        assert t["source"]["room"] == "auth"
        assert t["target"]["wing"] == "wing_db"
        assert t["target"]["room"] == "users"
        assert t["label"] == "auth uses users table"

    def test_list_tunnels_with_and_without_filter(self):
        create_tunnel("wing_a", "room1", "wing_b", "room2")
        create_tunnel("wing_a", "room3", "wing_c", "room4")
        assert len(list_tunnels()) == 2
        # Filtering by a wing that appears on either endpoint.
        assert len(list_tunnels("wing_a")) == 2
        assert len(list_tunnels("wing_c")) == 1
        assert len(list_tunnels("wing_nonexistent")) == 0

    def test_delete_tunnel(self):
        t = create_tunnel("wing_x", "r1", "wing_y", "r2")
        delete_tunnel(t["id"])
        assert list_tunnels() == []

    def test_dedup_same_endpoints_updates_label(self):
        create_tunnel("wing_a", "r1", "wing_b", "r2", label="first")
        create_tunnel("wing_a", "r1", "wing_b", "r2", label="updated")
        tunnels = list_tunnels()
        assert len(tunnels) == 1
        assert tunnels[0]["label"] == "updated"

    def test_follow_tunnels_returns_connected_endpoints(self):
        create_tunnel("wing_api", "auth", "wing_db", "users")
        create_tunnel("wing_api", "auth", "wing_frontend", "login")
        # Unrelated tunnel that must not surface.
        create_tunnel("wing_other", "notes", "wing_misc", "scratch")

        connections = follow_tunnels("wing_api", "auth")
        assert len(connections) == 2
        wings = {c["connected_wing"] for c in connections}
        assert wings == {"wing_db", "wing_frontend"}

    # ── regression: symmetry, durability, validation, concurrency ─────

    def test_tunnel_is_symmetric(self):
        """Regression: tunnels are undirected. create(A, B) and create(B, A)
        must resolve to the same canonical ID and dedupe into one record —
        the second call updates the label instead of creating a dupe."""
        first = create_tunnel("wing_a", "r1", "wing_b", "r2", label="forward")
        second = create_tunnel("wing_b", "r2", "wing_a", "r1", label="reversed")
        assert first["id"] == second["id"]
        assert len(list_tunnels()) == 1
        assert list_tunnels()[0]["label"] == "reversed"

    def test_follow_tunnels_works_from_either_endpoint(self):
        """Symmetric: you can follow_tunnels from either end of the link."""
        create_tunnel("wing_api", "auth", "wing_db", "users", label="auth uses users")
        from_source = follow_tunnels("wing_api", "auth")
        from_target = follow_tunnels("wing_db", "users")
        assert len(from_source) == 1
        assert len(from_target) == 1
        assert from_source[0]["connected_wing"] == "wing_db"
        assert from_target[0]["connected_wing"] == "wing_api"
        # Both surfaces should carry the same label.
        assert from_source[0]["label"] == "auth uses users"
        assert from_target[0]["label"] == "auth uses users"

    def test_empty_endpoint_fields_rejected(self):
        """Regression: create_tunnel must reject empty strings on any
        endpoint field so the JSON store can't grow phantom tunnels."""
        import pytest

        for args in [
            ("", "r1", "wing", "r2"),
            ("wing", "", "wing", "r2"),
            ("wing", "r1", "", "r2"),
            ("wing", "r1", "wing", ""),
            ("   ", "r1", "wing", "r2"),  # whitespace-only also rejected
        ]:
            with pytest.raises(ValueError):
                create_tunnel(*args)

    def test_corrupt_tunnel_file_does_not_lose_new_writes(self):
        """A truncated/corrupt tunnels.json (crash mid-write on a system
        without atomic rename) must not leak into subsequent reads — the
        file should be treated as empty and a fresh create_tunnel should
        persist cleanly."""
        import mempalace.palace_graph as pg

        # Simulate a crash that left a truncated file behind.
        with open(pg._get_tunnel_file(), "w") as f:
            f.write("{not valid json")

        # Load should return [] rather than raising.
        assert list_tunnels() == []

        # A subsequent create must persist (atomic write replaces the corrupt file).
        t = create_tunnel("wing_a", "r1", "wing_b", "r2")
        assert list_tunnels() == [t]

    def test_atomic_write_leaves_no_stray_tmp_file(self):
        """Regression: _save_tunnels uses write-then-os.replace. After a
        successful create, there must be no leftover ``tunnels.json.tmp``."""
        import mempalace.palace_graph as pg

        create_tunnel("wing_a", "r1", "wing_b", "r2")
        assert os.path.exists(pg._get_tunnel_file())
        assert not os.path.exists(pg._get_tunnel_file() + ".tmp")

    def test_concurrent_creates_preserve_all_tunnels(self):
        """Regression: two concurrent create_tunnel calls must not clobber
        each other. Without the mine_lock around load+save, the later
        writer's snapshot would overwrite the earlier writer's tunnel."""
        barrier = threading.Barrier(5)
        errors: list = []

        def worker(i):
            try:
                barrier.wait(timeout=2)
                create_tunnel(f"wing_{i}", "r", "wing_shared", "hub")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"worker raised: {errors}"
        tunnels = list_tunnels()
        assert len(tunnels) == 5, (
            f"expected 5 concurrent tunnels, got {len(tunnels)} — write race dropped some"
        )

    def test_created_at_is_timezone_aware(self):
        """Regression: created_at must be tz-aware UTC, not naive."""
        t = create_tunnel("wing_a", "r1", "wing_b", "r2")
        # ISO format with tz offset contains '+' or 'Z'.
        assert t["created_at"].endswith("+00:00") or t["created_at"].endswith("Z")


# ── drawer-grep neighbor expansion ────────────────────────────────────
#
# When a closet hit lands on a drawer whose chunk boundary clips a thought
# (matched chunk says "here's a breakdown:" and the breakdown lives in the
# next chunk), the closet path now expands to ±1 neighbor chunks from the
# same source file. These tests pin that behavior end-to-end and at the
# helper level.


class TestDrawerGrepExpansion:
    def _seed_source_file(self, palace_path, source: str, n_chunks: int):
        """Helper: put N sequential drawers for a single source file into
        the palace and return the drawer IDs keyed by chunk_index."""
        col = get_collection(palace_path)
        ids = [f"drawer_test_room_{source.replace('/', '_')}_{i:03d}" for i in range(n_chunks)]
        docs = [f"chunk_{i} content about topic alpha" for i in range(n_chunks)]
        metas = [
            {
                "wing": "test",
                "room": "room",
                "source_file": source,
                "chunk_index": i,
                "filed_at": "2026-04-13T00:00:00",
            }
            for i in range(n_chunks)
        ]
        col.upsert(ids=ids, documents=docs, metadatas=metas)
        return col, {i: ids[i] for i in range(n_chunks)}

    def test_expand_returns_matched_plus_neighbors(self, palace_path):
        col, by_idx = self._seed_source_file(palace_path, "/proj/doc.md", n_chunks=5)
        matched_meta = {"source_file": "/proj/doc.md", "chunk_index": 2}
        matched_doc = "chunk_2 content about topic alpha"

        out = _expand_with_neighbors(col, matched_doc, matched_meta, radius=1)
        assert out["drawer_index"] == 2
        assert out["total_drawers"] == 5
        # Expect chunks 1, 2, 3 joined in chunk_index order.
        text = out["text"]
        assert "chunk_1" in text
        assert "chunk_2" in text
        assert "chunk_3" in text
        # No leakage of non-neighbors.
        assert "chunk_0" not in text
        assert "chunk_4" not in text
        # Ordering preserved — chunk_1 before chunk_2 before chunk_3.
        assert text.index("chunk_1") < text.index("chunk_2") < text.index("chunk_3")

    def test_expand_at_start_of_file_only_has_next_neighbor(self, palace_path):
        col, _ = self._seed_source_file(palace_path, "/proj/edge_start.md", n_chunks=3)
        out = _expand_with_neighbors(
            col,
            "chunk_0 content",
            {"source_file": "/proj/edge_start.md", "chunk_index": 0},
        )
        assert out["drawer_index"] == 0
        assert out["total_drawers"] == 3
        assert "chunk_0" in out["text"]
        assert "chunk_1" in out["text"]
        # No chunk_-1 could exist; the expansion must not invent one.
        assert "chunk_-1" not in out["text"]

    def test_expand_at_end_of_file_only_has_prev_neighbor(self, palace_path):
        col, _ = self._seed_source_file(palace_path, "/proj/edge_end.md", n_chunks=3)
        out = _expand_with_neighbors(
            col,
            "chunk_2 content",
            {"source_file": "/proj/edge_end.md", "chunk_index": 2},
        )
        assert out["drawer_index"] == 2
        assert out["total_drawers"] == 3
        assert "chunk_1" in out["text"]
        assert "chunk_2" in out["text"]
        # No chunk_3 exists.
        assert "chunk_3" not in out["text"]

    def test_expand_single_drawer_file_returns_just_matched(self, palace_path):
        col, _ = self._seed_source_file(palace_path, "/proj/lone.md", n_chunks=1)
        out = _expand_with_neighbors(
            col,
            "chunk_0 content",
            {"source_file": "/proj/lone.md", "chunk_index": 0},
        )
        assert out["drawer_index"] == 0
        assert out["total_drawers"] == 1
        assert out["text"] == "chunk_0 content about topic alpha"

    def test_expand_falls_back_when_metadata_missing(self, palace_path):
        col = get_collection(palace_path)
        # No source_file / chunk_index in meta — degrade gracefully.
        out = _expand_with_neighbors(col, "matched doc", {})
        assert out["text"] == "matched doc"
        assert out["drawer_index"] is None
        assert out["total_drawers"] is None

    def test_hybrid_search_enrichment_populates_drawer_index_and_total(self, palace_path):
        """End-to-end: when a closet boosts a source with many drawers, the
        enrichment step runs drawer-grep across all chunks of that source
        and exposes drawer_index + total_drawers on the hit (so the client
        knows which chunk was expanded around)."""
        col = get_collection(palace_path)
        source = "/proj/indexed.md"
        # Seed 5 drawers for one source file.
        for i in range(5):
            col.upsert(
                ids=[f"drawer_proj_backend_indexed_{i:03d}"],
                documents=[f"chunk_{i} talks about JWT authentication flow"],
                metadatas=[
                    {
                        "wing": "project",
                        "room": "backend",
                        "source_file": source,
                        "chunk_index": i,
                        "filed_at": "2026-04-13T00:00:00",
                    }
                ],
            )
        # Closet pointing at chunk_2 for this source.
        closets = get_closets_collection(palace_path)
        closets.upsert(
            ids=["closet_proj_backend_indexed_01"],
            documents=["JWT auth|;|→drawer_proj_backend_indexed_002"],
            metadatas=[{"wing": "project", "room": "backend", "source_file": source}],
        )

        result = search_memories("JWT authentication", palace_path)
        assert result["results"]
        # The hybrid path promotes the closet-agreeing source to drawer+closet.
        boosted = [h for h in result["results"] if h["matched_via"] == "drawer+closet"]
        assert boosted, "hybrid search should mark the closet-agreeing source"
        top = boosted[0]
        assert top["total_drawers"] == 5
        assert isinstance(top["drawer_index"], int)
        # Enriched text must include the grep-best chunk plus one neighbor
        # on each side (chunk boundary may clip).
        assert "chunk_" in top["text"]

    def test_expand_isolates_chunks_by_parent_drawer_id_when_source_file_shared(self, palace_path):
        """Regression for #1580. After #1539 the chunked ``tool_add_drawer``
        path stores per-chunk drawers tagged with a ``parent_drawer_id``
        linking them to the logical group. If two unrelated logical
        drawers happen to share the same ``source_file`` (e.g. two pastes
        labelled ``source_file="chat.log"``), filtering only by
        ``source_file + chunk_index`` pulls chunks from both groups as if
        they were sequential neighbors, corrupting the enriched text.
        Scoping by ``parent_drawer_id`` when present keeps each logical
        group isolated. (``tool_diary_write`` chunks tag a different key
        (``parent_entry_id``) and are written without ``source_file``, so
        they never reach this enrichment path.)
        """
        col = get_collection(palace_path)
        source = "shared.log"
        # Group A: 2 chunks under parent_drawer_id="drawer_A".
        col.upsert(
            ids=["drawer_A_chunk_000000", "drawer_A_chunk_000001"],
            documents=["alpha-A-chunk-0 content", "alpha-A-chunk-1 content"],
            metadatas=[
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": 0,
                    "parent_drawer_id": "drawer_A",
                    "filed_at": "2026-04-13T00:00:00",
                },
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": 1,
                    "parent_drawer_id": "drawer_A",
                    "filed_at": "2026-04-13T00:00:00",
                },
            ],
        )
        # Group B: 2 chunks under the SAME source_file but a different
        # parent_drawer_id. Chunk indices intentionally collide with A.
        col.upsert(
            ids=["drawer_B_chunk_000000", "drawer_B_chunk_000001"],
            documents=["bravo-B-chunk-0 content", "bravo-B-chunk-1 content"],
            metadatas=[
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": 0,
                    "parent_drawer_id": "drawer_B",
                    "filed_at": "2026-04-13T00:00:00",
                },
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": 1,
                    "parent_drawer_id": "drawer_B",
                    "filed_at": "2026-04-13T00:00:00",
                },
            ],
        )

        matched_doc = "alpha-A-chunk-0 content"
        matched_meta = {
            "source_file": source,
            "chunk_index": 0,
            "parent_drawer_id": "drawer_A",
        }
        out = _expand_with_neighbors(col, matched_doc, matched_meta, radius=1)
        text = out["text"]
        # Group A's chunks are returned in chunk_index order.
        assert "alpha-A-chunk-0" in text
        assert "alpha-A-chunk-1" in text
        # No leakage of group B's chunks through the shared source_file key.
        assert "bravo-B-chunk-0" not in text
        assert "bravo-B-chunk-1" not in text
        # total_drawers is scoped to the parent group so the caller sees a
        # count consistent with the text returned (2 chunks in group A),
        # not 4 (every row sharing the source_file key).
        assert out["total_drawers"] == 2
        assert out["drawer_index"] == 0

    def test_expand_backwards_compat_no_parent_drawer_id_returns_all_source_neighbors(
        self, palace_path
    ):
        """Drawers without a ``parent_drawer_id`` (single-chunk writes,
        legacy palaces, ``diary_ingest`` chunks grouped by real file path)
        must take the 2-clause fallback (``source_file + chunk_index``)
        unchanged, so neighbor expansion still works file-globally for
        those callers.
        """
        col, _ = self._seed_source_file(palace_path, "/proj/legacy.md", n_chunks=5)
        matched_meta = {"source_file": "/proj/legacy.md", "chunk_index": 2}
        out = _expand_with_neighbors(
            col, "chunk_2 content about topic alpha", matched_meta, radius=1
        )
        # Same expectations as test_expand_returns_matched_plus_neighbors:
        # no parent_drawer_id anywhere, so behavior is unchanged.
        assert out["total_drawers"] == 5
        assert out["drawer_index"] == 2
        text = out["text"]
        assert "chunk_1" in text
        assert "chunk_2" in text
        assert "chunk_3" in text

    def test_hybrid_search_enrichment_isolates_chunks_across_drawers_sharing_source_file(
        self, palace_path
    ):
        """End-to-end for #1580. Two oversized add_drawer-shape groups
        share a ``source_file``, a closet boosts that source, and the
        ranked hit lands on group A. The enrichment step in
        ``search_memories`` must return only group A's text, not a mix
        of A and B chunks stitched as if they were sequential context.
        """
        col = get_collection(palace_path)
        source = "/proj/shared_log.md"
        # Group A: 2 chunks under parent_drawer_id "drawer_proj_log_aaa".
        col.upsert(
            ids=[
                "drawer_proj_log_aaa_chunk_000000",
                "drawer_proj_log_aaa_chunk_000001",
            ],
            documents=[
                "alpha JWT authentication flow",
                "alpha continues the auth narrative",
            ],
            metadatas=[
                {
                    "wing": "proj",
                    "room": "log",
                    "source_file": source,
                    "chunk_index": 0,
                    "parent_drawer_id": "drawer_proj_log_aaa",
                    "filed_at": "2026-04-13T00:00:00",
                },
                {
                    "wing": "proj",
                    "room": "log",
                    "source_file": source,
                    "chunk_index": 1,
                    "parent_drawer_id": "drawer_proj_log_aaa",
                    "filed_at": "2026-04-13T00:00:00",
                },
            ],
        )
        # Group B: 2 chunks under the SAME source_file but a different
        # parent_drawer_id, with content unrelated to the JWT query.
        col.upsert(
            ids=[
                "drawer_proj_log_bbb_chunk_000000",
                "drawer_proj_log_bbb_chunk_000001",
            ],
            documents=[
                "bravo unrelated topic about database migrations",
                "bravo continues with PostgreSQL specifics",
            ],
            metadatas=[
                {
                    "wing": "proj",
                    "room": "log",
                    "source_file": source,
                    "chunk_index": 0,
                    "parent_drawer_id": "drawer_proj_log_bbb",
                    "filed_at": "2026-04-13T00:00:00",
                },
                {
                    "wing": "proj",
                    "room": "log",
                    "source_file": source,
                    "chunk_index": 1,
                    "parent_drawer_id": "drawer_proj_log_bbb",
                    "filed_at": "2026-04-13T00:00:00",
                },
            ],
        )
        # Closet pointing at group A's first chunk for this source.
        closets = get_closets_collection(palace_path)
        closets.upsert(
            ids=["closet_proj_log_aaa_01"],
            documents=["JWT auth|;|→drawer_proj_log_aaa_chunk_000000"],
            metadatas=[{"wing": "proj", "room": "log", "source_file": source}],
        )

        result = search_memories("JWT authentication", palace_path)
        assert result["results"]
        boosted = [h for h in result["results"] if h["matched_via"] == "drawer+closet"]
        assert boosted, "hybrid search should mark the closet-agreeing source"
        top = boosted[0]
        text = top["text"]
        # Group A's content is present.
        assert "alpha" in text
        # Group B's content must not leak in through the shared source_file
        # key. The enrichment loop fetches sibling chunks for the matched
        # source, and prior to #1580 that fetch ignored parent_drawer_id.
        assert "bravo" not in text, (
            "neighbor enrichment leaked group B's chunks through the shared "
            "source_file key (see #1580)"
        )
        # total_drawers on the enriched hit is scoped to the matched
        # parent group (2 chunks in group A), not the full source_file
        # row count (4 across both groups). Pins the scoping contract on
        # the live enrichment path, not just the helper.
        assert top["total_drawers"] == 2
        # Internal scoring-loop keys must be scrubbed before results are
        # returned to MCP callers. ``_parent_drawer_id`` is added during
        # the #1580 fix and popped in the final cleanup loop alongside
        # the existing internal keys.
        for h in result["results"]:
            assert "_parent_drawer_id" not in h
            assert "_source_file_full" not in h
            assert "_chunk_index" not in h
            assert "_sort_key" not in h

    def test_expand_isolates_asymmetric_groups_under_shared_source_file(self, palace_path):
        """Asymmetric coverage: group A has 1 chunk, group B has 3 chunks
        under the shared ``source_file``. Catches a regression where
        ``total_drawers`` accidentally drifts back to the unscoped
        file-global count (4) when one group dominates the row mix.
        """
        col = get_collection(palace_path)
        source = "asym.log"
        col.upsert(
            ids=["drawer_solo_chunk_000000"],
            documents=["solo-A-chunk-0 content"],
            metadatas=[
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": 0,
                    "parent_drawer_id": "drawer_solo",
                    "filed_at": "2026-04-13T00:00:00",
                }
            ],
        )
        col.upsert(
            ids=[
                "drawer_trio_chunk_000000",
                "drawer_trio_chunk_000001",
                "drawer_trio_chunk_000002",
            ],
            documents=[
                "trio-B-chunk-0 content",
                "trio-B-chunk-1 content",
                "trio-B-chunk-2 content",
            ],
            metadatas=[
                {
                    "wing": "w",
                    "room": "r",
                    "source_file": source,
                    "chunk_index": i,
                    "parent_drawer_id": "drawer_trio",
                    "filed_at": "2026-04-13T00:00:00",
                }
                for i in range(3)
            ],
        )

        out = _expand_with_neighbors(
            col,
            "solo-A-chunk-0 content",
            {
                "source_file": source,
                "chunk_index": 0,
                "parent_drawer_id": "drawer_solo",
            },
            radius=1,
        )
        # Singleton group A: text is the matched chunk, total_drawers == 1.
        assert "solo-A-chunk-0" in out["text"]
        assert "trio-B-chunk" not in out["text"]
        assert out["total_drawers"] == 1
        assert out["drawer_index"] == 0

    def test_expand_empty_string_parent_drawer_id_treated_as_absent(self, palace_path):
        """Contract pin: an empty-string ``parent_drawer_id`` value
        degrades to the 2-clause file-global filter (matches the
        ``if not src`` empty-string handling for ``source_file`` at
        ``searcher.py:239``). Writers in the codebase never emit an
        empty parent id, but pinning the contract guards against a
        future migration that does and avoids a silent narrow-then-
        miss surprise.
        """
        col, _ = self._seed_source_file(palace_path, "/proj/empty_parent.md", n_chunks=3)
        matched_meta = {
            "source_file": "/proj/empty_parent.md",
            "chunk_index": 1,
            "parent_drawer_id": "",
        }
        out = _expand_with_neighbors(
            col, "chunk_1 content about topic alpha", matched_meta, radius=1
        )
        # Empty parent_drawer_id is treated as absent; full file-global
        # neighborhood is returned. Mirrors backwards-compat behavior.
        assert out["total_drawers"] == 3
        assert "chunk_0" in out["text"]
        assert "chunk_1" in out["text"]
        assert "chunk_2" in out["text"]
