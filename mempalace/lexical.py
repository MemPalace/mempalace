"""Tokenizer and Okapi-BM25 scoring shared by the searcher and every backend.

Kept dependency-free so importing it costs nothing on the wake-up path.
"""

import math
import re

TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


def tokenize(text, stop_words: frozenset = frozenset()) -> list:
    """Lowercased word tokens of two or more characters.

    ``None`` and empty text give no tokens: Chroma can return ``None`` for a
    drawer without text. ``stop_words`` must already be lowercase.
    """
    if not text:
        return []
    tokens = TOKEN_RE.findall(text.lower())
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
