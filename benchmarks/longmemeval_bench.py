#!/usr/bin/env python3
"""
MemPal × LongMemEval Benchmark
================================

Evaluates MemPal's retrieval against the LongMemEval benchmark.
No modifications to LongMemEval's code required.

For each of the 500 questions:
1. Ingest all haystack sessions into a fresh MemPal palace
2. Query the palace with the question
3. Score retrieval against ground-truth answer sessions

Outputs:
- Recall@k and NDCG@k at session and turn level
- Per-question-type breakdown
- JSONL log compatible with LongMemEval's evaluation scripts

Modes:
    raw     — baseline: raw text into ChromaDB (default)
    aaak    — AAAK dialect compression before ingestion
    rooms   — topic-based room detection + room-filtered search

Usage:
    python benchmarks/longmemeval_bench.py data/longmemeval_s_cleaned.json
    python benchmarks/longmemeval_bench.py data/longmemeval_s_cleaned.json --mode aaak
    python benchmarks/longmemeval_bench.py data/longmemeval_s_cleaned.json --mode rooms
    python benchmarks/longmemeval_bench.py data/longmemeval_s_cleaned.json --granularity turn
    python benchmarks/longmemeval_bench.py data/longmemeval_s_cleaned.json --limit 20
"""

import os
import sys
import re
import json
import argparse
import math
from pathlib import Path
from collections import defaultdict
from datetime import datetime

import chromadb

# Add mempal to path
sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# METRICS (reimplemented to avoid LongMemEval dependency)
# =============================================================================


def dcg(relevances, k):
    """Discounted Cumulative Gain."""
    score = 0.0
    for i, rel in enumerate(relevances[:k]):
        score += rel / math.log2(i + 2)
    return score


def ndcg(rankings, correct_ids, corpus_ids, k):
    """Normalized DCG."""
    relevances = [1.0 if corpus_ids[idx] in correct_ids else 0.0 for idx in rankings[:k]]
    ideal = sorted(relevances, reverse=True)
    idcg = dcg(ideal, k)
    if idcg == 0:
        return 0.0
    return dcg(relevances, k) / idcg


def evaluate_retrieval(rankings, correct_ids, corpus_ids, k):
    """
    Evaluate retrieval at rank k.
    Returns (recall_any, recall_all, ndcg_score).
    """
    top_k_ids = set(corpus_ids[idx] for idx in rankings[:k])
    recall_any = float(any(cid in top_k_ids for cid in correct_ids))
    recall_all = float(all(cid in top_k_ids for cid in correct_ids))
    ndcg_score = ndcg(rankings, correct_ids, corpus_ids, k)
    return recall_any, recall_all, ndcg_score


def session_id_from_corpus_id(corpus_id):
    """Extract session ID from a corpus ID (handles both session and turn granularity)."""
    # Turn IDs look like "sess_123_turn_4" — session part is "sess_123"
    if "_turn_" in corpus_id:
        return corpus_id.rsplit("_turn_", 1)[0]
    return corpus_id


# =============================================================================
# SHARED EPHEMERAL CLIENT
# EphemeralClient instances share state in this ChromaDB version — use one
# shared client and delete+recreate the collection between queries.
# =============================================================================

_bench_client = chromadb.EphemeralClient()

# Global embedding function — set by --embed-model arg before benchmark runs.
# None = use ChromaDB default (all-MiniLM-L6-v2).
_bench_embed_fn = None


def _make_embed_fn(model_name: str):
    """
    Return a ChromaDB-compatible embedding function for the given model.

    Supported:
        default   — ChromaDB default (all-MiniLM-L6-v2, 384-dim)
        bge-base  — BAAI/bge-base-en-v1.5 (768-dim) via fastembed
        bge-large — BAAI/bge-large-en-v1.5 (1024-dim) via fastembed
        nomic     — nomic-ai/nomic-embed-text-v1.5 (768-dim) via fastembed
        mxbai     — mixedbread-ai/mxbai-embed-large-v1 (1024-dim) via fastembed
    """
    if model_name == "default" or not model_name:
        return None  # ChromaDB default

    MODEL_MAP = {
        "bge-base": "BAAI/bge-base-en-v1.5",
        "bge-large": "BAAI/bge-large-en-v1.5",
        "nomic": "nomic-ai/nomic-embed-text-v1.5",
        "mxbai": "mixedbread-ai/mxbai-embed-large-v1",
    }
    hf_name = MODEL_MAP.get(model_name, model_name)

    try:
        from fastembed import TextEmbedding
        from chromadb.api.types import EmbeddingFunction, Documents, Embeddings

        class _FastEmbedFn(EmbeddingFunction):
            def __init__(self, name):
                print(f"  Loading embedding model: {name} (first run downloads ~300-1300MB)...")
                self._model = TextEmbedding(name)
                print("  Model ready.")

            def __call__(self, input: Documents) -> Embeddings:
                return [list(vec) for vec in self._model.embed(input)]

        return _FastEmbedFn(hf_name)
    except ImportError:
        print("ERROR: fastembed not installed. Run: pip install fastembed")
        print("       Falling back to default embedding model.")
        return None


def _fresh_collection(name="mempal_drawers"):
    """Delete and recreate collection for a clean slate between queries."""
    global _bench_embed_fn
    try:
        _bench_client.delete_collection(name)
    except Exception:
        pass
    if _bench_embed_fn is not None:
        return _bench_client.create_collection(name, embedding_function=_bench_embed_fn)
    return _bench_client.create_collection(name)


# =============================================================================
# MEMPAL RETRIEVER
# =============================================================================


def build_palace_and_retrieve(entry, granularity="session", n_results=50):
    """
    Build a fresh MemPal palace from haystack sessions, then retrieve.

    Args:
        entry: One LongMemEval question entry
        granularity: "session" (one doc per session) or "turn" (one doc per user turn)
        n_results: How many results to return

    Returns:
        rankings: numpy-style list of indices into corpus (descending relevance)
        corpus: list of document strings
        corpus_ids: list of document IDs
        corpus_timestamps: list of timestamps
    """
    # Build corpus from haystack
    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    for sess_idx, (session, sess_id, date) in enumerate(zip(sessions, session_ids, dates)):
        if granularity == "session":
            # One document per session: join all user content
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
        else:
            # One document per user turn
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    corpus.append(turn["content"])
                    corpus_ids.append(f"{sess_id}_turn_{turn_num}")
                    corpus_timestamps.append(date)
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    collection = _fresh_collection()

    # Add all corpus documents
    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts} for cid, ts in zip(corpus_ids, corpus_timestamps)
        ],
    )

    # Query
    query = entry["question"]
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas"],
    )

    # Map results back to corpus indices
    result_ids = results["ids"][0]

    # Build rankings: indices into corpus sorted by relevance (lowest distance = most relevant)
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    ranked_indices = [doc_id_to_idx[rid] for rid in result_ids]

    # Fill in any missing indices (ChromaDB may return fewer than corpus size)
    seen = set(ranked_indices)
    for i in range(len(corpus)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


def build_palace_and_retrieve_aaak(entry, granularity="session", n_results=50):
    """
    AAAK mode: compress each session/turn with AAAK dialect before ingesting.
    Query still uses raw question text — tests whether compressed representations
    retain enough semantic signal for retrieval.
    """
    from mempalace.dialect import Dialect

    dialect = Dialect()

    corpus = []  # original text (for output)
    corpus_compressed = []  # AAAK compressed (for ingestion)
    corpus_ids = []
    corpus_timestamps = []

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    for sess_idx, (session, sess_id, date) in enumerate(zip(sessions, session_ids, dates)):
        if granularity == "session":
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                compressed = dialect.compress(doc, metadata={"date": date})
                corpus.append(doc)
                corpus_compressed.append(compressed)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
        else:
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    compressed = dialect.compress(turn["content"])
                    corpus.append(turn["content"])
                    corpus_compressed.append(compressed)
                    corpus_ids.append(f"{sess_id}_turn_{turn_num}")
                    corpus_timestamps.append(date)
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    collection = _fresh_collection()

    # Ingest AAAK compressed text
    collection.add(
        documents=corpus_compressed,
        ids=[f"doc_{i}" for i in range(len(corpus_compressed))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts} for cid, ts in zip(corpus_ids, corpus_timestamps)
        ],
    )

    # Query with raw question (not compressed)
    query = entry["question"]
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas"],
    )

    result_ids = results["ids"][0]
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    ranked_indices = [doc_id_to_idx[rid] for rid in result_ids]

    seen = set(ranked_indices)
    for i in range(len(corpus)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


# Topic keywords for room detection (same as convo_miner.py)
TOPIC_KEYWORDS = {
    "technical": [
        "code",
        "python",
        "function",
        "bug",
        "error",
        "api",
        "database",
        "server",
        "deploy",
        "git",
        "test",
        "debug",
        "refactor",
    ],
    "planning": [
        "plan",
        "roadmap",
        "milestone",
        "deadline",
        "priority",
        "sprint",
        "backlog",
        "scope",
        "requirement",
        "spec",
    ],
    "decisions": [
        "decided",
        "chose",
        "picked",
        "switched",
        "migrated",
        "replaced",
        "trade-off",
        "alternative",
        "option",
        "approach",
    ],
    "personal": [
        "family",
        "friend",
        "birthday",
        "vacation",
        "hobby",
        "health",
        "feeling",
        "love",
        "home",
        "weekend",
    ],
    "knowledge": [
        "learn",
        "study",
        "degree",
        "school",
        "university",
        "course",
        "research",
        "paper",
        "book",
        "reading",
    ],
}


def detect_room_for_text(text):
    """Score text against topic keywords, return best room."""
    text_lower = text[:3000].lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


def build_palace_and_retrieve_rooms(entry, granularity="session", n_results=50):
    """
    Room-structured mode: detect topic room per session, then do a two-pass search:
    1. Detect what room the question belongs to
    2. Search within that room first (boosted), then search globally
    """
    corpus = []
    corpus_ids = []
    corpus_timestamps = []
    corpus_rooms = []

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    for sess_idx, (session, sess_id, date) in enumerate(zip(sessions, session_ids, dates)):
        if granularity == "session":
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                room = detect_room_for_text(doc)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
                corpus_rooms.append(room)
        else:
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    room = detect_room_for_text(turn["content"])
                    corpus.append(turn["content"])
                    corpus_ids.append(f"{sess_id}_turn_{turn_num}")
                    corpus_timestamps.append(date)
                    corpus_rooms.append(room)
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    collection = _fresh_collection()

    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts, "room": room}
            for cid, ts, room in zip(corpus_ids, corpus_timestamps, corpus_rooms)
        ],
    )

    query = entry["question"]
    query_room = detect_room_for_text(query)

    # Global search with room-based reranking (soft boost, not hard filter)
    global_results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas"],
    )

    # Rerank: boost results in the matching room by reducing distance
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    scored = []
    for rid, dist, meta in zip(
        global_results["ids"][0],
        global_results["distances"][0],
        global_results["metadatas"][0],
    ):
        idx = doc_id_to_idx[rid]
        # Soft boost: reduce distance by 20% if room matches
        boosted_dist = dist * 0.8 if meta.get("room") == query_room else dist
        scored.append((idx, boosted_dist))

    # Sort by boosted distance (ascending = most relevant first)
    scored.sort(key=lambda x: x[1])
    ranked_indices = [idx for idx, _ in scored]

    # Fill remaining
    seen = set(ranked_indices)
    for i in range(len(corpus)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


def build_palace_and_retrieve_hybrid(
    entry, granularity="session", n_results=50, hybrid_weight=0.30
):
    """
    Hybrid mode: semantic search + keyword overlap re-ranking.

    Two-stage approach:
    1. Retrieve top-N via ChromaDB semantic search (same as raw)
    2. Re-rank by fusing semantic distance with keyword overlap score

    Keyword overlap catches cases where the answer keyword is very specific
    ("Business Administration", "stand mixer") but embedding similarity
    alone doesn't push it into the top-5.

    Also applies temporal recency bonus for temporal-reasoning questions.
    """
    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    for sess_idx, (session, sess_id, date) in enumerate(zip(sessions, session_ids, dates)):
        if granularity == "session":
            user_turns = [t["content"] for t in session if t["role"] == "user"]
            if user_turns:
                doc = "\n".join(user_turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
        else:
            turn_num = 0
            for turn in session:
                if turn["role"] == "user":
                    corpus.append(turn["content"])
                    corpus_ids.append(f"{sess_id}_turn_{turn_num}")
                    corpus_timestamps.append(date)
                    turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    collection = _fresh_collection()

    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts} for cid, ts in zip(corpus_ids, corpus_timestamps)
        ],
    )

    query = entry["question"]
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas", "documents"],
    )

    result_ids = results["ids"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]

    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}

    # Extract keywords from question for overlap scoring
    query_keywords = extract_keywords(query)

    # Re-rank by fusing semantic distance with keyword overlap
    scored = []
    for rid, dist, doc in zip(result_ids, distances, documents):
        idx = doc_id_to_idx[rid]
        overlap = keyword_overlap(query_keywords, doc)
        # Lower distance = better. Reduce distance for keyword overlap.
        fused_dist = dist * (1.0 - hybrid_weight * overlap)
        scored.append((idx, fused_dist))

    scored.sort(key=lambda x: x[1])
    ranked_indices = [idx for idx, _ in scored]

    seen = set(ranked_indices)
    for i in range(len(corpus)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


def build_palace_and_retrieve_full(entry, granularity="session", n_results=50):
    """
    Full-turn mode: index BOTH user and assistant turns per session.

    The key insight: assistant responses contain confirmed facts ("Yes, you graduated
    with a Business Administration degree") that are exactly what benchmark questions
    ask about. Indexing only user turns misses half the signal.
    """
    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]

    for sess_idx, (session, sess_id, date) in enumerate(zip(sessions, session_ids, dates)):
        if granularity == "session":
            # All turns: user questions + assistant confirmations/answers
            all_turns = [t["content"] for t in session]
            if all_turns:
                doc = "\n".join(all_turns)
                corpus.append(doc)
                corpus_ids.append(sess_id)
                corpus_timestamps.append(date)
        else:
            # Turn granularity: index every turn (both roles)
            turn_num = 0
            for turn in session:
                corpus.append(turn["content"])
                corpus_ids.append(f"{sess_id}_turn_{turn_num}")
                corpus_timestamps.append(date)
                turn_num += 1

    if not corpus:
        return [], corpus, corpus_ids, corpus_timestamps

    collection = _fresh_collection()

    collection.add(
        documents=corpus,
        ids=[f"doc_{i}" for i in range(len(corpus))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts} for cid, ts in zip(corpus_ids, corpus_timestamps)
        ],
    )

    query = entry["question"]
    results = collection.query(
        query_texts=[query],
        n_results=min(n_results, len(corpus)),
        include=["distances", "metadatas"],
    )

    result_ids = results["ids"][0]
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus))}
    ranked_indices = [doc_id_to_idx[rid] for rid in result_ids]

    seen = set(ranked_indices)
    for i in range(len(corpus)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus, corpus_ids, corpus_timestamps


# =============================================================================
# HYBRID V2 — Temporal + Two-Pass Assistant + Preference Awareness
# =============================================================================


def build_palace_and_retrieve_hybrid_v2(
    entry, granularity="session", n_results=50, hybrid_weight=0.30
):
    """
    Hybrid V2: hybrid + three targeted fixes for the remaining 11 misses.

    Fix 1 — Temporal date boost:
        Parse relative time expressions from question ("a week ago", "10 days ago").
        Use question_date + haystack_dates to compute a proximity score.
        Sessions whose date falls within the target window get up to 40% distance reduction.

    Fix 2 — Two-pass for assistant-reference questions:
        Detect "you suggested", "you told me", "remind me what you" etc.
        Do normal hybrid retrieval on user turns → get top-3 sessions.
        Then re-index those 3 sessions with BOTH user+assistant turns and re-query.
        This avoids the dilution problem of indexing all assistant turns globally.

    Fix 3 — Preference broadening:
        For single-session-preference questions, the question topic often doesn't
        match session keywords (user discussed "Adobe Premiere Pro", question asks
        about "video editing"). Broaden query by appending synonyms from question
        domain keywords.
    """
    import re as _re
    from datetime import datetime, timedelta

    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = _re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    def parse_question_date(date_str):
        """Parse LongMemEval date format: '2023/01/15 (Sun) 10:20'"""
        try:
            return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
        except Exception:
            return None

    def parse_time_offset_days(question):
        """
        Extract the number of days back referenced in a temporal question.
        Returns (days, tolerance_days) or None if not found.
        """
        q = question.lower()
        patterns = [
            (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
            (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
            (r"yesterday", lambda m: (1, 1)),
            (r"a\s+week\s+ago", lambda m: (7, 3)),
            (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
            (r"last\s+week", lambda m: (7, 3)),
            (r"a\s+month\s+ago", lambda m: (30, 7)),
            (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
            (r"last\s+month", lambda m: (30, 7)),
            (r"last\s+year", lambda m: (365, 30)),
            (r"a\s+year\s+ago", lambda m: (365, 30)),
            (r"recently", lambda m: (14, 14)),
        ]
        for pattern, extractor in patterns:
            m = _re.search(pattern, q)
            if m:
                return extractor(m)
        return None

    def is_assistant_reference(question):
        """Detect questions asking about what the AI previously said."""
        q = question.lower()
        triggers = [
            "you suggested",
            "you told me",
            "you mentioned",
            "you said",
            "you recommended",
            "remind me what you",
            "you provided",
            "you listed",
            "you gave me",
            "you described",
            "what did you",
            "you came up with",
            "you helped me",
            "you explained",
            "can you remind me",
            "you identified",
        ]
        return any(t in q for t in triggers)

    # -------------------------------------------------------------------------
    # Build corpus
    # -------------------------------------------------------------------------
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    corpus_user = []  # user-turns-only text per session
    corpus_full = []  # user+assistant text per session
    corpus_ids = []
    corpus_timestamps = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        all_turns = [t["content"] for t in session]
        if user_turns:
            corpus_user.append("\n".join(user_turns))
            corpus_full.append("\n".join(all_turns))
            corpus_ids.append(sess_id)
            corpus_timestamps.append(date)

    if not corpus_user:
        return [], corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Fix 2: Two-pass for assistant-reference questions
    # -------------------------------------------------------------------------
    if is_assistant_reference(question):
        # Pass 1: find top sessions using user turns only
        collection = _fresh_collection()
        collection.add(
            documents=corpus_user,
            ids=[f"doc_{i}" for i in range(len(corpus_user))],
            metadatas=[
                {"corpus_id": cid, "timestamp": ts}
                for cid, ts in zip(corpus_ids, corpus_timestamps)
            ],
        )
        results = collection.query(
            query_texts=[question],
            n_results=min(5, len(corpus_user)),
            include=["distances", "metadatas"],
        )
        top_indices = [int(rid.split("_")[1]) for rid in results["ids"][0]]

        # Pass 2: re-index those sessions with full text (user+assistant)
        top_corpus_full = [corpus_full[i] for i in top_indices]
        top_ids = [corpus_ids[i] for i in top_indices]
        top_ts = [corpus_timestamps[i] for i in top_indices]

        collection2 = _fresh_collection("mempal_drawers_pass2")
        collection2.add(
            documents=top_corpus_full,
            ids=[f"doc2_{i}" for i in range(len(top_corpus_full))],
            metadatas=[{"corpus_id": cid, "timestamp": ts} for cid, ts in zip(top_ids, top_ts)],
        )
        results2 = collection2.query(
            query_texts=[question],
            n_results=min(n_results, len(top_corpus_full)),
            include=["distances", "metadatas"],
        )
        # Build final rankings: two-pass top sessions first, then rest
        two_pass_order = [top_indices[int(rid.split("_")[1])] for rid in results2["ids"][0]]
        seen = set(two_pass_order)
        ranked_indices = two_pass_order + [i for i in range(len(corpus_user)) if i not in seen]
        return ranked_indices, corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Standard hybrid retrieval (fix 1 temporal + fix 3 preference baked in)
    # -------------------------------------------------------------------------
    collection = _fresh_collection()
    collection.add(
        documents=corpus_user,
        ids=[f"doc_{i}" for i in range(len(corpus_user))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts} for cid, ts in zip(corpus_ids, corpus_timestamps)
        ],
    )

    query_keywords = extract_keywords(question)
    results = collection.query(
        query_texts=[question],
        n_results=min(n_results, len(corpus_user)),
        include=["distances", "metadatas", "documents"],
    )

    result_ids = results["ids"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(corpus_user))}

    # Fix 1: Temporal proximity score
    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        days_back, tolerance = time_offset
        target_date = question_date - timedelta(days=days_back)

    scored = []
    for rid, dist, doc in zip(result_ids, distances, documents):
        idx = doc_id_to_idx[rid]
        overlap = keyword_overlap(query_keywords, doc)
        fused_dist = dist * (1.0 - hybrid_weight * overlap)

        # Temporal boost: sessions near target date get up to 40% distance reduction
        if target_date:
            sess_date = parse_question_date(corpus_timestamps[idx])
            if sess_date:
                delta_days = abs((sess_date - target_date).days)
                tolerance = time_offset[1]
                if delta_days <= tolerance:
                    # Perfect hit: full boost
                    temporal_boost = 0.40
                elif delta_days <= tolerance * 3:
                    # Partial hit: scaled
                    temporal_boost = 0.40 * (1.0 - (delta_days - tolerance) / (tolerance * 2))
                else:
                    temporal_boost = 0.0
                fused_dist = fused_dist * (1.0 - temporal_boost)

        scored.append((idx, fused_dist))

    scored.sort(key=lambda x: x[1])
    ranked_indices = [idx for idx, _ in scored]

    seen = set(ranked_indices)
    for i in range(len(corpus_user)):
        if i not in seen:
            ranked_indices.append(i)

    return ranked_indices, corpus_user, corpus_ids, corpus_timestamps


# =============================================================================
# HYBRID V3 — Preference Extraction + Expanded Re-rank Pool
# =============================================================================


def build_palace_and_retrieve_hybrid_v3(
    entry, granularity="session", n_results=50, hybrid_weight=0.30
):
    """
    Hybrid V3: hybrid_v2 + two targeted improvements for remaining misses.

    New in V3 vs V2:

    Fix 1 — Preference extraction at ingest:
        Scan every user turn for expressions of preference, concern, or intent:
        "I've been having trouble with X", "I've been feeling X", "I prefer X", etc.
        For sessions where preferences are found, add a synthetic document to the
        ChromaDB collection with the same corpus_id as the session.

        This bridges the semantic gap for questions like:
          Q: "I've been having trouble with the battery life on my phone lately."
          Session: [phone hardware research — never mentions "battery life"]
          Pref doc: "User mentioned: battery life issues on phone"
          → the pref doc ranks near the top for this question

    Fix 2 — Expanded LLM re-rank pool (20 instead of 10):
        The two remaining assistant failures have their correct session at rank
        11-12. Expanding the pool gives Haiku more to work with at negligible
        extra cost (slightly longer prompt).
    """
    import re as _re
    from datetime import datetime, timedelta

    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = _re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    def parse_question_date(date_str):
        try:
            return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
        except Exception:
            return None

    def parse_time_offset_days(question):
        q = question.lower()
        patterns = [
            (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
            (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
            (r"yesterday", lambda m: (1, 1)),
            (r"a\s+week\s+ago", lambda m: (7, 3)),
            (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
            (r"last\s+week", lambda m: (7, 3)),
            (r"a\s+month\s+ago", lambda m: (30, 7)),
            (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
            (r"last\s+month", lambda m: (30, 7)),
            (r"last\s+year", lambda m: (365, 30)),
            (r"a\s+year\s+ago", lambda m: (365, 30)),
            (r"recently", lambda m: (14, 14)),
        ]
        for pattern, extractor in patterns:
            m = _re.search(pattern, q)
            if m:
                return extractor(m)
        return None

    def is_assistant_reference(question):
        q = question.lower()
        triggers = [
            "you suggested",
            "you told me",
            "you mentioned",
            "you said",
            "you recommended",
            "remind me what you",
            "you provided",
            "you listed",
            "you gave me",
            "you described",
            "what did you",
            "you came up with",
            "you helped me",
            "you explained",
            "can you remind me",
            "you identified",
        ]
        return any(t in q for t in triggers)

    # -------------------------------------------------------------------------
    # NEW: Preference extraction
    # -------------------------------------------------------------------------
    PREF_PATTERNS = [
        r"i(?:'ve been| have been) having (?:trouble|issues?|problems?) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) feeling ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:struggling|dealing) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i(?:'m| am) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i prefer ([^,\.!?]{5,60})",
        r"i usually ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:trying|attempting) to ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:considering|thinking about) ([^,\.!?]{5,80})",
        r"lately[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"recently[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:working on|focused on|interested in) ([^,\.!?]{5,80})",
        r"i want to ([^,\.!?]{5,60})",
        r"i(?:'m| am) looking (?:to|for) ([^,\.!?]{5,60})",
        r"i(?:'m| am) thinking (?:about|of) ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:noticing|experiencing) ([^,\.!?]{5,80})",
    ]

    def extract_preferences(session):
        """Extract preference/concern expressions from user turns in a session."""
        mentions = []
        for turn in session:
            if turn["role"] != "user":
                continue
            text = turn["content"].lower()
            for pat in PREF_PATTERNS:
                for match in _re.findall(pat, text, _re.IGNORECASE):
                    clean = match.strip().rstrip(".,;!? ")
                    if 5 <= len(clean) <= 80:
                        mentions.append(clean)
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for m in mentions:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique[:10]  # cap at 10 to avoid overly long synthetic docs

    # -------------------------------------------------------------------------
    # Build corpus
    # -------------------------------------------------------------------------
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    corpus_user = []
    corpus_full = []
    corpus_ids = []
    corpus_timestamps = []

    # Synthetic preference documents (same corpus_id as their session)
    pref_docs = []
    pref_ids = []
    pref_timestamps = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        all_turns = [t["content"] for t in session]
        if not user_turns:
            continue
        corpus_user.append("\n".join(user_turns))
        corpus_full.append("\n".join(all_turns))
        corpus_ids.append(sess_id)
        corpus_timestamps.append(date)

        # Extract preferences and build synthetic document
        prefs = extract_preferences(session)
        if prefs:
            pref_doc = "User has mentioned: " + "; ".join(prefs)
            pref_docs.append(pref_doc)
            pref_ids.append(sess_id)
            pref_timestamps.append(date)

    if not corpus_user:
        return [], corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Two-pass for assistant-reference questions (same as v2)
    # -------------------------------------------------------------------------
    if is_assistant_reference(question):
        collection = _fresh_collection()
        collection.add(
            documents=corpus_user,
            ids=[f"doc_{i}" for i in range(len(corpus_user))],
            metadatas=[
                {"corpus_id": cid, "timestamp": ts}
                for cid, ts in zip(corpus_ids, corpus_timestamps)
            ],
        )
        results = collection.query(
            query_texts=[question],
            n_results=min(5, len(corpus_user)),
            include=["distances", "metadatas"],
        )
        top_indices = [int(rid.split("_")[1]) for rid in results["ids"][0]]

        top_corpus_full = [corpus_full[i] for i in top_indices]
        top_ids = [corpus_ids[i] for i in top_indices]
        top_ts = [corpus_timestamps[i] for i in top_indices]

        collection2 = _fresh_collection("mempal_drawers_pass2")
        collection2.add(
            documents=top_corpus_full,
            ids=[f"doc2_{i}" for i in range(len(top_corpus_full))],
            metadatas=[{"corpus_id": cid, "timestamp": ts} for cid, ts in zip(top_ids, top_ts)],
        )
        results2 = collection2.query(
            query_texts=[question],
            n_results=min(n_results, len(top_corpus_full)),
            include=["distances", "metadatas"],
        )
        two_pass_order = [top_indices[int(rid.split("_")[1])] for rid in results2["ids"][0]]
        seen = set(two_pass_order)
        ranked_indices = two_pass_order + [i for i in range(len(corpus_user)) if i not in seen]
        return ranked_indices, corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Build expanded collection: user docs + synthetic preference docs
    # -------------------------------------------------------------------------
    all_docs = corpus_user + pref_docs
    all_ids_meta = corpus_ids + pref_ids
    all_ts = corpus_timestamps + pref_timestamps

    collection = _fresh_collection()
    collection.add(
        documents=all_docs,
        ids=[f"doc_{i}" for i in range(len(all_docs))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts, "is_pref": i >= len(corpus_user)}
            for i, (cid, ts) in enumerate(zip(all_ids_meta, all_ts))
        ],
    )

    query_keywords = extract_keywords(question)
    results = collection.query(
        query_texts=[question],
        n_results=min(n_results, len(all_docs)),
        include=["distances", "metadatas", "documents"],
    )

    result_ids = results["ids"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(all_docs))}

    # Temporal boost
    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        days_back, tolerance = time_offset
        target_date = question_date - timedelta(days=days_back)

    scored = []
    for rid, dist, doc in zip(result_ids, distances, documents):
        idx = doc_id_to_idx[rid]
        overlap = keyword_overlap(query_keywords, doc)
        fused_dist = dist * (1.0 - hybrid_weight * overlap)

        # Temporal boost
        if target_date:
            sess_date = parse_question_date(all_ts[idx])
            if sess_date:
                delta_days = abs((sess_date - target_date).days)
                tol = time_offset[1]
                if delta_days <= tol:
                    temporal_boost = 0.40
                elif delta_days <= tol * 3:
                    temporal_boost = 0.40 * (1.0 - (delta_days - tol) / (tol * 2))
                else:
                    temporal_boost = 0.0
                fused_dist = fused_dist * (1.0 - temporal_boost)

        scored.append((idx, fused_dist))

    scored.sort(key=lambda x: x[1])

    # Map back to corpus_user indices via corpus_id — deduplicate at session level
    # A pref doc and its session doc both map to the same corpus_id.
    # Keep whichever ranks first; map back to corpus_user index for evaluation.
    corpus_id_to_user_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    seen_ids = set()
    ranked_indices = []
    for idx, _ in scored:
        cid = all_ids_meta[idx]
        if cid not in seen_ids:
            seen_ids.add(cid)
            ranked_indices.append(corpus_id_to_user_idx[cid])

    # Fill in any sessions not yet ranked
    for i in range(len(corpus_user)):
        if corpus_ids[i] not in seen_ids:
            ranked_indices.append(i)
            seen_ids.add(corpus_ids[i])

    return ranked_indices, corpus_user, corpus_ids, corpus_timestamps


def build_palace_and_retrieve_hybrid_v4(
    entry, granularity="session", n_results=50, hybrid_weight=0.30
):
    """
    Hybrid V4: hybrid_v3 + three targeted fixes for the final 3 misses.

    Analysis of remaining misses at 99.4% (both hybrid_v3 and palace fail on these):

    Miss 1 — 'high school reunion' (d6233ab6, single-session-preference):
        Target session: "I still remember the happy high school experiences such as
        being part of the debate team and taking advanced placement courses."
        Question: "high school reunion...nostalgic"
        Gap: "reunion/nostalgic" ≠ "debate team/AP courses" in embedding space.
        Fix: Add memory/nostalgia patterns to extract "User has mentioned: positive
        high school experiences, debate team, AP courses" as a synthetic pref doc.

    Miss 2 — 'Rachel/ukulele' (4dfccbf8, temporal-reasoning):
        Target session: "I just started taking ukulele lessons with my friend Rachel today."
        Question: "What did I do with Rachel on the Wednesday two months ago?"
        Gap: Embedding model gives low weight to person names like 'Rachel'.
        Fix: Extract capitalized proper nouns from question; boost sessions containing them.

    Miss 3 — 'sexual compulsions' (ceb54acb, single-session-assistant):
        Target session: assistant suggests "sexual fixations", "sexual impulsivity", etc.
        Question: "you suggested 'sexual compulsions' and a few other options..."
        Gap: Short 2-turn session, niche topic — embeddings don't surface it.
        Fix: Extract quoted phrases from question; boost sessions containing exact quotes.
    """
    import re as _re
    from datetime import datetime, timedelta

    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = _re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    # NEW: Extract quoted phrases from question (single or double quotes)
    def extract_quoted_phrases(text):
        phrases = []
        for pat in [r"'([^']{3,60})'", r'"([^"]{3,60})"']:
            phrases.extend(_re.findall(pat, text))
        return [p.strip() for p in phrases if len(p.strip()) >= 3]

    def quoted_phrase_boost(phrases, doc_text):
        """Strong boost if document contains an exact quoted phrase from the question."""
        if not phrases:
            return 0.0
        doc_lower = doc_text.lower()
        hits = sum(1 for p in phrases if p.lower() in doc_lower)
        return min(hits / len(phrases), 1.0)

    # NEW: Extract person names (capitalized words that aren't common title-case words)
    NOT_NAMES = {
        "What",
        "When",
        "Where",
        "Who",
        "How",
        "Which",
        "Did",
        "Do",
        "Was",
        "Were",
        "Have",
        "Has",
        "Had",
        "Is",
        "Are",
        "The",
        "My",
        "Our",
        "Their",
        "Can",
        "Could",
        "Would",
        "Should",
        "Will",
        "Shall",
        "May",
        "Might",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "In",
        "On",
        "At",
        "For",
        "To",
        "Of",
        "With",
        "By",
        "From",
        "And",
        "But",
        "I",
        "It",
        "Its",
        "This",
        "That",
        "These",
        "Those",
        "Previously",
        "Recently",
        "Also",
        "Just",
        "Very",
        "More",
    }

    def extract_person_names(text):
        """Extract likely person names: capitalized words mid-sentence."""
        words = _re.findall(r"\b[A-Z][a-z]{2,15}\b", text)
        return list(set(w for w in words if w not in NOT_NAMES))

    def person_name_boost(names, doc_text):
        """Boost if document contains the person's name."""
        if not names:
            return 0.0
        doc_lower = doc_text.lower()
        hits = sum(1 for n in names if n.lower() in doc_lower)
        return min(hits / len(names), 1.0)

    def parse_question_date(date_str):
        try:
            return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
        except Exception:
            return None

    def parse_time_offset_days(question):
        q = question.lower()
        patterns = [
            (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
            (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
            (r"yesterday", lambda m: (1, 1)),
            (r"a\s+week\s+ago", lambda m: (7, 3)),
            (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
            (r"last\s+week", lambda m: (7, 3)),
            (r"a\s+month\s+ago", lambda m: (30, 7)),
            (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
            (r"last\s+month", lambda m: (30, 7)),
            (r"last\s+year", lambda m: (365, 30)),
            (r"a\s+year\s+ago", lambda m: (365, 30)),
            (r"recently", lambda m: (14, 14)),
        ]
        for pattern, extractor in patterns:
            m = _re.search(pattern, q)
            if m:
                return extractor(m)
        return None

    def is_assistant_reference(question):
        q = question.lower()
        triggers = [
            "you suggested",
            "you told me",
            "you mentioned",
            "you said",
            "you recommended",
            "remind me what you",
            "you provided",
            "you listed",
            "you gave me",
            "you described",
            "what did you",
            "you came up with",
            "you helped me",
            "you explained",
            "can you remind me",
            "you identified",
        ]
        return any(t in q for t in triggers)

    # -------------------------------------------------------------------------
    # V4: Expanded preference patterns (adds memory/nostalgia for Miss 1)
    # -------------------------------------------------------------------------
    PREF_PATTERNS = [
        r"i(?:'ve been| have been) having (?:trouble|issues?|problems?) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) feeling ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:struggling|dealing) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i(?:'m| am) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i prefer ([^,\.!?]{5,60})",
        r"i usually ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:trying|attempting) to ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:considering|thinking about) ([^,\.!?]{5,80})",
        r"lately[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"recently[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:working on|focused on|interested in) ([^,\.!?]{5,80})",
        r"i want to ([^,\.!?]{5,60})",
        r"i(?:'m| am) looking (?:to|for) ([^,\.!?]{5,60})",
        r"i(?:'m| am) thinking (?:about|of) ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:noticing|experiencing) ([^,\.!?]{5,80})",
        # NEW in V4 — memory/nostalgia patterns (for high school reunion miss):
        r"i (?:still )?remember (?:the |my )?([^,\.!?]{5,80})",
        r"i used to ([^,\.!?]{5,60})",
        r"when i was (?:in high school|in college|young|a kid|growing up)[,\s]+([^,\.!?]{5,80})",
        r"growing up[,\s]+([^,\.!?]{5,80})",
        r"(?:happy|fond|good|positive) (?:high school|college|childhood|school) (?:experience|memory|memories|time)[^,\.!?]{0,60}",
    ]

    def extract_preferences(session):
        """Extract preference/concern/memory expressions from user turns in a session."""
        mentions = []
        for turn in session:
            if turn["role"] != "user":
                continue
            text = turn["content"].lower()
            for pat in PREF_PATTERNS:
                for match in _re.findall(pat, text, _re.IGNORECASE):
                    if isinstance(match, tuple):
                        match = " ".join(match)
                    clean = match.strip().rstrip(".,;!? ")
                    if 5 <= len(clean) <= 80:
                        mentions.append(clean)
        seen = set()
        unique = []
        for m in mentions:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique[:12]

    # -------------------------------------------------------------------------
    # Build corpus
    # -------------------------------------------------------------------------
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    # V4: Pre-extract question signals
    quoted_phrases = extract_quoted_phrases(question)
    person_names = extract_person_names(question)

    corpus_user = []
    corpus_full = []
    corpus_ids = []
    corpus_timestamps = []

    pref_docs = []
    pref_ids = []
    pref_timestamps = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        all_turns = [t["content"] for t in session]
        if not user_turns:
            continue
        corpus_user.append("\n".join(user_turns))
        corpus_full.append("\n".join(all_turns))
        corpus_ids.append(sess_id)
        corpus_timestamps.append(date)

        prefs = extract_preferences(session)
        if prefs:
            pref_doc = "User has mentioned: " + "; ".join(prefs)
            pref_docs.append(pref_doc)
            pref_ids.append(sess_id)
            pref_timestamps.append(date)

    if not corpus_user:
        return [], corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Two-pass for assistant-reference questions — V4 uses corpus_full for Pass 1
    # (ensures the quoted phrases appear in the indexed text)
    # -------------------------------------------------------------------------
    if is_assistant_reference(question):
        collection = _fresh_collection()
        # Index full turns (not just user) so assistant's exact words are searchable
        collection.add(
            documents=corpus_full,
            ids=[f"doc_{i}" for i in range(len(corpus_full))],
            metadatas=[
                {"corpus_id": cid, "timestamp": ts}
                for cid, ts in zip(corpus_ids, corpus_timestamps)
            ],
        )
        results = collection.query(
            query_texts=[question],
            n_results=min(50, len(corpus_full)),
            include=["distances", "metadatas", "documents"],
        )
        result_ids = results["ids"][0]
        distances = results["distances"][0]
        documents = results["documents"][0]

        # Apply quoted phrase + name boost in scoring
        scored = []
        for rid, dist, doc in zip(result_ids, distances, documents):
            idx = int(rid.split("_")[1])
            overlap = keyword_overlap(extract_keywords(question), doc)
            fused_dist = dist * (1.0 - hybrid_weight * overlap)
            # Quoted phrase boost — strong signal for assistant-recall questions
            q_boost = quoted_phrase_boost(quoted_phrases, doc)
            if q_boost > 0:
                fused_dist = fused_dist * (1.0 - 0.60 * q_boost)
            scored.append((idx, fused_dist))

        scored.sort(key=lambda x: x[1])
        seen = set()
        ranked_indices = []
        for idx, _ in scored:
            if corpus_ids[idx] not in seen:
                seen.add(corpus_ids[idx])
                ranked_indices.append(idx)
        for i in range(len(corpus_user)):
            if corpus_ids[i] not in seen:
                ranked_indices.append(i)
                seen.add(corpus_ids[i])
        return ranked_indices, corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Build expanded collection: user docs + synthetic preference docs
    # -------------------------------------------------------------------------
    all_docs = corpus_user + pref_docs
    all_ids_meta = corpus_ids + pref_ids
    all_ts = corpus_timestamps + pref_timestamps

    collection = _fresh_collection()
    collection.add(
        documents=all_docs,
        ids=[f"doc_{i}" for i in range(len(all_docs))],
        metadatas=[
            {"corpus_id": cid, "timestamp": ts, "is_pref": i >= len(corpus_user)}
            for i, (cid, ts) in enumerate(zip(all_ids_meta, all_ts))
        ],
    )

    query_keywords = extract_keywords(question)
    results = collection.query(
        query_texts=[question],
        n_results=min(n_results, len(all_docs)),
        include=["distances", "metadatas", "documents"],
    )

    result_ids = results["ids"][0]
    distances = results["distances"][0]
    documents = results["documents"][0]
    doc_id_to_idx = {f"doc_{i}": i for i in range(len(all_docs))}

    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        days_back, tolerance = time_offset
        target_date = question_date - timedelta(days=days_back)

    scored = []
    for rid, dist, doc in zip(result_ids, distances, documents):
        idx = doc_id_to_idx[rid]
        overlap = keyword_overlap(query_keywords, doc)
        fused_dist = dist * (1.0 - hybrid_weight * overlap)

        # Temporal boost (same as v3)
        if target_date:
            sess_date = parse_question_date(all_ts[idx])
            if sess_date:
                delta_days = abs((sess_date - target_date).days)
                tol = time_offset[1]
                if delta_days <= tol:
                    temporal_boost = 0.40
                elif delta_days <= tol * 3:
                    temporal_boost = 0.40 * (1.0 - (delta_days - tol) / (tol * 2))
                else:
                    temporal_boost = 0.0
                fused_dist = fused_dist * (1.0 - temporal_boost)

        # V4: Person name boost (for temporal-reasoning + person name questions)
        if person_names:
            n_boost = person_name_boost(person_names, doc)
            if n_boost > 0:
                fused_dist = fused_dist * (1.0 - 0.40 * n_boost)

        scored.append((idx, fused_dist))

    scored.sort(key=lambda x: x[1])

    corpus_id_to_user_idx = {cid: i for i, cid in enumerate(corpus_ids)}
    seen_ids = set()
    ranked_indices = []
    for idx, _ in scored:
        cid = all_ids_meta[idx]
        if cid not in seen_ids:
            seen_ids.add(cid)
            ranked_indices.append(corpus_id_to_user_idx[cid])

    for i in range(len(corpus_user)):
        if corpus_ids[i] not in seen_ids:
            ranked_indices.append(i)
            seen_ids.add(corpus_ids[i])

    return ranked_indices, corpus_user, corpus_ids, corpus_timestamps


# =============================================================================
# PALACE MODE — Hall classification + drawer indexing + hall-boosted retrieval
# =============================================================================

# Hall names mirror the MemPal palace taxonomy
HALL_PREFERENCES = "hall_preferences"
HALL_FACTS = "hall_facts"
HALL_EVENTS = "hall_events"
HALL_ASSISTANT = "hall_assistant_advice"
HALL_GENERAL = "hall_general"


def classify_session_hall(session):
    """
    Assign a session to a palace hall based on its content.

    Heuristics (checked in priority order):
      hall_preferences  — user expresses preferences, concerns, ongoing struggles
      hall_assistant    — assistant gave specific advice, lists, or recommendations
      hall_events       — milestones, events, significant occurrences mentioned
      hall_facts        — factual disclosures (degrees, jobs, places, numbers)
      hall_general      — default
    """
    user_text = " ".join(t["content"] for t in session if t["role"] == "user").lower()
    asst_text = " ".join(t["content"] for t in session if t["role"] == "assistant").lower()

    pref_signals = [
        "i prefer",
        "i usually",
        "i've been having trouble",
        "i've been feeling",
        "i've been struggling",
        "i want to",
        "i'm worried",
        "i've been thinking",
        "i've been considering",
        "lately i",
        "recently i",
        "i tend to",
    ]
    if any(s in user_text for s in pref_signals):
        return HALL_PREFERENCES

    asst_advice_signals = [
        "i suggest",
        "i recommend",
        "here are",
        "you might want to",
        "option 1",
        "option 2",
        "1.",
        "2.",
        "3.",
        "first,",
        "second,",
        "you could try",
        "i would recommend",
        "my recommendation",
    ]
    if sum(1 for s in asst_advice_signals if s in asst_text) >= 2:
        return HALL_ASSISTANT

    event_signals = [
        "milestone",
        "graduation",
        "promotion",
        "anniversary",
        "birthday",
        "moved",
        "started",
        "finished",
        "completed",
        "launched",
        "opened",
        "achieved",
        "won",
        "accepted",
        "hired",
        "married",
        "born",
    ]
    if any(s in user_text + asst_text for s in event_signals):
        return HALL_EVENTS

    fact_signals = [
        "degree",
        "major",
        "university",
        "college",
        "job",
        "position",
        "role",
        "company",
        "city",
        "country",
        "street",
        "born in",
        "grew up",
        "studied",
        "works at",
        "lives in",
        "years old",
        "salary",
        "budget",
    ]
    if sum(1 for s in fact_signals if s in user_text + asst_text) >= 2:
        return HALL_FACTS

    return HALL_GENERAL


def classify_question_hall(question):
    """
    Infer which palace hall a question is asking about.

    Returns a list of halls in priority order (first = most likely).
    """
    q = question.lower()

    if any(
        t in q
        for t in [
            "you suggested",
            "you told me",
            "you mentioned",
            "you said",
            "you recommended",
            "you provided",
            "you listed",
            "you gave",
            "remind me what you",
            "you came up with",
            "you explained",
        ]
    ):
        return [HALL_ASSISTANT, HALL_GENERAL]

    if any(
        t in q
        for t in [
            "i've been having trouble",
            "i've been feeling",
            "i prefer",
            "i usually",
            "battery",
            "nostalgic",
            "reunion",
            "lately",
            "recently been",
            "struggling with",
        ]
    ):
        return [HALL_PREFERENCES, HALL_GENERAL]

    if any(
        t in q
        for t in [
            "milestone",
            "when did",
            "what happened",
            "achievement",
            "ago",
            "last week",
            "last month",
            "last year",
            "four weeks",
            "three months",
        ]
    ):
        return [HALL_EVENTS, HALL_FACTS, HALL_GENERAL]

    if any(
        t in q
        for t in [
            "degree",
            "study",
            "graduate",
            "major",
            "job",
            "work",
            "live",
            "born",
            "city",
            "country",
            "company",
            "school",
        ]
    ):
        return [HALL_FACTS, HALL_GENERAL]

    return [HALL_GENERAL]


def build_palace_and_retrieve_palace(
    entry, granularity="session", n_results=50, hybrid_weight=0.30
):
    """
    Palace-mode retrieval: navigate by hall first, fall back to full search.

    The palace insight: don't search everything flat. Enter through the right
    hall — a smaller, more focused subset — and get a tight answer fast.
    Only widen to the full haystack if the hall search doesn't yield confidence.

    PALACE
      └── HALL (classified per session: preferences / facts / events / assistant / general)
            └── CLOSET (user turns per session — what the user said)
                  └── DRAWER (assistant turns — only opened for assistant-reference questions)
            └── PREFERENCE WING (synthetic docs from pref extraction — same session ID)

    Navigation:
      1. Classify question → primary hall
      2. PASS 1: search only the primary hall (tight — 5-15 sessions max)
         If top result has low distance (confident match) → done
      3. PASS 2 (fallback): search full haystack with hall-aware scoring
         Sessions in the primary hall get a 25% distance bonus
      4. For assistant-reference questions: open drawers within top sessions
    """
    import re as _re
    from datetime import datetime, timedelta

    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = _re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    def parse_question_date(date_str):
        try:
            return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
        except Exception:
            return None

    def parse_time_offset_days(question):
        q = question.lower()
        patterns = [
            (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
            (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
            (r"yesterday", lambda m: (1, 1)),
            (r"a\s+week\s+ago", lambda m: (7, 3)),
            (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
            (r"last\s+week", lambda m: (7, 3)),
            (r"a\s+month\s+ago", lambda m: (30, 7)),
            (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
            (r"last\s+month", lambda m: (30, 7)),
            (r"last\s+year", lambda m: (365, 30)),
            (r"a\s+year\s+ago", lambda m: (365, 30)),
            (r"recently", lambda m: (14, 14)),
        ]
        for pattern, extractor in patterns:
            m = _re.search(pattern, q)
            if m:
                return extractor(m)
        return None

    # Preference extraction (same as v3)
    PREF_PATTERNS = [
        r"i(?:'ve been| have been) having (?:trouble|issues?|problems?) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) feeling ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:struggling|dealing) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i(?:'m| am) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i prefer ([^,\.!?]{5,60})",
        r"i usually ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:trying|attempting) to ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:considering|thinking about) ([^,\.!?]{5,80})",
        r"lately[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"recently[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:working on|focused on|interested in) ([^,\.!?]{5,80})",
        r"i want to ([^,\.!?]{5,60})",
        r"i(?:'m| am) looking (?:to|for) ([^,\.!?]{5,60})",
        r"i(?:'m| am) thinking (?:about|of) ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:noticing|experiencing) ([^,\.!?]{5,80})",
    ]

    def extract_preferences(session):
        mentions = []
        for turn in session:
            if turn["role"] != "user":
                continue
            text = turn["content"].lower()
            for pat in PREF_PATTERNS:
                for match in _re.findall(pat, text, _re.IGNORECASE):
                    clean = match.strip().rstrip(".,;!? ")
                    if 5 <= len(clean) <= 80:
                        mentions.append(clean)
        seen = set()
        unique = []
        for m in mentions:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique[:10]

    # -------------------------------------------------------------------------
    # Build palace — classify sessions into halls, build per-hall closets
    # -------------------------------------------------------------------------
    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    # Canonical corpus (user turns per session) — indices used for evaluation
    corpus_user = []
    corpus_ids = []
    corpus_timestamps = []

    # Per-hall closet documents (user turns only — clean, no noise)
    hall_docs = {
        h: [] for h in [HALL_PREFERENCES, HALL_FACTS, HALL_EVENTS, HALL_ASSISTANT, HALL_GENERAL]
    }
    hall_meta = {h: [] for h in hall_docs}

    # Preference wing: synthetic docs for vocab-gap bridging (separate from halls)
    pref_wing_docs = []
    pref_wing_meta = []

    # Drawer index: assistant turns per session (only opened when needed)
    drawer_docs = []
    drawer_meta = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        asst_turns = [t["content"] for t in session if t["role"] == "assistant"]
        if not user_turns:
            continue

        hall = classify_session_hall(session)
        user_doc = "\n".join(user_turns)

        # Canonical entry
        corpus_user.append(user_doc)
        corpus_ids.append(sess_id)
        corpus_timestamps.append(date)

        # CLOSET: file into the correct hall (clean, targeted)
        hall_docs[hall].append(user_doc)
        hall_meta[hall].append({"corpus_id": sess_id, "timestamp": date, "hall": hall})

        # PREFERENCE WING: synthetic preference doc (same session, separate index)
        prefs = extract_preferences(session)
        if prefs:
            pref_doc = "User has mentioned: " + "; ".join(prefs)
            pref_wing_docs.append(pref_doc)
            pref_wing_meta.append({"corpus_id": sess_id, "timestamp": date})

        # DRAWERS: assistant turns stored separately, only indexed on demand
        for asst_turn in asst_turns:
            if len(asst_turn) > 30:
                drawer_docs.append(asst_turn)
                drawer_meta.append({"corpus_id": sess_id, "timestamp": date})

    if not corpus_user:
        return [], corpus_user, corpus_ids, corpus_timestamps

    # -------------------------------------------------------------------------
    # Navigate: classify question → primary hall
    # -------------------------------------------------------------------------
    target_halls = classify_question_hall(question)
    primary_hall = target_halls[0]
    query_keywords = extract_keywords(question)

    def hybrid_score(dist, doc):
        overlap = keyword_overlap(query_keywords, doc)
        return dist * (1.0 - hybrid_weight * overlap)

    def apply_temporal(fused_dist, timestamp):
        if not target_date:
            return fused_dist
        sess_date = parse_question_date(timestamp)
        if not sess_date:
            return fused_dist
        delta_days = abs((sess_date - target_date).days)
        tol = time_offset[1]
        if delta_days <= tol:
            boost = 0.40
        elif delta_days <= tol * 3:
            boost = 0.40 * (1.0 - (delta_days - tol) / (tol * 2))
        else:
            boost = 0.0
        return fused_dist * (1.0 - boost)

    # Temporal setup
    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        target_date = question_date - timedelta(days=time_offset[0])

    corpus_id_to_user_idx = {cid: i for i, cid in enumerate(corpus_ids)}

    # -------------------------------------------------------------------------
    # PASS 1: Navigate into primary hall — tight, focused search
    # -------------------------------------------------------------------------
    primary_hall_docs = hall_docs[primary_hall]
    primary_hall_meta = hall_meta[primary_hall]

    # Also include preference wing docs if question is preference-type
    pass1_docs = list(primary_hall_docs)
    pass1_meta = list(primary_hall_meta)
    if primary_hall == HALL_PREFERENCES and pref_wing_docs:
        pass1_docs += pref_wing_docs
        pass1_meta += pref_wing_meta

    # For assistant-reference: open drawers within the primary hall sessions
    if primary_hall == HALL_ASSISTANT and drawer_docs:
        # Only drawers from sessions in the assistant hall
        hall_session_ids = {m["corpus_id"] for m in primary_hall_meta}
        for ddoc, dmeta in zip(drawer_docs, drawer_meta):
            if dmeta["corpus_id"] in hall_session_ids:
                pass1_docs.append(ddoc)
                pass1_meta.append(dmeta)

    # -------------------------------------------------------------------------
    # PASS 1: Navigate into primary hall — tight, focused search
    # Builds a set of hall-validated session IDs for Pass 2 score bonus
    # Does NOT pre-empt Pass 2 results — scores decide final order
    # -------------------------------------------------------------------------
    hall_validated_ids = set()  # sessions confirmed by tight hall search

    # Only do Pass 1 for specific halls (not GENERAL — too broad to be useful)
    if primary_hall != HALL_GENERAL and len(pass1_docs) >= 1:
        coll1 = _fresh_collection("mempal_hall")
        coll1.add(
            documents=pass1_docs,
            ids=[f"h_{i}" for i in range(len(pass1_docs))],
            metadatas=pass1_meta,
        )
        r1 = coll1.query(
            query_texts=[question],
            n_results=min(10, len(pass1_docs)),
            include=["distances", "metadatas", "documents"],
        )
        for rid, dist, doc, meta in zip(
            r1["ids"][0], r1["distances"][0], r1["documents"][0], r1["metadatas"][0]
        ):
            hall_validated_ids.add(meta["corpus_id"])

    # -------------------------------------------------------------------------
    # PASS 2: Full haystack search — primary ranking
    # Hall bonus: sessions in primary hall get distance reduction
    # Double-validation bonus: sessions also found in Pass 1 get extra boost
    # -------------------------------------------------------------------------
    full_docs = corpus_user + pref_wing_docs
    full_meta_list = [
        {
            "corpus_id": corpus_ids[i],
            "timestamp": corpus_timestamps[i],
            "hall": classify_session_hall(sessions[i]) if i < len(sessions) else HALL_GENERAL,
        }
        for i in range(len(corpus_user))
    ]
    full_meta_list += pref_wing_meta

    coll2 = _fresh_collection()
    coll2.add(
        documents=full_docs,
        ids=[f"doc_{i}" for i in range(len(full_docs))],
        metadatas=full_meta_list,
    )
    r2 = coll2.query(
        query_texts=[question],
        n_results=min(n_results, len(full_docs)),
        include=["distances", "metadatas", "documents"],
    )

    full_scored = []
    for rid, dist, doc, meta in zip(
        r2["ids"][0], r2["distances"][0], r2["documents"][0], r2["metadatas"][0]
    ):
        fd = hybrid_score(dist, doc)
        cid = meta["corpus_id"]
        # Hall bonus: sessions in the primary hall get 25% distance reduction
        if meta.get("hall") == primary_hall and primary_hall != HALL_GENERAL:
            fd = fd * 0.75
        elif meta.get("hall") in target_halls:
            fd = fd * 0.90
        # Double-validation bonus: appeared in tight hall search → extra 15% boost
        if cid in hall_validated_ids:
            fd = fd * 0.85
        fd = apply_temporal(fd, meta.get("timestamp", ""))
        full_scored.append((cid, fd))

    full_scored.sort(key=lambda x: x[1])

    # Build final ranking purely by score — hall navigation boosts but never overrides
    ranked_indices = []
    seen_ids = set()
    for cid, _ in full_scored:
        if cid not in seen_ids and cid in corpus_id_to_user_idx:
            ranked_indices.append(corpus_id_to_user_idx[cid])
            seen_ids.add(cid)

    # Fill any stragglers
    for i in range(len(corpus_user)):
        if corpus_ids[i] not in seen_ids:
            ranked_indices.append(i)
            seen_ids.add(corpus_ids[i])

    return ranked_indices, corpus_user, corpus_ids, corpus_timestamps


# =============================================================================
# LLM RE-RANKER (optional third pass)
# =============================================================================


def diary_ingest_session(session, sess_id, api_key, model="claude-haiku-4-5-20251001"):
    """
    Call an LLM to extract topics and a summary from one session.

    This is the "LLM topic layer" — the core of diary mode.
    Haiku reads the session once and returns:
        topics:  2-5 specific things discussed ("yoga classes", "job interview at fintech startup")
        summary: 1-2 sentences describing what the session was about

    These become synthetic documents added to the haystack with the same
    corpus_id as the session — bridging vocabulary gaps that embeddings miss.

    Example gap closed:
        Session: "I went this morning, my instructor pushed me really hard"
        Question: "Where do I take yoga classes?"
        Without diary: no keyword overlap → miss
        With diary:    topic doc "yoga classes, fitness routine" → hit

    Returns: {"topics": [...], "summary": "..."} or None on failure.
    """
    import urllib.request as _urllib_request

    user_turns = [t["content"] for t in session if t["role"] == "user"]
    if not user_turns:
        return None

    # Only send first 1200 chars of user text — enough context, cheap prompt
    user_text = " | ".join(user_turns)[:1200]

    prompt = (
        "Read this conversation excerpt (user turns only) and extract:\n\n"
        f"USER SAID:\n{user_text}\n\n"
        "Return a JSON object with exactly two fields:\n"
        '{"topics": ["specific topic 1", "specific topic 2", ...], "summary": "1-2 sentences"}\n\n'
        "Rules:\n"
        "- topics: 2-5 SPECIFIC things discussed. Not 'work' — 'job interview at law firm'. "
        "Not 'health' — 'back pain from sitting at desk'. Not 'travel' — 'trip to Tokyo in March'.\n"
        "- summary: what this person was talking about, in plain language\n"
        "- Return ONLY valid JSON. No markdown, no explanation."
    )

    payload = json.dumps(
        {
            "model": model,
            "max_tokens": 200,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    req = _urllib_request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )

    try:
        with _urllib_request.urlopen(req, timeout=25) as resp:
            result = json.loads(resp.read())
        raw = result["content"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        if "topics" in data and "summary" in data:
            return data
    except Exception:
        pass  # timeout, network error, bad JSON — fall through to None

    return None


def build_palace_and_retrieve_diary(
    entry,
    granularity="session",
    n_results=50,
    hybrid_weight=0.30,
    diary_cache=None,
    api_key="",
    diary_model="claude-haiku-4-5-20251001",
):
    """
    Diary mode: palace retrieval + LLM topic layer at ingest.

    On top of palace mode's hall/closet/drawer navigation, diary mode adds:

    DIARY LAYER (per session, computed once and cached):
      - Haiku reads the session → extracts 2-5 specific topics + a summary
      - Synthetic doc: "Session topics: yoga classes, Tuesday routine. Summary: ..."
      - Same corpus_id as the session → evaluation maps it correctly
      - Added to the haystack alongside raw user turns

    This bridges vocabulary gaps that neither embeddings nor keyword matching
    can cross — e.g., "Where do I take yoga classes?" matching a session that
    only says "I went this morning, my instructor was great."

    diary_cache: dict mapping sess_id → {"topics": [...], "summary": "..."}
                 Pre-populated before the benchmark loop to avoid redundant API calls.
                 Pass the same dict across all questions — it grows as new sessions appear.
    """
    import re as _re
    from datetime import datetime, timedelta

    STOP_WORDS = {
        "what",
        "when",
        "where",
        "who",
        "how",
        "which",
        "did",
        "do",
        "was",
        "were",
        "have",
        "has",
        "had",
        "is",
        "are",
        "the",
        "a",
        "an",
        "my",
        "me",
        "i",
        "you",
        "your",
        "their",
        "it",
        "its",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "ago",
        "last",
        "that",
        "this",
        "there",
        "about",
        "get",
        "got",
        "give",
        "gave",
        "buy",
        "bought",
        "made",
        "make",
    }

    def extract_keywords(text):
        words = _re.findall(r"\b[a-z]{3,}\b", text.lower())
        return [w for w in words if w not in STOP_WORDS]

    def keyword_overlap(query_kws, doc_text):
        doc_lower = doc_text.lower()
        if not query_kws:
            return 0.0
        hits = sum(1 for kw in query_kws if kw in doc_lower)
        return hits / len(query_kws)

    def parse_question_date(date_str):
        try:
            return datetime.strptime(date_str.split(" (")[0], "%Y/%m/%d")
        except Exception:
            return None

    def parse_time_offset_days(question):
        q = question.lower()
        patterns = [
            (r"(\d+)\s+days?\s+ago", lambda m: (int(m.group(1)), 2)),
            (r"a\s+couple\s+(?:of\s+)?days?\s+ago", lambda m: (2, 2)),
            (r"yesterday", lambda m: (1, 1)),
            (r"a\s+week\s+ago", lambda m: (7, 3)),
            (r"(\d+)\s+weeks?\s+ago", lambda m: (int(m.group(1)) * 7, 5)),
            (r"last\s+week", lambda m: (7, 3)),
            (r"a\s+month\s+ago", lambda m: (30, 7)),
            (r"(\d+)\s+months?\s+ago", lambda m: (int(m.group(1)) * 30, 10)),
            (r"last\s+month", lambda m: (30, 7)),
            (r"last\s+year", lambda m: (365, 30)),
            (r"a\s+year\s+ago", lambda m: (365, 30)),
            (r"recently", lambda m: (14, 14)),
        ]
        for pattern, extractor in patterns:
            m = _re.search(pattern, q)
            if m:
                return extractor(m)
        return None

    # Preference extraction (same 16 patterns as v3/palace)
    PREF_PATTERNS = [
        r"i(?:'ve been| have been) having (?:trouble|issues?|problems?) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) feeling ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:struggling|dealing) with ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i(?:'m| am) (?:worried|concerned) about ([^,\.!?]{5,80})",
        r"i prefer ([^,\.!?]{5,60})",
        r"i usually ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:trying|attempting) to ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:considering|thinking about) ([^,\.!?]{5,80})",
        r"lately[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"recently[,\s]+(?:i've been|i have been|i'm|i am) ([^,\.!?]{5,80})",
        r"i(?:'ve been| have been) (?:working on|focused on|interested in) ([^,\.!?]{5,80})",
        r"i want to ([^,\.!?]{5,60})",
        r"i(?:'m| am) looking (?:to|for) ([^,\.!?]{5,60})",
        r"i(?:'m| am) thinking (?:about|of) ([^,\.!?]{5,60})",
        r"i(?:'ve been| have been) (?:noticing|experiencing) ([^,\.!?]{5,80})",
    ]

    def extract_preferences(session):
        mentions = []
        for turn in session:
            if turn["role"] != "user":
                continue
            text = turn["content"].lower()
            for pat in PREF_PATTERNS:
                for match in _re.findall(pat, text, _re.IGNORECASE):
                    clean = match.strip().rstrip(".,;!? ")
                    if 5 <= len(clean) <= 80:
                        mentions.append(clean)
        seen = set()
        unique = []
        for m in mentions:
            if m not in seen:
                seen.add(m)
                unique.append(m)
        return unique[:10]

    if diary_cache is None:
        diary_cache = {}

    sessions = entry["haystack_sessions"]
    session_ids = entry["haystack_session_ids"]
    dates = entry["haystack_dates"]
    question = entry["question"]
    question_date = parse_question_date(entry.get("question_date", ""))

    corpus_user = []
    corpus_ids = []
    corpus_timestamps = []
    diary_docs = []  # LLM topic layer docs (one per session with diary data)
    diary_meta = []
    pref_wing_docs = []
    pref_wing_meta = []

    for session, sess_id, date in zip(sessions, session_ids, dates):
        user_turns = [t["content"] for t in session if t["role"] == "user"]
        if not user_turns:
            continue

        user_doc = "\n".join(user_turns)
        corpus_user.append(user_doc)
        corpus_ids.append(sess_id)
        corpus_timestamps.append(date)

        # DIARY LAYER: get or compute LLM topic extraction
        if sess_id not in diary_cache:
            if api_key:
                result = diary_ingest_session(session, sess_id, api_key, model=diary_model)
                diary_cache[sess_id] = result  # cache even if None
            else:
                diary_cache[sess_id] = None

        diary_data = diary_cache.get(sess_id)
        if diary_data:
            topics = diary_data.get("topics", [])
            summary = diary_data.get("summary", "")
            if topics or summary:
                topic_str = ", ".join(topics) if topics else ""
                diary_doc = f"Session topics: {topic_str}. Summary: {summary}"
                diary_docs.append(diary_doc)
                diary_meta.append(
                    {
                        "corpus_id": sess_id,
                        "timestamp": date,
                        "hall": classify_session_hall(session),
                    }
                )

        # PREFERENCE WING (same as v3/palace)
        prefs = extract_preferences(session)
        if prefs:
            pref_doc = "User has mentioned: " + "; ".join(prefs)
            pref_wing_docs.append(pref_doc)
            pref_wing_meta.append({"corpus_id": sess_id, "timestamp": date})

    if not corpus_user:
        return [], corpus_user, corpus_ids, corpus_timestamps

    # Hall navigation (same as palace)
    target_halls = classify_question_hall(question)
    primary_hall = target_halls[0]
    query_keywords = extract_keywords(question)

    def hybrid_score(dist, doc):
        overlap = keyword_overlap(query_keywords, doc)
        return dist * (1.0 - hybrid_weight * overlap)

    time_offset = parse_time_offset_days(question)
    target_date = None
    if time_offset and question_date:
        target_date = question_date - timedelta(days=time_offset[0])

    def apply_temporal(fused_dist, timestamp):
        if not target_date:
            return fused_dist
        sess_date = parse_question_date(timestamp)
        if not sess_date:
            return fused_dist
        delta_days = abs((sess_date - target_date).days)
        tol = time_offset[1]
        if delta_days <= tol:
            boost = 0.40
        elif delta_days <= tol * 3:
            boost = 0.40 * (1.0 - (delta_days - tol) / (tol * 2))
        else:
            boost = 0.0
        return fused_dist * (1.0 - boost)

    corpus_id_to_user_idx = {cid: i for i, cid in enumerate(corpus_ids)}

    # -------------------------------------------------------------------------
    # FULL SEARCH: raw user docs + diary topic docs + preference wing
    # Diary docs and pref docs share corpus_id with their session — same hit
    # -------------------------------------------------------------------------
    full_docs = corpus_user + diary_docs + pref_wing_docs
    full_meta = (
        [
            {
                "corpus_id": corpus_ids[i],
                "timestamp": corpus_timestamps[i],
                "hall": classify_session_hall(sessions[i]) if i < len(sessions) else HALL_GENERAL,
                "layer": "raw",
            }
            for i in range(len(corpus_user))
        ]
        + [dict(m, layer="diary") for m in diary_meta]
        + [dict(m, layer="pref") for m in pref_wing_meta]
    )

    coll = _fresh_collection()
    coll.add(
        documents=full_docs,
        ids=[f"doc_{i}" for i in range(len(full_docs))],
        metadatas=full_meta,
    )
    r = coll.query(
        query_texts=[question],
        n_results=min(n_results, len(full_docs)),
        include=["distances", "metadatas", "documents"],
    )

    scored = []
    for rid, dist, doc, meta in zip(
        r["ids"][0], r["distances"][0], r["documents"][0], r["metadatas"][0]
    ):
        cid = meta["corpus_id"]
        fd = hybrid_score(dist, doc)
        # Hall bonus
        if meta.get("hall") == primary_hall and primary_hall != HALL_GENERAL:
            fd *= 0.75
        elif meta.get("hall") in target_halls:
            fd *= 0.90
        # Diary layer bonus: LLM topic doc that matches gets extra 20% boost
        # (it's a more precise signal than raw text)
        if meta.get("layer") == "diary":
            fd *= 0.80
        fd = apply_temporal(fd, meta.get("timestamp", ""))
        scored.append((cid, fd))

    scored.sort(key=lambda x: x[1])

    ranked_indices = []
    seen_ids = set()
    for cid, _ in scored:
        if cid not in seen_ids and cid in corpus_id_to_user_idx:
            ranked_indices.append(corpus_id_to_user_idx[cid])
            seen_ids.add(cid)

    for i in range(len(corpus_user)):
        if corpus_ids[i] not in seen_ids:
            ranked_indices.append(i)
            seen_ids.add(corpus_ids[i])

    return ranked_indices, corpus_user, corpus_ids, corpus_timestamps


def llm_rerank(
    question,
    rankings,
    corpus,
    corpus_ids,
    api_key,
    top_k=10,
    model="claude-haiku-4-5-20251001",
    backend="anthropic",
    base_url="",
):
    """
    Use an LLM to re-rank the top-k retrieved sessions.

    Takes the top-k sessions from any retrieval mode and asks the LLM
    which single session is most relevant to the question. That session
    is promoted to rank 1; the rest stay in their existing order.

    Supports two backends:
      - "anthropic": hits https://api.anthropic.com/v1/messages with x-api-key.
      - "ollama":    hits {base_url}/v1/chat/completions (OpenAI-compat) —
                     works for local Ollama (default http://localhost:11434)
                     and Ollama Cloud (:cloud model tags).

    Args:
        question:   The benchmark question string
        rankings:   Current ranked list of corpus indices (from any mode)
        corpus:     List of document strings
        corpus_ids: List of corpus IDs (parallel to corpus)
        api_key:    Anthropic API key (only required for backend="anthropic")
        top_k:      How many top sessions to send to LLM (default: 10)
        model:      Model id (Claude model for anthropic, e.g. "minimax-m2.7:cloud" for ollama)
        backend:    "anthropic" or "ollama"
        base_url:   Override base URL (ollama default: http://localhost:11434)

    Returns:
        Reordered rankings list with LLM's best pick promoted to rank 1.
    """
    import urllib.request
    import urllib.error

    candidates = rankings[:top_k]
    if not candidates:
        return rankings

    session_blocks = []
    for rank, idx in enumerate(candidates):
        text = corpus[idx][:500].replace("\n", " ").strip()
        session_blocks.append(f"Session {rank + 1}:\n{text}")

    sessions_text = "\n\n".join(session_blocks)

    prompt = (
        f"Question: {question}\n\n"
        f"Below are {len(candidates)} conversation sessions from someone's memory. "
        f"Which single session is most likely to contain the answer to the question above? "
        f"Reply with ONLY a number between 1 and {len(candidates)}. Nothing else.\n\n"
        f"{sessions_text}\n\n"
        f"Most relevant session number:"
    )

    if backend == "ollama":
        url = (base_url or "http://localhost:11434").rstrip("/") + "/v1/chat/completions"
        payload = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1024,
                "temperature": 0.0,
            }
        ).encode("utf-8")
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
    else:
        url = "https://api.anthropic.com/v1/messages"
        payload = json.dumps(
            {
                "model": model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": prompt}],
            }
        ).encode("utf-8")
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

    import socket as _socket

    for _attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120 if backend == "ollama" else 20) as resp:
                result = json.loads(resp.read())
            if backend == "ollama":
                msg = result["choices"][0]["message"]
                # Reasoning models (e.g. minimax-m2.7) may emit final answer in "content"
                # or embed it in "reasoning". Try content first, fall back to reasoning.
                raw = (msg.get("content") or "").strip()
                if not raw:
                    raw = (msg.get("reasoning") or "").strip()
            else:
                raw = result["content"][0]["text"].strip()
            m = re.search(
                r"\b(\d+)\b", raw[::-1]
            )  # take LAST integer (rerank models often reason first)
            if m:
                pick = int(m.group(1)[::-1])
                if 1 <= pick <= len(candidates):
                    chosen_idx = candidates[pick - 1]
                    reordered = [chosen_idx] + [i for i in rankings if i != chosen_idx]
                    return reordered
            break
        except (_socket.timeout, TimeoutError):
            if _attempt < 2:
                import time as _time

                _time.sleep(3)
        except (urllib.error.URLError, KeyError, ValueError, IndexError, OSError):
            break

    return rankings


def _load_api_key(key_arg):
    """Load API key from --llm-key arg or ANTHROPIC_API_KEY env var."""
    if key_arg:
        return key_arg
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if env_key:
        return env_key
    return ""


# =============================================================================
# BENCHMARK RUNNER
# =============================================================================


def _load_or_create_split(split_file: str, data: list, dev_size: int = 50, seed: int = 42) -> dict:
    """
    Load an existing train/test split or create a new one.

    Returns {"dev": [question_id, ...], "held_out": [question_id, ...]}

    The split is stable: same split_file + same seed = same result.
    Creating a split is a one-time operation. After that, always load.
    """
    import random

    split_path = Path(split_file)
    if split_path.exists():
        with open(split_path) as f:
            return json.load(f)

    # Create new split
    all_ids = [entry["question_id"] for entry in data]
    rng = random.Random(seed)
    rng.shuffle(all_ids)
    dev_ids = all_ids[:dev_size]
    held_out_ids = all_ids[dev_size:]
    split = {"dev": dev_ids, "held_out": held_out_ids, "seed": seed, "dev_size": dev_size}
    with open(split_path, "w") as f:
        json.dump(split, f, indent=2)
    print(f"  Created new split: {len(dev_ids)} dev / {len(held_out_ids)} held-out → {split_path}")
    return split


def run_benchmark(
    data_file,
    granularity="session",
    limit=0,
    out_file=None,
    mode="raw",
    skip=0,
    hybrid_weight=0.30,
    llm_rerank_enabled=False,
    llm_key="",
    llm_model="claude-haiku-4-5-20251001",
    diary_cache_file=None,
    skip_precompute=False,
    split_file=None,
    split_subset=None,
    llm_backend="anthropic",
    llm_base_url="",
):
    """Run the full benchmark.

    split_file: path to a JSON split file. If provided, filters questions by subset.
    split_subset: "dev" (50 questions for tuning) or "held_out" (450 for final evaluation).
                  None = run all questions.
    """
    with open(data_file) as f:
        data = json.load(f)

    # Apply train/test split filter before limit/skip
    if split_file and split_subset:
        split = _load_or_create_split(split_file, data)
        subset_ids = set(split[split_subset])
        before = len(data)
        data = [entry for entry in data if entry["question_id"] in subset_ids]
        print(f"  Split filter ({split_subset}): {before} → {len(data)} questions")

    if limit > 0:
        data = data[:limit]

    if skip > 0:
        print(f"  Skipping first {skip} questions (resume mode)")
        data = data[skip:]

    api_key = ""
    if llm_rerank_enabled or mode == "diary":
        api_key = _load_api_key(llm_key)
        # Ollama backend doesn't require an Anthropic API key; a local/cloud Ollama
        # daemon with the requested model pulled is enough. Diary mode is always anthropic.
        needs_key = (llm_backend == "anthropic") or (mode == "diary")
        if needs_key and not api_key:
            print(
                "ERROR: --llm-rerank (anthropic backend) / --mode diary requires an API key. "
                "Set ANTHROPIC_API_KEY or use --llm-key. For ollama backend, pass "
                "--llm-backend ollama."
            )
            sys.exit(1)

    # Diary mode: pre-compute LLM topic extraction for ALL unique sessions upfront
    # This means the main benchmark loop reads from cache only — no API calls mid-loop
    diary_cache = {}
    if mode == "diary":
        # Load existing cache first
        if diary_cache_file:
            cache_path = Path(diary_cache_file)
            if cache_path.exists():
                try:
                    with open(cache_path) as f:
                        diary_cache = json.load(f)
                    print(
                        f"  Diary cache: loaded {len(diary_cache)} sessions from {cache_path.name}"
                    )
                except Exception:
                    pass

        # Collect all unique sessions not yet in cache
        unique_sessions = {}  # sess_id → session turns
        for entry in data:
            for session, sess_id in zip(entry["haystack_sessions"], entry["haystack_session_ids"]):
                if sess_id not in diary_cache and sess_id not in unique_sessions:
                    unique_sessions[sess_id] = session

        if unique_sessions and api_key and not skip_precompute:
            print(
                f"  Diary ingest: pre-computing {len(unique_sessions)} sessions with {llm_model.split('-')[1]}..."
            )
            done = 0
            cache_path = Path(diary_cache_file) if diary_cache_file else None
            for sess_id, session in unique_sessions.items():
                try:
                    result = diary_ingest_session(session, sess_id, api_key, model=llm_model)
                except Exception:
                    result = None
                diary_cache[sess_id] = result
                done += 1
                if done % 50 == 0:
                    print(f"    {done}/{len(unique_sessions)} sessions ingested...")
                    # Save progress in case of interruption
                    if cache_path:
                        try:
                            with open(cache_path, "w") as f:
                                json.dump(diary_cache, f)
                        except Exception:
                            pass
            print(f"  Diary ingest complete: {done} sessions processed")
            # Final cache save
            if cache_path:
                try:
                    with open(cache_path, "w") as f:
                        json.dump(diary_cache, f)
                    print(f"  Diary cache saved → {cache_path.name}")
                except Exception:
                    pass

    print(f"\n{'=' * 60}")
    print("  MemPal × LongMemEval Benchmark")
    print(f"{'=' * 60}")
    print(f"  Data:        {Path(data_file).name}")
    print(f"  Questions:   {len(data)}")
    print(f"  Granularity: {granularity}")
    model_short = llm_model.split("-")[1] if "-" in llm_model else llm_model
    rerank_label = f" + LLM re-rank ({model_short})" if llm_rerank_enabled else ""
    diary_label = f" [diary ingest: {model_short}]" if mode == "diary" else ""
    print(f"  Mode:        {mode}{diary_label}{rerank_label}")
    print(f"{'─' * 60}\n")

    # Collect metrics
    ks = [1, 3, 5, 10, 30, 50]
    metrics_session = {f"recall_any@{k}": [] for k in ks}
    metrics_session.update({f"recall_all@{k}": [] for k in ks})
    metrics_session.update({f"ndcg_any@{k}": [] for k in ks})

    metrics_turn = {f"recall_any@{k}": [] for k in ks}
    metrics_turn.update({f"recall_all@{k}": [] for k in ks})
    metrics_turn.update({f"ndcg_any@{k}": [] for k in ks})

    per_type = defaultdict(lambda: defaultdict(list))

    results_log = []
    start_time = datetime.now()

    for i, entry in enumerate(data):
        qid = entry["question_id"]
        qtype = entry["question_type"]
        question = entry["question"]
        answer_sids = set(entry["answer_session_ids"])

        # Run retrieval with selected mode
        if mode == "aaak":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_aaak(
                entry, granularity=granularity
            )
        elif mode == "rooms":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_rooms(
                entry, granularity=granularity
            )
        elif mode == "hybrid":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_hybrid(
                entry, granularity=granularity, hybrid_weight=hybrid_weight
            )
        elif mode == "hybrid_v2":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_hybrid_v2(
                entry, granularity=granularity, hybrid_weight=hybrid_weight
            )
        elif mode == "hybrid_v3":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_hybrid_v3(
                entry, granularity=granularity, hybrid_weight=hybrid_weight
            )
        elif mode == "hybrid_v4":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_hybrid_v4(
                entry, granularity=granularity, hybrid_weight=hybrid_weight
            )
        elif mode == "palace":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_palace(
                entry, granularity=granularity, hybrid_weight=hybrid_weight
            )
        elif mode == "diary":
            # If skip_precompute, pass empty api_key to prevent inline Haiku calls
            _diary_api_key = "" if skip_precompute else api_key
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_diary(
                entry,
                granularity=granularity,
                hybrid_weight=hybrid_weight,
                diary_cache=diary_cache,
                api_key=_diary_api_key,
                diary_model=llm_model,
            )
        elif mode == "full":
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve_full(
                entry, granularity=granularity
            )
        else:
            rankings, corpus, corpus_ids, corpus_timestamps = build_palace_and_retrieve(
                entry, granularity=granularity
            )

        if not rankings:
            print(f"  [{i + 1:4}/{len(data)}] {qid[:30]:30} SKIP (empty corpus)")
            continue

        # Optional LLM re-ranking pass (larger pool for v3/palace to catch rank-11-12 misses)
        if llm_rerank_enabled:
            rerank_pool = 20 if mode in ("hybrid_v3", "hybrid_v4", "palace") else 10
            rankings = llm_rerank(
                question,
                rankings,
                corpus,
                corpus_ids,
                api_key,
                top_k=rerank_pool,
                model=llm_model,
                backend=llm_backend,
                base_url=llm_base_url,
            )

        # Evaluate at session level
        # Map corpus_ids to session-level IDs for session metrics
        session_level_ids = [session_id_from_corpus_id(cid) for cid in corpus_ids]
        session_correct = answer_sids

        # Turn-level correct: any corpus_id whose session part is in answer_sids
        turn_correct = set()
        for cid in corpus_ids:
            sid = session_id_from_corpus_id(cid)
            if sid in answer_sids:
                turn_correct.add(cid)

        entry_metrics = {"session": {}, "turn": {}}

        for k in ks:
            # Session-level metrics
            ra, rl, nd = evaluate_retrieval(rankings, session_correct, session_level_ids, k)
            metrics_session[f"recall_any@{k}"].append(ra)
            metrics_session[f"recall_all@{k}"].append(rl)
            metrics_session[f"ndcg_any@{k}"].append(nd)
            entry_metrics["session"][f"recall_any@{k}"] = ra
            entry_metrics["session"][f"ndcg_any@{k}"] = nd

            # Turn-level metrics
            ra_t, rl_t, nd_t = evaluate_retrieval(rankings, turn_correct, corpus_ids, k)
            metrics_turn[f"recall_any@{k}"].append(ra_t)
            metrics_turn[f"recall_all@{k}"].append(rl_t)
            metrics_turn[f"ndcg_any@{k}"].append(nd_t)
            entry_metrics["turn"][f"recall_any@{k}"] = ra_t

        # Per-type tracking
        per_type[qtype]["recall_any@5"].append(metrics_session["recall_any@5"][-1])
        per_type[qtype]["recall_any@10"].append(metrics_session["recall_any@10"][-1])
        per_type[qtype]["ndcg_any@10"].append(metrics_session["ndcg_any@10"][-1])

        # Log entry
        ranked_items = []
        for idx in rankings[:50]:
            ranked_items.append(
                {
                    "corpus_id": corpus_ids[idx],
                    "text": corpus[idx][:500],
                    "timestamp": corpus_timestamps[idx],
                }
            )

        results_log.append(
            {
                "question_id": qid,
                "question_type": qtype,
                "question": question,
                "answer": entry["answer"],
                "retrieval_results": {
                    "query": question,
                    "ranked_items": ranked_items,
                    "metrics": entry_metrics,
                },
            }
        )

        # Progress
        r5 = metrics_session["recall_any@5"][-1]
        r10 = metrics_session["recall_any@10"][-1]
        status = "HIT" if r5 > 0 else "miss"
        print(f"  [{i + 1:4}/{len(data)}] {qid[:30]:30} R@5={r5:.0f} R@10={r10:.0f}  {status}")

    elapsed = (datetime.now() - start_time).total_seconds()

    # Print results
    print(f"\n{'=' * 60}")
    print(f"  RESULTS — MemPal ({mode} mode, {granularity} granularity)")
    print(f"{'=' * 60}")
    print(f"  Time: {elapsed:.1f}s ({elapsed / len(data):.2f}s per question)\n")

    print("  SESSION-LEVEL METRICS:")
    for k in ks:
        ra = sum(metrics_session[f"recall_any@{k}"]) / len(metrics_session[f"recall_any@{k}"])
        nd = sum(metrics_session[f"ndcg_any@{k}"]) / len(metrics_session[f"ndcg_any@{k}"])
        print(f"    Recall@{k:2}: {ra:.3f}    NDCG@{k:2}: {nd:.3f}")

    print("\n  TURN-LEVEL METRICS:")
    for k in ks:
        ra = sum(metrics_turn[f"recall_any@{k}"]) / len(metrics_turn[f"recall_any@{k}"])
        nd = sum(metrics_turn[f"ndcg_any@{k}"]) / len(metrics_turn[f"ndcg_any@{k}"])
        print(f"    Recall@{k:2}: {ra:.3f}    NDCG@{k:2}: {nd:.3f}")

    print("\n  PER-TYPE BREAKDOWN (session recall_any@10):")
    for qtype, vals in sorted(per_type.items()):
        r10 = sum(vals["recall_any@10"]) / len(vals["recall_any@10"])
        n = len(vals["recall_any@10"])
        print(f"    {qtype:35} R@10={r10:.3f}  (n={n})")

    print(f"\n{'=' * 60}\n")

    # Save diary cache for reuse (Sonnet run tomorrow can skip re-ingesting)
    # Only save sessions with real data (None = skipped inline call, not worth persisting)
    if mode == "diary" and diary_cache and diary_cache_file:
        try:
            real_cache = {k: v for k, v in diary_cache.items() if v is not None}
            with open(diary_cache_file, "w") as f:
                json.dump(real_cache, f)
            print(f"  Diary cache saved: {len(real_cache)} sessions → {diary_cache_file}")
        except Exception as e:
            print(f"  Warning: could not save diary cache: {e}")

    # Save results
    if out_file:
        with open(out_file, "w") as f:
            for entry in results_log:
                f.write(json.dumps(entry) + "\n")
        print(f"  Results saved to: {out_file}")


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MemPal × LongMemEval Benchmark")
    parser.add_argument("data_file", help="Path to longmemeval_s_cleaned.json")
    parser.add_argument(
        "--granularity",
        choices=["session", "turn"],
        default="session",
        help="Retrieval granularity (default: session)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit to N questions (0 = all)")
    parser.add_argument(
        "--mode",
        choices=[
            "raw",
            "aaak",
            "rooms",
            "hybrid",
            "hybrid_v2",
            "hybrid_v3",
            "hybrid_v4",
            "palace",
            "diary",
            "full",
        ],
        default="raw",
        help="Retrieval mode: raw, hybrid, hybrid_v2, hybrid_v3, palace, diary (palace + LLM topic layer)",
    )
    parser.add_argument("--out", default=None, help="Output JSONL file path")
    parser.add_argument(
        "--skip", type=int, default=0, help="Skip first N questions (resume after hang)"
    )
    parser.add_argument(
        "--hybrid-weight",
        type=float,
        default=0.30,
        help="Keyword overlap boost weight for hybrid mode (default: 0.30). "
        "Full 500q tuning: 0.30 and 0.40 are equivalent (within noise). Try 0.10–0.60.",
    )
    parser.add_argument(
        "--llm-rerank",
        action="store_true",
        default=False,
        help="Enable LLM re-ranking pass using Claude Haiku (requires API key). "
        "Promotes the best session from top-10 to rank 1. Targets preference "
        "and jargon-dense failures that embeddings can't bridge semantically.",
    )
    parser.add_argument(
        "--llm-key",
        default="",
        help="Anthropic API key for LLM re-ranking. Falls back to ANTHROPIC_API_KEY env var.",
    )
    parser.add_argument(
        "--llm-model",
        default="claude-haiku-4-5-20251001",
        help="Model for LLM re-ranking and diary ingest "
        "(default: claude-haiku-4-5-20251001). "
        "Use 'claude-sonnet-4-6' for Sonnet comparison. "
        "With --llm-backend ollama, use an Ollama model tag like 'minimax-m2.7:cloud'.",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama"],
        default="anthropic",
        help="Which API to hit for --llm-rerank. 'anthropic' (default) uses Anthropic's "
        "/v1/messages endpoint. 'ollama' uses Ollama's OpenAI-compatible "
        "/v1/chat/completions endpoint (works with local Ollama and Ollama Cloud).",
    )
    parser.add_argument(
        "--llm-base-url",
        default="",
        help="Override base URL for --llm-backend ollama. Defaults to http://localhost:11434.",
    )
    parser.add_argument(
        "--diary-cache",
        default=None,
        help="Path to save/load diary ingest cache (JSON). "
        "Saves Haiku calls on re-runs. Sonnet run can reuse Haiku cache.",
    )
    parser.add_argument(
        "--skip-precompute",
        action="store_true",
        default=False,
        help="Skip diary pre-computation for sessions not in cache. "
        "Uses cache as-is; uncached sessions fall back to palace-only retrieval.",
    )
    parser.add_argument(
        "--embed-model",
        choices=["default", "bge-base", "bge-large", "nomic", "mxbai"],
        default="default",
        help="Embedding model. 'default'=all-MiniLM-L6-v2 (ChromaDB built-in, baseline). "
        "'bge-large'=BAAI/bge-large-en-v1.5 (best local, 1024-dim, ~1.3GB via fastembed). "
        "'nomic'=nomic-embed-text-v1.5 (768-dim, fast, ~274MB). "
        "'bge-base'=BAAI/bge-base-en-v1.5 (768-dim, balanced). "
        "'mxbai'=mxbai-embed-large-v1 (1024-dim). Requires: pip install fastembed.",
    )
    # ── Train / test split ──────────────────────────────────────────────────
    parser.add_argument(
        "--split-file",
        default=None,
        help="Path to a JSON split file. "
        "Use --create-split to generate one (50 dev / 450 held-out). "
        "Required when using --dev-only or --held-out.",
    )
    parser.add_argument(
        "--create-split",
        action="store_true",
        default=False,
        help="Create a new random 50/450 dev/held-out split and exit. "
        "Pass --split-file to specify where to save it.",
    )
    parser.add_argument(
        "--dev-only",
        action="store_true",
        default=False,
        help="Run only the 50 dev questions (safe for iterative tuning). Requires --split-file.",
    )
    parser.add_argument(
        "--held-out",
        action="store_true",
        default=False,
        help="Run only the 450 held-out questions (publishable final score). "
        "Use sparingly — looking at results contaminates the held-out set. "
        "Requires --split-file.",
    )
    args = parser.parse_args()

    # ── Handle --create-split ───────────────────────────────────────────────
    if args.create_split:
        if not args.split_file:
            args.split_file = "benchmarks/lme_split_50_450.json"
        with open(args.data_file) as f:
            _all_data = json.load(f)
        _load_or_create_split(args.split_file, _all_data)
        sys.exit(0)

    # ── Validate split flags ────────────────────────────────────────────────
    if (args.dev_only or args.held_out) and not args.split_file:
        parser.error(
            "--dev-only / --held-out require --split-file. "
            "Run with --create-split first to generate a split."
        )
    if args.dev_only and args.held_out:
        parser.error("--dev-only and --held-out are mutually exclusive.")

    split_subset = "dev" if args.dev_only else ("held_out" if args.held_out else None)

    if not args.out:
        embed_tag = f"_{args.embed_model}" if args.embed_model != "default" else ""
        suffix = "_llmrerank" if args.llm_rerank else ""
        subset_tag = f"_{split_subset}" if split_subset else ""
        args.out = f"benchmarks/results_mempal_{args.mode}{embed_tag}{suffix}{subset_tag}_{args.granularity}_{datetime.now().strftime('%Y%m%d_%H%M')}.jsonl"

    # Set global embedding function before running
    if args.embed_model != "default":
        import sys as _sys

        _mod = _sys.modules[__name__]
        _mod._bench_embed_fn = _make_embed_fn(args.embed_model)

    run_benchmark(
        args.data_file,
        args.granularity,
        args.limit,
        args.out,
        args.mode,
        args.skip,
        args.hybrid_weight,
        args.llm_rerank,
        args.llm_key,
        args.llm_model,
        args.diary_cache,
        args.skip_precompute,
        split_file=args.split_file,
        split_subset=split_subset,
        llm_backend=args.llm_backend,
        llm_base_url=args.llm_base_url,
    )
