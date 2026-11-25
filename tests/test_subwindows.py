"""Sub-window vectors for drawers longer than the embedder's window (#390)."""

import hashlib
import math
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


def test_merge_keeps_the_pool_size():
    drawers = _results([("a", "A", {}, 0.5), ("b", "B", {}, 0.6)])
    best = {"c": (0.1, parent_sha("C"))}
    merged, _ = merge_window_hits(
        drawers, best, {"c": ("C", {})}, to_drawer_scale=lambda d: d, limit=2
    )
    assert merged["ids"] == [["c", "a"]]


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
