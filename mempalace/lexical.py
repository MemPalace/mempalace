"""Tokenizer and Okapi-BM25 scoring shared by the searcher and every backend.

Kept dependency-free so importing it costs nothing on the wake-up path.
"""

import math
import re

TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)

# Scripts written without spaces between words. ``\w{2,}`` reads a whole
# Japanese or Chinese sentence as one token, which no query ever repeats, so a
# run of these characters is split into overlapping character bigrams instead:
# the CJK bigram scheme of Lucene's CJKAnalyzer, which needs no dictionary.
_CJK_CHARS = (
    "\u1100-\u11ff"  # Hangul Jamo
    "\u3005-\u3007"  # 々 〆 〇
    "\u3040-\u309f"  # Hiragana
    "\u30a0-\u30ff"  # Katakana, including the prolonged sound mark ー
    "\u3100-\u312f"  # Bopomofo
    "\u3130-\u318f"  # Hangul Compatibility Jamo
    "\u31a0-\u31bf"  # Bopomofo Extended
    "\u31f0-\u31ff"  # Katakana Phonetic Extensions
    "\u3400-\u4dbf"  # CJK Unified Ideographs Extension A
    "\u4e00-\u9fff"  # CJK Unified Ideographs
    "\ua960-\ua97f"  # Hangul Jamo Extended-A
    "\uac00-\ud7af"  # Hangul Syllables
    "\ud7b0-\ud7ff"  # Hangul Jamo Extended-B
    "\uf900-\ufaff"  # CJK Compatibility Ideographs
    "\uff66-\uff9f"  # Halfwidth Katakana
    "\uffa0-\uffdc"  # Halfwidth Hangul
    "\U00020000-\U000323af"  # Ideograph Extensions B-H and Compatibility Supplement
)
_CJK_RUN_RE = re.compile(f"[{_CJK_CHARS}]+")

# chroma's full-text index uses the trigram tokenizer, which matches nothing
# shorter than three characters.
FTS_MIN_TERM_LEN = 3


def is_cjk(term: str) -> bool:
    """Whether ``term`` holds any character from a script written without spaces."""
    return _CJK_RUN_RE.search(term) is not None


def _split_cjk(token: str):
    """Yield ``(part, is_cjk_run)`` for the CJK runs and the text between them."""
    pos = 0
    for m in _CJK_RUN_RE.finditer(token):
        if m.start() > pos:
            yield token[pos : m.start()], False
        yield m.group(), True
        pos = m.end()
    if pos < len(token):
        yield token[pos:], False


def _ngrams(run: str, n: int) -> list:
    return [run[i : i + n] for i in range(len(run) - n + 1)]


def tokenize(text, stop_words: frozenset = frozenset(), cjk_bigrams: bool = True) -> list:
    """Lowercased word tokens of two or more characters.

    A run of CJK characters becomes its overlapping bigrams (``会議室`` →
    ``会議``, ``議室``); text in other scripts is unchanged. ``cjk_bigrams=False``
    keeps every ``\\w{2,}`` run whole, for callers that must match a store's own
    word tokenizer. ``None`` and empty text give no tokens: Chroma can return
    ``None`` for a drawer without text. ``stop_words`` must already be lowercase.
    """
    if not text:
        return []
    tokens = TOKEN_RE.findall(text.lower())
    if cjk_bigrams:
        split: list = []
        for token in tokens:
            if not is_cjk(token):
                split.append(token)
                continue
            for part, cjk in _split_cjk(token):
                if cjk:
                    split.extend(_ngrams(part, 2))
                elif len(part) >= 2:
                    split.append(part)
        tokens = split
    if stop_words:
        return [t for t in tokens if t not in stop_words]
    return tokens


def fts_terms(query) -> list:
    """Query terms for a trigram full-text index, in query order.

    Words of three or more characters are kept whole, exactly as before CJK
    support. A CJK run becomes its overlapping trigrams, since the index has
    no word boundaries to match a bigram against. Where a CJK run touches
    other letters inside one word (``fts修復``), the trigrams spanning the
    boundary are added too. A two-character run has no trigram of its own:
    :func:`fts_short_terms` returns it for a substring scan instead.
    """
    terms: list = []
    for token in TOKEN_RE.findall((query or "").lower()):
        parts = list(_split_cjk(token))
        for part, cjk in parts:
            if cjk:
                terms.extend(_ngrams(part, FTS_MIN_TERM_LEN))
            elif len(part) >= FTS_MIN_TERM_LEN:
                terms.append(part)
        if len(parts) > 1:
            terms.extend(
                g
                for g in _ngrams(token, FTS_MIN_TERM_LEN)
                if is_cjk(g) and not _CJK_RUN_RE.fullmatch(g)
            )
    return terms


def fts_short_terms(query) -> list:
    """Two-character CJK runs of ``query``, which a trigram index cannot match.

    ``設計``, ``会议`` and ``회의`` are whole words, so callers look them up as
    substrings (``LIKE``) next to the trigram ``MATCH``. A single character is
    left out: it would match nearly every drawer.
    """
    terms: list = []
    for token in TOKEN_RE.findall((query or "").lower()):
        for part, cjk in _split_cjk(token):
            if cjk and len(part) == FTS_MIN_TERM_LEN - 1 and part not in terms:
                terms.append(part)
    return terms


def bm25_scores(
    query: str,
    documents: list,
    k1: float = 1.5,
    b: float = 0.75,
    stop_words: frozenset = frozenset(),
) -> list:
    """Okapi-BM25 score of ``query`` against each document, in order.

    IDF is computed over the given documents with the smoothed form
    ``log((N - df + 0.5) / (df + 0.5) + 1)``, which never goes negative, so it
    ranks a candidate set by how discriminative each term is within it.
    """
    n_docs = len(documents)
    query_terms = set(tokenize(query, stop_words))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [tokenize(d, stop_words) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        for term in set(toks) & query_terms:
            df[term] += 1

    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in query_terms}

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict = {}
        for t in toks:
            if t in query_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores
