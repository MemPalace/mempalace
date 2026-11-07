"""Golden tokenizer and BM25 values for ``mempalace.lexical``.

The scores were produced by the five copies this module replaced (the
searcher and the chroma, sqlite_exact, pgvector and qdrant backends), which
all agreed bit for bit on this corpus. Any change here changes ranking on
every backend, so it must be deliberate.
"""

import pytest

from mempalace.lexical import bm25_scores, tokenize

CORPUS = [
    "Kubernetes rollout notes: we rolled back the deploy on Friday.",
    "The cat sat on the mat. The mat was red.",
    "Caf\xe9 r\xe9sum\xe9 na\xefve — d\xe9j\xe0 vu \xfcber stra\xdfe",
    "東京で会議がありました 会議 会議",
    "stray \udcff byte and caf\xe9 again",
    "",
    None,
    "deploy deploy deploy kubernetes",
    "a b c d",
    "Rust CLI: cargo clippy --all-targets warnings fixed",
]

TOKENS = [
    ["kubernetes", "rollout", "notes", "we", "rolled", "back", "the", "deploy", "on", "friday"],
    ["the", "cat", "sat", "on", "the", "mat", "the", "mat", "was", "red"],
    ["caf\xe9", "r\xe9sum\xe9", "na\xefve", "d\xe9j\xe0", "vu", "\xfcber", "stra\xdfe"],
    ["東京で会議がありました", "会議", "会議"],
    # A lone surrogate (a stray source byte, kept by the lossless decode) is
    # not a word character, so it never becomes a token.
    ["stray", "byte", "and", "caf\xe9", "again"],
    [],
    [],
    ["deploy", "deploy", "deploy", "kubernetes"],
    [],
    ["rust", "cli", "cargo", "clippy", "all", "targets", "warnings", "fixed"],
]

GOLDEN = {
    "kubernetes deploy": [1.965713858106934, 0, 0, 0, 0, 0, 0, 4.152877067572238, 0, 0],
    "the mat": [0.982856929053467, 4.015397748905191, 0, 0, 0, 0, 0, 0, 0, 0],
    "caf\xe9": [0, 0, 1.214218193957073, 0, 1.440236058395825, 0, 0, 0, 0, 0],
    "会議": [0, 0, 0, 3.220781349628192, 0, 0, 0, 0, 0, 0],
    "stra\xdfe \xfcber": [0, 0, 3.265709424252474, 0, 0, 0, 0, 0, 0, 0],
    "clippy warnings": [0, 0, 0, 0, 0, 0, 0, 0, 0, 3.028107283441865],
    "": [0] * 10,
    "a": [0] * 10,
    "nothing matches here": [0] * 10,
}


def test_tokens_match_the_golden_corpus():
    assert [tokenize(d) for d in CORPUS] == TOKENS


@pytest.mark.parametrize("query", list(GOLDEN))
def test_bm25_scores_match_the_golden_corpus(query):
    assert bm25_scores(query, CORPUS) == pytest.approx(GOLDEN[query], rel=1e-12, abs=0.0)


def test_ranking_order_matches_the_golden_corpus():
    scores = bm25_scores("kubernetes deploy", CORPUS)
    ranked = sorted(range(len(CORPUS)), key=lambda i: -scores[i])
    assert ranked[:2] == [7, 0]


def test_stop_words_drop_from_query_and_documents():
    stop = frozenset({"the", "on", "was"})
    assert tokenize(CORPUS[1], stop) == ["cat", "sat", "mat", "mat", "red"]
    # Only stop words in the query: no scores at all.
    assert bm25_scores("the on", CORPUS, stop_words=stop) == [0.0] * len(CORPUS)
