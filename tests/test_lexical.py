"""Golden tokenizer and BM25 values for ``mempalace.lexical``.

The scores were produced by the five copies this module replaced (the
searcher and the chroma, sqlite_exact, pgvector and qdrant backends), which
all agreed bit for bit on this corpus. Any change here changes ranking on
every backend, so it must be deliberate.

The CJK bigram tokenizer changed the Japanese row's tokens and, through
``avgdl``, every score in the table; the values below were regenerated from
that implementation.
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
    [
        "東京",
        "京で",
        "で会",
        "会議",
        "議が",
        "があ",
        "あり",
        "りま",
        "まし",
        "した",
        "東京で会議がありました",
        "会議",
        "会議",
    ],
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
    "kubernetes deploy": [2.2122189609084555, 0, 0, 0, 0, 0, 0, 4.37956829971116, 0, 0],
    "the mat": [1.1061094804542277, 4.3683692119674395, 0, 0, 0, 0, 0, 0, 0, 0],
    "caf\xe9": [0, 0, 1.3436986290004818, 0, 1.5682722160200613, 0, 0, 0, 0, 0],
    "会議": [0, 0, 0, 2.515360340804911, 0, 0, 0, 0, 0, 0],
    "stra\xdfe \xfcber": [0, 0, 3.61395447533307, 0, 0, 0, 0, 0, 0, 0],
    "clippy warnings": [0, 0, 0, 0, 0, 0, 0, 0, 0, 3.3724875838854262],
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


def test_cjk_runs_split_into_bigrams():
    # Chinese has no spaces: before this, a whole sentence was one token and a
    # short query could never match it.
    assert tokenize("完工入库") == ["完工", "工入", "入库", "完工入库"]
    assert tokenize("自建API授权") == ["自建", "api", "授权"]


def test_cjk_query_matches_a_longer_run():
    doc = "完工入库只有提交、审核并回查确认已审核后，才能回传飞书。"
    scores = bm25_scores("完工入库", [doc, "unrelated english text"])
    assert scores[0] > 0
    assert scores[1] == 0
    # A short query still matches a run it is only part of.
    assert bm25_scores("授权", ["自建API授权"])[0] > 0
