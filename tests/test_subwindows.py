"""Sub-window vectors for drawers longer than the embedder's window (#390)."""

import hashlib
import math
import random
import re

import pytest

from mempalace import subwindows
from mempalace.subwindows import (
    WindowProfile,
    WindowTokenizer,
    merge_window_hits,
    parent_sha,
    plan_windows,
)

_WORD = re.compile(r"\S+")


def _word_tokenizer(budget: int) -> WindowTokenizer:
    """One token per whitespace-separated word, ``budget`` tokens per window."""

    def offsets(text):
        return [(m.start(), m.end()) for m in _WORD.finditer(text)]

    return WindowTokenizer(offsets, budget)


# ── window planning ──────────────────────────────────────────────────────


def test_text_within_the_window_gets_no_windows():
    assert plan_windows("one two three four", _word_tokenizer(4), overlap=1) == []


def test_windows_cover_the_tail_verbatim_and_skip_the_head():
    words = [f"w{i}" for i in range(20)]
    text = " ".join(words)

    spans = plan_windows(text, _word_tokenizer(8), overlap=2)

    # Stride 6: the head window [0, 8) is the drawer's own vector, so the
    # stored windows start at tokens 6 and 12; the second one reaches the end.
    assert [text[a:b] for a, b in spans] == [
        " ".join(words[6:14]),
        " ".join(words[12:20]),
    ]


def test_window_spans_keep_the_original_characters():
    text = "Ünïcode  spacing\tand\npunctuation, " * 6
    spans = plan_windows(text, _word_tokenizer(5), overlap=1)
    assert spans
    for start, end in spans:
        assert text[start:end] == text[start:end].strip()
        assert text[start:end] in text


# ── folding windows into parents ─────────────────────────────────────────


def _results(rows):
    return {
        "ids": [[r[0] for r in rows]],
        "documents": [[r[1] for r in rows]],
        "metadatas": [[r[2] for r in rows]],
        "distances": [[r[3] for r in rows]],
    }


def test_a_drawer_scores_by_its_best_window_and_new_parents_join():
    drawers = _results([("a", "A", {"wing": "w"}, 0.5), ("b", "B", {"wing": "w"}, 0.6)])
    best = {"b": (0.1, parent_sha("B")), "c": (0.2, parent_sha("C"))}
    parents = {"b": ("B", {"wing": "w"}), "c": ("C", {"wing": "w"})}

    merged, promoted = merge_window_hits(
        drawers, best, parents, to_drawer_scale=lambda d: d, limit=10
    )

    assert merged["ids"] == [["b", "c", "a"]]
    assert merged["documents"] == [["B", "C", "A"]]
    assert merged["distances"] == [[0.1, 0.2, 0.5]]
    assert promoted == 2


def test_a_window_never_makes_its_drawer_worse():
    drawers = _results([("a", "A", {}, 0.1)])
    merged, promoted = merge_window_hits(
        drawers,
        {"a": (0.4, parent_sha("A"))},
        {"a": ("A", {})},
        to_drawer_scale=lambda d: d,
        limit=5,
    )
    assert merged["distances"] == [[0.1]]
    assert promoted == 0


def test_stale_or_orphaned_windows_are_ignored():
    drawers = _results([("a", "A", {}, 0.5)])
    best = {
        "a": (0.05, parent_sha("old text of A")),  # parent rewritten since
        "gone": (0.01, parent_sha("G")),  # parent deleted / filtered out
    }

    merged, promoted = merge_window_hits(
        drawers, best, {"a": ("A", {})}, to_drawer_scale=lambda d: d, limit=5
    )

    assert merged is drawers
    assert promoted == 0


def test_merge_keeps_displaced_drawers_for_the_hybrid_reranker():
    drawers = _results([("a", "A", {}, 0.5), ("b", "B", {}, 0.6)])
    best = {"c": (0.1, parent_sha("C"))}
    merged, _ = merge_window_hits(
        drawers, best, {"c": ("C", {})}, to_drawer_scale=lambda d: d, limit=2
    )
    assert merged["ids"] == [["c", "a", "b"]]


def test_window_distances_are_converted_to_the_drawer_scale():
    drawers = _results([("a", "A", {}, 0.9)])
    merged, _ = merge_window_hits(
        drawers,
        {"a": (0.2, parent_sha("A"))},
        {"a": ("A", {})},
        to_drawer_scale=lambda d: d * 2,  # e.g. cosine windows, l2 drawers
        limit=5,
    )
    assert merged["distances"] == [[0.4]]


# ── a real palace ────────────────────────────────────────────────────────

FILLER = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu".split()


def _long_text(fact: str, filler_words: int = 40) -> str:
    return " ".join(FILLER[i % len(FILLER)] + str(i) for i in range(filler_words)) + " " + fact


WINDOW = 16
DIM = 1024


def _truncating_embedding(text: str) -> list:
    """Bag-of-words vector of the first ``WINDOW`` words, like a 256-token model."""
    vec = [0.0] * DIM
    for word in _WORD.findall((text or "").lower())[:WINDOW] or [""]:
        vec[int(hashlib.blake2b(word.encode(), digest_size=4).hexdigest(), 16) % DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _TruncatingEF:
    @staticmethod
    def name():
        return "default"

    def __call__(self, input):
        return [_truncating_embedding(str(t)) for t in list(input or [])]

    def embed_query(self, input):
        return self(input=input)


@pytest.fixture
def window_palace(palace_path, monkeypatch):
    """A palace with one long drawer (fact in its tail) among short distractors.

    The embedder reads only the first ``WINDOW`` words of a text, and the
    window seam gets the matching word tokenizer.
    """
    import mempalace.backends.chroma as chroma_mod
    import mempalace.backends.embedding_wrapper as embedding_wrapper
    import mempalace.embedding as embedding_mod
    from mempalace.palace import get_collection

    ef = _TruncatingEF()
    monkeypatch.setattr(embedding_mod, "get_embedding_function", lambda *_, **__: ef)
    monkeypatch.setattr(
        chroma_mod.ChromaBackend, "_resolve_embedding_function", staticmethod(lambda **_: ef)
    )
    monkeypatch.setattr(embedding_wrapper, "_embed_texts", lambda texts, **_: ef(input=list(texts)))
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "1")
    tokenizer = _word_tokenizer(WINDOW)
    profile = WindowProfile("test-words", DIM, tokenizer.budget, 4)
    monkeypatch.setattr(
        subwindows, "active_profile", lambda embedder=None: (ef, tokenizer, profile)
    )
    col = get_collection(palace_path, create=True)
    ids = ["long_1", "long_2"] + [f"short_{i}" for i in range(30)]
    docs = [
        _long_text("the zebra quokka migration happened"),
        _long_text("nothing special here", filler_words=30),
    ] + [f"note {i} about apples pears migration {i}" for i in range(30)]
    metas = [{"wing": "w", "room": "r", "source_file": f"/src/{i}.md"} for i in ids]
    col.upsert(ids=ids, documents=docs, metadatas=metas)
    return palace_path, col


def _search(palace_path, query="zebra quokka migration", n_results=1):
    from mempalace.searcher import search_memories

    return search_memories(query, palace_path, n_results=n_results)


def _windows_change_nothing(palace_path, monkeypatch, query="zebra quokka migration"):
    """Search with windows on returns exactly what it returns with them off."""
    on = _search(palace_path, query, n_results=3)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    off = _search(palace_path, query, n_results=3)
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "1")
    return on == off


def _window_rows(palace_path):
    from mempalace.palace import open_windows_collection

    col = open_windows_collection(palace_path)
    if col is None:
        return {}
    got = col.get(include=["documents", "metadatas"])
    return {i: (d, m) for i, d, m in zip(got["ids"], got["documents"], got["metadatas"])}


def test_build_embeds_only_long_drawers_and_stores_no_text(window_palace):
    palace_path, col = window_palace

    stats = subwindows.build_windows(palace_path)

    rows = _window_rows(palace_path)
    assert stats["long_drawers"] == 2
    assert {m["window_of"] for _, m in rows.values()} == {"long_1", "long_2"}
    assert all(doc in ("", None) for doc, _ in rows.values())
    long_1 = col.get(ids=["long_1"])["documents"][0]
    for _, meta in rows.values():
        if meta["window_of"] == "long_1":
            assert long_1[meta["char_start"] : meta["char_end"]] in long_1


def test_window_rows_never_show_up_as_drawers(window_palace):
    palace_path, col = window_palace
    before = sorted(col.get(include=[])["ids"])

    subwindows.build_windows(palace_path)

    assert col.count() == 32
    assert sorted(col.get(include=[])["ids"]) == before
    from mempalace.searcher import search_memories

    hits = search_memories("alpha0 beta1", palace_path, n_results=50)["results"]
    assert all("::w" not in hit["drawer_id"] for hit in hits)


def test_tail_fact_is_found_through_its_window(window_palace):
    palace_path, col = window_palace
    without = _search(palace_path)
    subwindows.build_windows(palace_path)

    with_windows = _search(palace_path)

    assert [h["drawer_id"] for h in without["results"]] != ["long_1"]
    assert [h["drawer_id"] for h in with_windows["results"]] == ["long_1"]
    # The hit is the verbatim drawer, not the window.
    assert with_windows["results"][0]["text"] == col.get(ids=["long_1"])["documents"][0]


def test_flag_off_search_is_unchanged_and_never_opens_windows(window_palace, monkeypatch):
    palace_path, _ = window_palace
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    before = _search(palace_path, n_results=5)
    subwindows.build_windows(palace_path)

    import mempalace.palace as palace_mod

    opened = []
    real = palace_mod.get_collection

    def spy(*args, **kwargs):
        opened.append(kwargs.get("collection_name"))
        return real(*args, **kwargs)

    monkeypatch.setattr(palace_mod, "get_collection", spy)
    after = _search(palace_path, n_results=5)

    assert after == before
    assert palace_mod.WINDOWS_COLLECTION_NAME not in opened


def test_deleting_a_drawer_deletes_its_windows(window_palace):
    from mempalace.palace import drawers_removed

    palace_path, col = window_palace
    subwindows.build_windows(palace_path)

    col.delete(ids=["long_1"])
    drawers_removed(palace_path, aaak_ids=["long_1"])

    assert {m["window_of"] for _, m in _window_rows(palace_path).values()} == {"long_2"}


def test_deleting_a_source_deletes_its_windows(window_palace):
    from mempalace.palace import drawers_removed

    palace_path, col = window_palace
    subwindows.build_windows(palace_path)

    col.delete(where={"source_file": "/src/long_2.md"})
    drawers_removed(palace_path, aaak_source="/src/long_2.md")

    assert {m["window_of"] for _, m in _window_rows(palace_path).values()} == {"long_1"}


def test_refiling_a_file_replaces_the_windows_of_changed_drawers(window_palace, monkeypatch):
    from mempalace.palace import write_file_drawers

    palace_path, col = window_palace
    subwindows.build_windows(palace_path)
    index = subwindows.open_window_index(palace_path)
    meta = {"wing": "w", "room": "r", "source_file": "/src/long_1.md"}

    write_file_drawers(
        col,
        "/src/long_1.md",
        [("long_1", _long_text("a walrus learned the harp"), meta)],
        windows=index,
    )

    assert _search(palace_path, "walrus harp")["results"][0]["drawer_id"] == "long_1"
    assert _windows_change_nothing(palace_path, monkeypatch)
    rows = [m for _, m in _window_rows(palace_path).values() if m["window_of"] == "long_1"]
    assert rows and {m["parent_sha"] for m in rows} == {
        parent_sha(col.get(ids=["long_1"])["documents"][0])
    }


def test_a_window_whose_parent_changed_without_purge_is_ignored(window_palace, monkeypatch):
    palace_path, col = window_palace
    subwindows.build_windows(palace_path)

    # A path that rewrites text without the cascade: the old windows linger.
    col.update(ids=["long_1"], documents=["completely different short text"])

    assert _windows_change_nothing(palace_path, monkeypatch)


def test_build_is_incremental_and_prunes_orphans(window_palace):
    palace_path, col = window_palace
    first = subwindows.build_windows(palace_path)

    again = subwindows.build_windows(palace_path)
    assert again["embedded"] == 0
    assert again["skipped"] == first["long_drawers"]

    col.update(ids=["long_2"], documents=[_long_text("a fresh tail", filler_words=31)])
    col.delete(ids=["long_1"])  # no cascade: leaves orphans behind
    third = subwindows.build_windows(palace_path)

    assert third["embedded"] > 0
    assert third["skipped"] == 0
    assert third["orphans_pruned"] > 0
    assert {m["window_of"] for _, m in _window_rows(palace_path).values()} == {"long_2"}


def test_a_drawer_that_shrank_loses_its_windows(window_palace):
    palace_path, col = window_palace
    subwindows.build_windows(palace_path)

    col.update(ids=["long_1"], documents=["now short"])
    subwindows.build_windows(palace_path)

    assert "long_1" not in {m["window_of"] for _, m in _window_rows(palace_path).values()}


def test_a_new_embedder_profile_invalidates_windows(window_palace, monkeypatch):
    palace_path, _ = window_palace
    subwindows.build_windows(palace_path)
    ef, tokenizer, _ = subwindows.active_profile()
    other = WindowProfile("another-model", DIM, tokenizer.budget, 4)
    monkeypatch.setattr(subwindows, "active_profile", lambda embedder=None: (ef, tokenizer, other))

    # Search ignores windows of another profile...
    assert _windows_change_nothing(palace_path, monkeypatch)
    # ...and a build rewrites them all for the new one.
    stats = subwindows.build_windows(palace_path)
    assert stats["skipped"] == 0 and stats["embedded"] > 0
    assert {m["window_profile"] for _, m in _window_rows(palace_path).values()} == {other.key}
    assert _search(palace_path)["results"][0]["drawer_id"] == "long_1"


def test_mining_with_the_flag_off_creates_no_windows(palace_path, monkeypatch):
    monkeypatch.delenv("MEMPALACE_EMBEDDING_SUBWINDOWS", raising=False)
    assert subwindows.open_window_index(palace_path) is None


def test_real_minilm_tokenizer_windows_past_256_tokens():
    """The window plan uses MiniLM's own tokenizer and 256-token limit."""
    from mempalace.embedding import _build_ef_class

    ef = _build_ef_class()()
    try:
        ef.tokenizer
    except Exception as exc:  # pragma: no cover - model not downloaded
        pytest.skip(f"MiniLM tokenizer unavailable: {exc}")
    tokenizer = subwindows.window_tokenizer(ef, "minilm")

    assert tokenizer.budget == 254  # 256 minus [CLS] and [SEP]
    assert plan_windows("short text", tokenizer, 64) == []
    text = " ".join(f"word{i}" for i in range(400))
    spans = plan_windows(text, tokenizer, 64)
    assert spans and spans[-1][1] == len(text)
    assert subwindows.window_tokenizer(object(), "openai-compat") is None


# ── MCP writes ───────────────────────────────────────────────────────────


@pytest.fixture
def mcp_windows(window_palace, config, monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_config", config)
    return window_palace[0], mcp_server


def _windows_of(palace_path, parent):
    return {i for i, (_, m) in _window_rows(palace_path).items() if m["window_of"] == parent}


def test_an_mcp_added_drawer_gets_windows(mcp_windows):
    palace_path, server = mcp_windows

    added = server.tool_add_drawer("w", "r", _long_text("an okapi tapir sighting"))

    assert added["success"], added
    assert _windows_of(palace_path, added["drawer_id"])
    hits = _search(palace_path, "okapi tapir sighting")["results"]
    assert [h["drawer_id"] for h in hits] == [added["drawer_id"]]


def test_an_mcp_update_replaces_the_windows_of_the_old_text(mcp_windows):
    palace_path, server = mcp_windows
    subwindows.build_windows(palace_path)
    old = _window_rows(palace_path)

    updated = server.tool_update_drawer("long_1", content=_long_text("a narwhal walrus encounter"))

    assert updated["success"], updated
    new = {i: m for i, (_, m) in _window_rows(palace_path).items() if m["window_of"] == "long_1"}
    assert new
    old_shas = {m["parent_sha"] for _, m in old.values() if m["window_of"] == "long_1"}
    assert not old_shas & {m["parent_sha"] for m in new.values()}
    hits = _search(palace_path, "narwhal walrus encounter")["results"]
    assert [h["drawer_id"] for h in hits] == ["long_1"]


def test_an_mcp_diary_entry_gets_windows(mcp_windows):
    palace_path, server = mcp_windows

    wrote = server.tool_diary_write("agent", _long_text("the ibex chamois climb"))

    assert wrote["success"], wrote
    assert _windows_of(palace_path, wrote["entry_id"])


# ── CLI ──────────────────────────────────────────────────────────────────


def test_windows_status_on_a_missing_palace_exits_with_a_hint(tmp_path, capsys):
    import argparse

    from mempalace.cli import cmd_windows

    missing = tmp_path / "no-palace"
    args = argparse.Namespace(palace=str(missing), windows_action="status", wing=None)
    with pytest.raises(SystemExit) as exited:
        cmd_windows(args)

    assert exited.value.code == 1
    assert "No palace with drawers" in capsys.readouterr().out
    assert not missing.exists()


def test_windows_build_reports_what_it_embedded(window_palace, capsys):
    import argparse

    from mempalace.cli import cmd_windows

    palace_path, _ = window_palace
    args = argparse.Namespace(palace=palace_path, windows_action="build", wing=None, dry_run=False)
    cmd_windows(args)

    out = capsys.readouterr().out
    assert "Over the window:  2" in out
    assert _windows_of(palace_path, "long_1")


def test_hybrid_retains_head_candidate_for_bm25(window_palace, monkeypatch):
    import mempalace.searcher as searcher

    palace, col = window_palace
    head = "zebra quokka migration"
    col.upsert(ids=["head"], documents=[head], metadatas=[{"room": "r"}])
    # The four baseline candidates include an exact lexical match. Four closer
    # tail windows can outrank them by vector distance before the lexical pass.
    tail_ids = [f"tail{i}" for i in range(4)]
    tails = [_long_text(f"zebra herd {i}") for i in range(4)]
    col.upsert(ids=tail_ids, documents=tails, metadatas=[{"room": "r"}] * 4)
    subwindows.build_windows(palace)
    monkeypatch.setattr(
        searcher,
        "_query_drawers_with_filter_fallback",
        lambda *a: _results(
            [(f"other{i}", f"haystack {i}", {}, 0.6) for i in range(3)] + [("head", head, {}, 0.61)]
        ),
    )
    monkeypatch.setattr(
        subwindows,
        "window_candidates",
        lambda *_: (
            {i: (0.5, parent_sha(doc)) for i, doc in zip(tail_ids, tails)},
            {i: (doc, {}) for i, doc in zip(tail_ids, tails)},
        ),
    )
    assert _search(palace, head)["results"][0]["drawer_id"] == "head"


def test_sweep_indexes_the_message_it_wrote(window_palace, tmp_path):
    import json

    from mempalace.sweeper import sweep

    palace, _ = window_palace
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:00Z",
                "sessionId": "s",
                "uuid": "message",
                "message": {"role": "user", "content": _long_text("a tapir on the riverbank")},
            }
        )
    )
    assert sweep(str(transcript), palace)["drawers_added"] == 1
    assert _window_rows(palace)


def test_real_tokenizer_accepts_losslessly_decoded_source_bytes():
    from tokenizers import Tokenizer, models

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    offsets = subwindows._offsets_fn(tokenizer)
    assert offsets("a\udcffb") == [(0, 3)]


def test_default_off_write_never_opens_window_storage(window_palace, monkeypatch):
    import mempalace.palace as palace_mod

    palace, _ = window_palace
    monkeypatch.setenv("MEMPALACE_EMBEDDING_SUBWINDOWS", "0")
    opened = []
    monkeypatch.setattr(palace_mod, "get_windows_collection", lambda *a, **k: opened.append(1))
    subwindows.index_after_write(palace, ["id"], ["text"], [{}])
    palace_mod.drawers_removed(palace, aaak_ids=["id"])
    assert not opened


def test_backfill_refuses_window_name_already_used_by_drawers(window_palace, monkeypatch):
    import mempalace.config as config
    from mempalace.palace import WINDOWS_COLLECTION_NAME, get_collection

    palace, _ = window_palace
    monkeypatch.setattr(config, "get_configured_collection_name", lambda: WINDOWS_COLLECTION_NAME)
    col = get_collection(palace)
    col.upsert(ids=["real"], documents=[_long_text("tail")], metadatas=[{"room": "r"}])
    with pytest.raises(subwindows.SubwindowsUnsupported):
        subwindows.build_windows(palace)
    assert col.get(include=[])["ids"] == ["real"]


def test_control_keeps_mixed_head_queries_and_unaffected_drawers():
    from tests.benchmarks import subwindow_data as data

    mixed = data.build_corpus(n_tail=4, n_head=4, n_short=4)
    control = data.head_control(mixed)
    assert control.queries == [q for q in mixed.queries if q.position == "head"]
    assert len(control.drawers) == len(mixed.drawers)
    for a, b in zip(mixed.drawers, control.drawers):
        if a.position == "tail":
            assert a.fact not in b.text and a.drawer_id == b.drawer_id
        else:
            assert a == b


def test_rebuild_accepts_old_identity_on_derived_collection_only(window_palace):
    import mempalace.palace as palace_mod
    from mempalace.backends.base import EmbedderIdentity

    palace, _ = window_palace
    subwindows.build_windows(palace)
    windows = palace_mod.get_windows_collection(palace)
    windows.set_embedder_identity(EmbedderIdentity("embeddinggemma", DIM))
    palace_mod._VALIDATED_IDENTITY.clear()
    assert subwindows.build_windows(palace)["long_drawers"] == 2


def test_window_collection_cannot_be_searched_as_drawers(window_palace):
    from mempalace.palace import WINDOWS_COLLECTION_NAME
    from mempalace.searcher import search_memories

    palace, _ = window_palace
    subwindows.build_windows(palace)
    assert (
        search_memories("migration", palace, collection_name=WINDOWS_COLLECTION_NAME)["results"]
        == []
    )


def test_server_embedder_search_keeps_using_text(window_palace, monkeypatch):
    from mempalace import searcher

    palace, col = window_palace
    monkeypatch.setattr(col._inner._backend, "capabilities", frozenset({"server_embedder"}))
    monkeypatch.setattr(searcher, "_open_search_collection", lambda *a, **k: (col, None))
    observed = []
    real = col.query

    def query(**kwargs):
        observed.append("query_texts" in kwargs)
        return real(**kwargs)

    monkeypatch.setattr(col, "query", query)
    assert _search(palace)["results"] and observed == [True]


def test_stress_corpus_has_all_requested_distinct_lexical_targets():
    from tests.benchmarks.subwindow_data import _fact_slots

    slots = _fact_slots(random.Random(390), 2000)
    assert len({(s, a, o) for s, a, o, _ in slots}) == 2000


def test_filtered_window_search_reaches_past_global_window_limit(window_palace, monkeypatch):
    from mempalace.searcher import search_memories

    palace, col = window_palace
    col.update(ids=["long_1"], metadatas=[{"room": "target"}])
    distractors = [f"d{i}" for i in range(5)]
    col.upsert(
        ids=distractors,
        documents=[f"migration apples pears {i}" for i in range(5)],
        metadatas=[{"room": "target"}] * 5,
    )
    col.upsert(
        ids=["other-room"],
        documents=[_long_text("zebra quokka migration " * 12, filler_words=16)],
        metadatas=[{"room": "elsewhere"}],
    )
    subwindows.build_windows(palace)
    monkeypatch.setattr(subwindows, "WINDOW_MAX_FETCH", 1)
    assert (
        search_memories("zebra quokka migration", palace, room="target", n_results=1)["results"][0][
            "drawer_id"
        ]
        == "long_1"
    )


def test_window_cannot_promote_an_older_snapshot_of_the_parent():
    old = _results([("p", "previous version", {}, 0.9)])
    best = {"p": (0.1, parent_sha("new version"))}
    merged, _ = merge_window_hits(
        old, best, {"p": ("new version", {})}, to_drawer_scale=float, limit=1
    )
    assert merged["documents"] == [["new version"]]


def test_source_delete_retry_purges_windows_after_drawer_delete(mcp_windows):
    from mempalace.palace import get_collection

    palace, server = mcp_windows
    subwindows.build_windows(palace)
    get_collection(palace).delete(ids=["long_1"])
    assert server.tool_delete_by_source("/src/long_1.md", dry_run=False)["success"]
    assert not _windows_of(palace, "long_1")


def test_partial_refresh_keeps_best_current_window(window_palace):
    from mempalace.palace import get_windows_collection

    palace, col = window_palace
    subwindows.build_windows(palace)
    windows = get_windows_collection(palace)
    # One stale vector is an exact query match, while other rows for the
    # same parent already carry the current fingerprint.
    windows.upsert(
        ids=["obsolete"],
        documents=[""],
        embeddings=[_truncating_embedding("zebra")],
        metadatas=[
            {
                "window_of": "long_1",
                "parent_sha": "stale",
                "window_profile": subwindows.active_profile()[2].key,
            }
        ],
    )
    best, _ = subwindows.window_candidates(
        col, windows, [_truncating_embedding("zebra")], subwindows.active_profile()[2].key, {}, 10
    )
    assert best["long_1"][1] == parent_sha(col.get(ids=["long_1"])["documents"][0])


def test_backfill_drops_source_pointer_removed_from_parent(window_palace):
    palace, col = window_palace
    subwindows.build_windows(palace)
    col.update(ids=["long_1"], metadatas=[{"source_file": None}])
    assert "source_file" not in col.get(ids=["long_1"])["metadatas"][0]
    subwindows.build_windows(palace)
    assert all(
        "source_file" not in meta
        for _, meta in _window_rows(palace).values()
        if meta["window_of"] == "long_1"
    )


def test_scoped_window_pass_reads_ids_only_and_is_bounded(window_palace, monkeypatch):
    """The filtered fallback pages parent ids, not text, and stops at its cap."""
    from mempalace.searcher import search_memories

    palace, col = window_palace
    col.update(ids=["long_1"], metadatas=[{"room": "target"}])
    col.upsert(
        ids=[f"t{i}" for i in range(12)],
        documents=[f"migration apples pears {i}" for i in range(12)],
        metadatas=[{"room": "target"}] * 12,
    )
    col.upsert(
        ids=["other-room"],
        documents=[_long_text("zebra quokka migration " * 12, filler_words=16)],
        metadatas=[{"room": "elsewhere"}],
    )
    subwindows.build_windows(palace)
    monkeypatch.setattr(subwindows, "WINDOW_MAX_FETCH", 1)
    monkeypatch.setattr(subwindows, "WINDOW_SCOPED_PAGE", 5)
    reads = []
    real_get = col.get

    def spy(**kwargs):
        reads.append(kwargs)
        return real_get(**kwargs)

    monkeypatch.setattr(col, "get", spy)
    monkeypatch.setattr("mempalace.searcher._open_search_collection", lambda *a, **k: (col, None))

    hits = search_memories("zebra quokka migration", palace, room="target", n_results=1)
    assert hits["results"][0]["drawer_id"] == "long_1"
    paged = [r for r in reads if "offset" in r]
    assert paged and all(r["include"] == [] for r in paged)

    # With the cap below the in-scope parent count, the pass stops early.
    reads.clear()
    monkeypatch.setattr(subwindows, "WINDOW_SCOPED_MAX_PARENTS", 5)
    search_memories("zebra quokka migration", palace, room="target", n_results=1)
    assert len([r for r in reads if "offset" in r]) == 1


def test_scoped_window_pass_skips_a_stale_nearest_window(window_palace, monkeypatch):
    from mempalace.palace import get_windows_collection

    palace, col = window_palace
    col.update(ids=["long_1"], metadatas=[{"room": "target"}])
    col.upsert(
        ids=["other-room"],
        documents=[_long_text("zebra quokka migration " * 12, filler_words=16)],
        metadatas=[{"room": "elsewhere"}],
    )
    subwindows.build_windows(palace)
    key = subwindows.active_profile()[2].key
    get_windows_collection(palace).upsert(
        ids=["stale"],
        documents=[""],
        embeddings=[_truncating_embedding("zebra quokka migration")],
        metadatas=[{"window_of": "long_1", "parent_sha": "stale", "window_profile": key}],
    )
    monkeypatch.setattr(subwindows, "WINDOW_MAX_FETCH", 1)
    best, parents = subwindows.window_candidates(
        col,
        get_windows_collection(palace),
        [_truncating_embedding("zebra quokka migration")],
        key,
        {"room": "target"},
        5,
    )
    assert set(best) == {"long_1"} and set(parents) == {"long_1"}
    assert best["long_1"][1] == parent_sha(col.get(ids=["long_1"])["documents"][0])
