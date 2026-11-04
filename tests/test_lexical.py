"""Golden tokenizer and BM25 values for ``mempalace.lexical``.

The scores were produced by the five copies this module replaced (the
searcher and the chroma, sqlite_exact, pgvector and qdrant backends), which
all agreed bit for bit on this corpus. Any change here changes ranking on
every backend, so it must be deliberate.

CJK support changed the Japanese row from one sentence-long token to its
character bigrams. That row's length enters the average document length, so
every score below moved with it; the ranking order did not. Text without CJK
tokenizes exactly as before (``test_text_without_cjk_tokenizes_as_before``).
"""

import pytest

from mempalace.lexical import (
    TOKEN_RE,
    bm25_scores,
    fts_short_terms,
    fts_terms,
    is_cjk,
    tokenize,
)

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
    "kubernetes deploy": [2.1891782135027986, 0, 0, 0, 0, 0, 0, 4.359492133866964, 0, 0],
    "the mat": [1.0945891067513993, 4.336352166092053, 0, 0, 0, 0, 0, 0, 0, 0],
    "caf\xe9": [0, 0, 1.3317793626285084, 0, 1.556657679019814, 0, 0, 0, 0, 0],
    "会議": [0, 0, 0, 2.582779843116934, 0, 0, 0, 0, 0, 0],
    "stra\xdfe \xfcber": [0, 0, 3.5818969252857635, 0, 0, 0, 0, 0, 0, 0],
    "clippy warnings": [0, 0, 0, 0, 0, 0, 0, 0, 0, 3.3406014737320815],
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


def test_text_without_cjk_tokenizes_as_before():
    """CJK support only touches CJK runs: every other text gives the plain
    ``\\w{2,}`` tokens, so palaces without CJK rank bit for bit as before."""
    for doc in CORPUS:
        if doc and not is_cjk(doc):
            assert tokenize(doc) == TOKEN_RE.findall(doc.lower())


@pytest.mark.parametrize(
    "text, tokens",
    [
        ("記憶の整合性", ["記憶", "憶の", "の整", "整合", "合性"]),
        ("FTS修復", ["fts", "修復"]),
        ("rtx_5070ti を確認", ["rtx_5070ti", "を確", "確認"]),
        ("コンテナをdockerで起動", ["コン", "ンテ", "テナ", "ナを", "docker", "で起", "起動"]),
        ("ユーザー", ["ユー", "ーザ", "ザー"]),
        ("会议记录", ["会议", "议记", "记录"]),
        ("회의록 작성", ["회의", "의록", "작성"]),
        # A single CJK character between other letters is dropped, like any
        # one-character word.
        ("Claude Codeのhook", ["claude", "code", "hook"]),
    ],
)
def test_cjk_runs_become_overlapping_bigrams(text, tokens):
    assert tokenize(text) == tokens


def test_cjk_bigrams_can_be_turned_off_for_word_indexes():
    assert tokenize("東京で会議 FTS修復", cjk_bigrams=False) == ["東京で会議", "fts修復"]


def test_cjk_stop_words_drop_bigrams():
    assert tokenize("確認しました", frozenset({"まし", "した"})) == ["確認", "認し", "しま"]


def test_bm25_scores_japanese_text_by_shared_words():
    """A sentence-long token never matched a query before; bigrams let the
    drawer that shares the query's words score above the others."""
    docs = [
        "今日は天気が良いので散歩しました",
        "MemPalace の FTS インデックスを修復して整合性を確認しました",
        "整列の方法を説明します",
    ]
    scores = bm25_scores("インデックスの整合性", docs)
    assert scores[1] > 0
    assert scores[1] == max(scores)
    assert scores[0] == 0


def test_fts_terms_keep_non_cjk_words_as_before():
    query = "Kubernetes rollout on the deploy, a b cd"
    assert fts_terms(query) == [t for t in TOKEN_RE.findall(query.lower()) if len(t) >= 3]


@pytest.mark.parametrize(
    "query, terms",
    [
        ("整合性を確認", ["整合性", "合性を", "性を確", "を確認"]),
        # Two CJK characters cannot be matched by a trigram index on their own.
        ("設計", []),
        # Next to other letters they can, through the trigrams spanning the
        # boundary, when the text writes them the same way.
        ("FTS修復", ["fts", "ts修", "s修復"]),
        ("PostgreSQL 16にアップ", ["postgresql", "にアッ", "アップ", "16に", "6にア"]),
        ("", []),
        (None, []),
    ],
)
def test_fts_terms_split_cjk_into_trigrams(query, terms):
    assert fts_terms(query) == terms


@pytest.mark.parametrize(
    "text, tokens",
    [
        # Simplified and Traditional Chinese
        ("向量数据库的索引", ["向量", "量数", "数据", "据库", "库的", "的索", "索引"]),
        ("會議記錄，明天開會", ["會議", "議記", "記錄", "明天", "天開", "開會"]),
        # Korean writes particles onto the word; bigrams still share 회의 and 의록
        ("회의록을 작성했습니다", ["회의", "의록", "록을", "작성", "성했", "했습", "습니", "니다"]),
        # Bopomofo and halfwidth Hangul
        ("ㄅㄆㄇ", ["ㄅㄆ", "ㄆㄇ"]),
        ("\uffa1\uffa4\uffa7", ["\uffa1\uffa4", "\uffa4\uffa7"]),
    ],
)
def test_chinese_and_korean_runs_become_bigrams(text, tokens):
    assert tokenize(text) == tokens


@pytest.mark.parametrize(
    "text, tokens",
    [
        # The vertical iteration mark repeats the character before it
        ("時\u303b来る", ["時\u303b", "\u303b来", "来る"]),
        # Hentaigana (Kana Supplement) and small kana (Small Kana Extension)
        ("\U0001b002\U0001b003\U0001b004", ["\U0001b002\U0001b003", "\U0001b003\U0001b004"]),
        ("\U0001b150\U0001b151", ["\U0001b150\U0001b151"]),
    ],
)
def test_supplementary_kana_and_iteration_marks_become_bigrams(text, tokens):
    assert tokenize(text) == tokens


@pytest.mark.parametrize(
    "char",
    [
        "\u303b",  # vertical iteration mark
        "\U0001aff1",  # Kana Extended-B
        "\U0001b001",  # Kana Supplement
        "\U0001b132",  # Small Kana Extension
        "\U0002ebf0",  # CJK Unified Ideographs Extension I
        "\U000323b0",  # CJK Unified Ideographs Extension J (Unicode 17)
    ],
)
def test_newer_cjk_blocks_count_as_cjk(char):
    # is_cjk reads the range table directly, so it holds on any Python version
    # even where the runtime's Unicode database predates the block.
    assert is_cjk(char)


def test_bm25_ranks_chinese_and_korean_by_shared_words():
    zh = ["今天天气很好", "向量数据库需要重建索引", "数据很重要"]
    zh_scores = bm25_scores("重建索引", zh)
    assert zh_scores.index(max(zh_scores)) == 1 and zh_scores[0] == 0
    ko = ["오늘 날씨가 좋다", "회의록을 작성했습니다", "회사 일정 공유"]
    ko_scores = bm25_scores("회의록", ko)
    assert ko_scores.index(max(ko_scores)) == 1 and ko_scores[0] == 0


@pytest.mark.parametrize(
    "query, terms",
    [
        ("設計", ["設計"]),
        ("設計 レビュー", ["設計"]),
        ("会议 记录", ["会议", "记录"]),
        ("회의 일정", ["회의", "일정"]),
        ("FTS修復", ["修復"]),
        # Runs of three or more go to the trigram query; one character is too broad.
        ("整合性 の", []),
        ("設計 設計", ["設計"]),
        ("deploy", []),
        (None, []),
    ],
)
def test_fts_short_terms_are_two_character_cjk_runs(query, terms):
    assert fts_short_terms(query) == terms
