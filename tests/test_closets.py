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
        assert (
            enter_a < exit_a <= enter_b < exit_b
        ), f"critical sections overlapped — lock failed to serialize: {intervals}"


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
            assert (
                f"topic {i}\n" not in second_docs
            ), f"stale 'Topic {i}' from first mine survived the rebuild"
        # Numbered closets that existed only in the larger first run must be gone.
        leftover = first_ids - set(second_pass["ids"])
        for stale_id in leftover:
            assert not col.get(ids=[stale_id])[
                "ids"
            ], f"orphan closet {stale_id} from larger first run survived purge"


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
            assert (
                "diary_ingest" not in entry.name
            ), f"state file leaked into user diary dir: {entry}"

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

        from mempalace.diary_ingest import _diary_drawer_id, ingest_diaries

        ingest_diaries(str(personal_dir), str(palace_dir), wing="personal", force=True)
        ingest_diaries(str(work_dir), str(palace_dir), wing="work", force=True)

        col = get_collection(str(palace_dir))
        personal_id = _diary_drawer_id("personal", "2026-04-13")
        work_id = _diary_drawer_id("work", "2026-04-13")
        assert personal_id != work_id

        personal = col.get(ids=[personal_id])
        work = col.get(ids=[work_id])
        assert personal["ids"] == [personal_id]
        assert work["ids"] == [work_id]
        assert "Personal-only marker." in personal["documents"][0]
        assert "Work-only marker." in work["documents"][0]


# ── cross-wing tunnels ───────────────────────────────────────────────


class TestTunnels:
    """Tunnels are explicit cross-wing connections stored in
    ``~/.mempalace/tunnels.json``. Each test points the module-level
    ``_TUNNEL_FILE`` at a fresh tmp file so tests don't cross-contaminate
    or touch the user's real tunnels."""

    def setup_method(self):
        import mempalace.palace_graph as pg

        self._orig = pg._TUNNEL_FILE
        self._tmpdir = tempfile.mkdtemp()
        pg._TUNNEL_FILE = os.path.join(self._tmpdir, "tunnels.json")

    def teardown_method(self):
        import mempalace.palace_graph as pg

        pg._TUNNEL_FILE = self._orig
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
        with open(pg._TUNNEL_FILE, "w") as f:
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
        assert os.path.exists(pg._TUNNEL_FILE)
        assert not os.path.exists(pg._TUNNEL_FILE + ".tmp")

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
            f"expected 5 concurrent tunnels, got {len(tunnels)} — " "write race dropped some"
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
