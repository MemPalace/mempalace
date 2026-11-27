"""Tokenizer and Okapi-BM25 scoring shared by the searcher and every backend.

Kept dependency-free so importing it costs nothing on the wake-up path.
"""

import math
import re

TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
# Chinese and Japanese are written without spaces, so ``\w{2,}`` turns a whole
# phrase into one token and a short query can never match it. Splitting those
# runs into overlapping character bigrams gives BM25 something to match
# without pulling in a segmentation dependency.
CJK_RUN_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _cjk_tokens(run: str) -> list:
    """Character bigrams of a CJK run, plus the run itself when long enough.

    The whole run is kept because the sqlite FTS fallback discards tokens
    shorter than three characters; without it a CJK query would leave that
    path with no terms at all.
    """
    if len(run) == 1:
        return [run]
    tokens = [run[i : i + 2] for i in range(len(run) - 1)]
    if len(run) > 2:
        tokens.append(run)
    return tokens


def tokenize(text, stop_words: frozenset = frozenset()) -> list:
    """Lowercased word tokens of two or more characters.

    ``None`` and empty text give no tokens: Chroma can return ``None`` for a
    drawer without text. ``stop_words`` must already be lowercase.

    Runs of CJK characters are split into bigrams (see ``_cjk_tokens``) so a
    query shorter than the sentence it appears in can still match.
    """
    if not text:
        return []
    tokens = []
    for match in TOKEN_RE.finditer(text.lower()):
        run = match.group()
        pos = 0
        for cjk in CJK_RUN_RE.finditer(run):
            if cjk.start() > pos:
                tokens.append(run[pos : cjk.start()])
            tokens.extend(_cjk_tokens(cjk.group()))
            pos = cjk.end()
        if pos < len(run):
            tokens.append(run[pos:])
    if stop_words:
        return [t for t in tokens if t not in stop_words]
    return tokens


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
