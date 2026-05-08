#!/usr/bin/env python3
"""
MemPal × LoCoMo Benchmark
===========================

Evaluates MemPal's retrieval against the LoCoMo benchmark.
10 conversations, ~200 QA pairs across 5 categories.

For each conversation:
1. Ingest all sessions into a fresh MemPal palace
2. For each QA pair, query the palace
3. Score retrieval recall (did we find the evidence dialog?)
4. Score F1 (optional, if --llm is provided)

Usage:
    python benchmarks/locomo_bench.py /path/to/locomo/data/locomo10.json
    python benchmarks/locomo_bench.py /path/to/locomo/data/locomo10.json --top-k 10
    python benchmarks/locomo_bench.py /path/to/locomo/data/locomo10.json --mode hybrid
    python benchmarks/locomo_bench.py /path/to/locomo/data/locomo10.json --mode hybrid --llm-rerank
"""

import os
import sys
import json
import re
import string
import shutil
import tempfile
import argparse
import urllib.request
import urllib.error
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime

import chromadb

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Optional bge-large embeddings ────────────────────────────────────────────
_fastembed_model = None


def _get_embedder(model_name: str):
    """Lazy-load a fastembed model. Cached globally after first load."""
    global _fastembed_model
    if _fastembed_model is None:
        try:
            from fastembed import TextEmbedding

            print(f"  Loading embedding model: {model_name} (first run may download ~1.3GB)")
            _fastembed_model = TextEmbedding(model_name=model_name)
            print("  Embedding model loaded.")
        except ImportError:
            print("  fastembed not installed — pip3 install fastembed")
            sys.exit(1)
    return _fastembed_model


def _embed(texts: list, embed_model: str) -> list:
    """Embed a list of texts. Returns list of float lists, or None for default."""
    if not embed_model or embed_model == "default":
        return None
    embedder = _get_embedder(embed_model)
    return [vec.tolist() for vec in embedder.embed(texts)]


def _query(collection, question: str, n_results: int, embed_model: str, include=None, where=None):
    """Query collection with either query_texts or query_embeddings."""
    if include is None:
        include = ["distances", "metadatas", "documents"]
    q_emb = _embed([question], embed_model)
    kwargs = dict(n_results=n_results, include=include)
    if where:
        kwargs["where"] = where
    if q_emb is not None:
        kwargs["query_embeddings"] = q_emb
    else:
        kwargs["query_texts"] = [question]
    return collection.query(**kwargs)


CATEGORIES = {
    1: "Single-hop",
    2: "Temporal",
    3: "Temporal-inference",
    4: "Open-domain",
    5: "Adversarial",
}


# =============================================================================
# METRICS (from LoCoMo's evaluation.py)
# =============================================================================


def normalize_answer(s):
    """Normalize answer for F1 comparison."""
    s = s.replace(",", "")
    s = re.sub(r"\b(a|an|the|and)\b", " ", s)
    s = " ".join(s.split())
    s = "".join(ch for ch in s if ch not in string.punctuation)
    return s.lower().strip()


def f1_score(prediction, ground_truth):
    """Token-level F1 with normalization."""
    pred_tokens = normalize_answer(prediction).split()
    truth_tokens = normalize_answer(ground_truth).split()
    if not pred_tokens or not truth_tokens:
        return float(pred_tokens == truth_tokens)
    common = Counter(pred_tokens) & Counter(truth_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(truth_tokens)
    return (2 * precision * recall) / (precision + recall)


# =============================================================================
# DATA LOADING
# =============================================================================


def load_conversation_sessions(conversation, session_summaries=None):
    """Extract sessions from a LoCoMo conversation dict."""
    sessions = []
    session_num = 1
    while True:
        key = f"session_{session_num}"
        date_key = f"session_{session_num}_date_time"
        if key not in conversation:
            break
        dialogs = conversation[key]
        date = conversation.get(date_key, "")
        summary = ""
        if session_summaries:
            summary = session_summaries.get(f"session_{session_num}_summary", "")
        sessions.append(
            {
                "session_num": session_num,
                "date": date,
                "dialogs": dialogs,
                "summary": summary,
            }
        )
        session_num += 1
    return sessions


def build_corpus_from_sessions(sessions, granularity="dialog"):
    """
    Build retrieval corpus from conversation sessions.

    granularity:
        'dialog'  — one doc per dialog turn (matches evidence format D1:3)
        'session' — one doc per session (all dialog text joined)
        'rooms'   — one doc per session using pre-computed summary (palace room label)
    """
    corpus = []
    corpus_ids = []
    corpus_timestamps = []

    for sess in sessions:
        if granularity in ("session", "rooms"):
            if granularity == "rooms" and sess.get("summary"):
                doc = sess["summary"]
            else:
                texts = []
                for d in sess["dialogs"]:
                    speaker = d.get("speaker", "?")
                    text = d.get("text", "")
                    texts.append(f'{speaker} said, "{text}"')
                doc = "\n".join(texts)
            corpus.append(doc)
            corpus_ids.append(f"session_{sess['session_num']}")
            corpus_timestamps.append(sess["date"])
        else:
            for d in sess["dialogs"]:
                dia_id = d.get("dia_id", f"D{sess['session_num']}:?")
                speaker = d.get("speaker", "?")
                text = d.get("text", "")
                doc = f'{speaker} said, "{text}"'
                corpus.append(doc)
                corpus_ids.append(dia_id)
                corpus_timestamps.append(sess["date"])

    return corpus, corpus_ids, corpus_timestamps


# =============================================================================
# HYBRID V4 SCORING — same logic as longmemeval_bench.py hybrid_v4
# =============================================================================

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
    "said",
}

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
    "Said",
    "Speaker",
    "Person",
    "Time",
    "Date",
    "Year",
    "Day",
}


def _kw(text):
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def _kw_overlap(query_kws, doc_text):
    doc_lower = doc_text.lower()
    if not query_kws:
        return 0.0
    hits = sum(1 for kw in query_kws if kw in doc_lower)
    return hits / len(query_kws)


def _quoted_phrases(text):
    phrases = []
    for pat in [r"'([^']{3,60})'", r'"([^"]{3,60})"']:
        phrases.extend(re.findall(pat, text))
    return [p.strip() for p in phrases if len(p.strip()) >= 3]


def _quoted_boost(phrases, doc_text):
    if not phrases:
        return 0.0
    doc_lower = doc_text.lower()
    hits = sum(1 for p in phrases if p.lower() in doc_lower)
    return min(hits / len(phrases), 1.0)


def _person_names(text):
    words = re.findall(r"\b[A-Z][a-z]{2,15}\b", text)
    return list(set(w for w in words if w not in NOT_NAMES))


def _name_boost(names, doc_text):
    if not names:
        return 0.0
    doc_lower = doc_text.lower()
    hits = sum(1 for n in names if n.lower() in doc_lower)
    return min(hits / len(names), 1.0)


# =============================================================================
# PALACE MODE — LLM-assisted room assignment at index time
# =============================================================================

# Room taxonomy for LoCoMo-style personal conversations.
# Broad enough to cover common life topics, specific enough to discriminate.
PALACE_ROOMS = [
    "identity_sexuality",  # gender identity, LGBTQ, self-discovery
    "career_education",  # jobs, research, school, studying, counseling
    "relationships_romance",  # dating, partners, romantic feelings
    "family_children",  # kids, parents, siblings, family events
    "health_wellness",  # physical health, mental health, therapy, fitness
    "hobbies_creativity",  # painting, music, sports, art, crafts
    "social_community",  # friends, support groups, events, volunteering
    "home_living",  # moving, apartment, home, neighborhood
    "travel_places",  # trips, vacations, visiting somewhere
    "food_cooking",  # meals, restaurants, cooking, recipes
    "money_finance",  # spending, saving, bills, budgeting
    "emotions_mood",  # feelings, stress, happiness, grief, anxiety
    "media_entertainment",  # movies, books, music, TV, games
    "general",  # catch-all for mixed/unclear sessions
]

_PALACE_ROOM_LIST = "\n".join(f"  - {r}" for r in PALACE_ROOMS)


def _llm_call(prompt, api_key, model="claude-haiku-4-5-20251001", max_tokens=32):
    """Minimal LLM call. Returns text response or empty string on failure."""
    payload = json.dumps(
        {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
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
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read())
        return result["content"][0]["text"].strip()
    except Exception:
        return ""


def _assign_room(session_text, api_key, model="claude-haiku-4-5-20251001"):
    """Ask LLM to assign a session to a palace room. Returns room name."""
    snippet = session_text[:600].replace("\n", " ")
    prompt = (
        f"Read this conversation and assign it to exactly one room from the list below.\n"
        f"Reply with ONLY the room name, nothing else.\n\n"
        f"Rooms:\n{_PALACE_ROOM_LIST}\n\n"
        f"Conversation:\n{snippet}"
    )
    raw = _llm_call(prompt, api_key, model=model, max_tokens=20)
    # Normalize: find the closest matching room name
    raw_lower = raw.lower().strip()
    for room in PALACE_ROOMS:
        if room in raw_lower or raw_lower in room:
            return room
    # Partial match on first word
    first_word = raw_lower.split("_")[0].split()[0] if raw_lower else ""
    for room in PALACE_ROOMS:
        if first_word and first_word in room:
            return room
    return "general"


def _route_question(question, api_key, model="claude-haiku-4-5-20251001"):
    """Ask LLM which 1-2 rooms a question is about. Returns list of room names."""
    prompt = (
        f"Which 1 or 2 rooms from the list below does this question relate to?\n"
        f"Reply with ONLY room name(s), comma-separated if two, nothing else.\n\n"
        f"Rooms:\n{_PALACE_ROOM_LIST}\n\n"
        f"Question: {question}"
    )
    raw = _llm_call(prompt, api_key, model=model, max_tokens=40)
    raw_lower = raw.lower()
    found = []
    for room in PALACE_ROOMS:
        if room in raw_lower:
            found.append(room)
        if len(found) >= 2:
            break
    if not found:
        # fallback: partial word match
        for part in re.split(r"[,\s]+", raw_lower):
            part = part.strip("_").strip()
            for room in PALACE_ROOMS:
                if part and part in room and room not in found:
                    found.append(room)
                if len(found) >= 2:
                    break
    return found or PALACE_ROOMS  # if routing fails, search everywhere


def palace_assign_rooms(sessions, sample_id, api_key, cache, model="claude-haiku-4-5-20251001"):
    """
    Assign each session to a palace room. Uses cache to avoid re-calling LLM.

    cache: dict loaded from palace_cache file, mutated in place.
    Returns dict: session_id → room_name
    """
    assignments = {}
    for sess in sessions:
        sess_key = f"{sample_id}_session_{sess['session_num']}"
        if sess_key in cache:
            assignments[f"session_{sess['session_num']}"] = cache[sess_key]
            continue

        # Build session text for LLM
        texts = []
        for d in sess["dialogs"]:
            speaker = d.get("speaker", "?")
            text = d.get("text", "")
            texts.append(f"{speaker}: {text}")
        session_text = "\n".join(texts)

        # Prefer summary if available (shorter, cleaner)
        summary = sess.get("summary", "")
        llm_input = summary if summary else session_text

        room = _assign_room(llm_input, api_key, model=model)
        assignments[f"session_{sess['session_num']}"] = room
        cache[sess_key] = room

    return assignments


# =============================================================================
# LLM RERANK
# =============================================================================


def llm_rerank_locomo(
    question,
    retrieved_ids,
    retrieved_docs,
    api_key,
    top_k=10,
    model="claude-sonnet-4-6",
    backend="anthropic",
    base_url="",
):
    """
    Ask LLM to pick the single most relevant document for this question.
    Returns reordered retrieved_ids with the best candidate first.

    Supports backend="anthropic" (default) or "ollama" (OpenAI-compat endpoint).
    """
    candidates = retrieved_ids[:top_k]
    candidate_docs = retrieved_docs[:top_k]

    if len(candidates) <= 1:
        return retrieved_ids

    lines = []
    for i, (cid, doc) in enumerate(zip(candidates, candidate_docs), 1):
        snippet = doc[:300].replace("\n", " ")
        lines.append(f"{i}. [{cid}] {snippet}")

    prompt = (
        f"Question: {question}\n\n"
        f"Which of the following passages most directly answers this question? "
        f"Reply with just the number (1-{len(candidates)}).\n\n" + "\n".join(lines)
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
            with urllib.request.urlopen(req, timeout=120 if backend == "ollama" else 30) as resp:
                result = json.loads(resp.read())
            if backend == "ollama":
                msg = result["choices"][0]["message"]
                raw = (msg.get("content") or "").strip() or (msg.get("reasoning") or "").strip()
            else:
                raw = result["content"][0]["text"].strip()
            # Take LAST integer — reasoning models often count candidates first
            m = re.search(r"\b(\d+)\b", raw[::-1])
            if m:
                pick = int(m.group(1)[::-1])
                if 1 <= pick <= len(candidates):
                    chosen_id = candidates[pick - 1]
                    reordered = [chosen_id] + [cid for cid in retrieved_ids if cid != chosen_id]
                    return reordered
            break
        except (_socket.timeout, TimeoutError):
            if _attempt < 2:
                import time as _time

                _time.sleep(3)
        except (urllib.error.URLError, KeyError, ValueError, IndexError, OSError):
            break

    return retrieved_ids


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


def run_benchmark(
    data_file,
    top_k=10,
    mode="raw",
    limit=0,
    granularity="dialog",
    out_file=None,
    llm_rerank_enabled=False,
    llm_key="",
    llm_model="claude-sonnet-4-6",
    hybrid_weight=0.30,
    palace_cache_file=None,
    palace_model="claude-haiku-4-5-20251001",
    embed_model="default",
    llm_backend="anthropic",
    llm_base_url="",
):
    """Run LoCoMo retrieval benchmark."""
    with open(data_file) as f:
        data = json.load(f)

    if limit > 0:
        data = data[:limit]

    api_key = ""
    if llm_rerank_enabled or mode == "palace":
        api_key = _load_api_key(llm_key)
        # Ollama backend doesn't require an Anthropic key. Palace mode still does
        # (it uses Anthropic for room-assignment indexing) — so only relax the
        # requirement when rerank is the ONLY llm use and backend is ollama.
        needs_key = mode == "palace" or (llm_rerank_enabled and llm_backend == "anthropic")
        if needs_key and not api_key:
            print(f"ERROR: --mode {mode} / --llm-rerank (anthropic) requires an API key.")
            sys.exit(1)

    # Palace mode: load or create room assignment cache
    palace_cache = {}
    _palace_cache_path = None
    if mode == "palace":
        _palace_cache_path = palace_cache_file or str(
            Path(__file__).parent / "palace_cache_locomo.json"
        )
        if Path(_palace_cache_path).exists():
            with open(_palace_cache_path) as f:
                palace_cache = json.load(f)
            print(f"  Palace cache: {len(palace_cache)} room assignments loaded")

    rerank_label = f" + LLM re-rank ({llm_model.split('-')[1]})" if llm_rerank_enabled else ""

    print(f"\n{'=' * 60}")
    print("  MemPal × LoCoMo Benchmark")
    print(f"{'=' * 60}")
    print(f"  Data:        {Path(data_file).name}")
    print(f"  Conversations: {len(data)}")
    print(f"  Top-k:       {top_k}")
    print(f"  Mode:        {mode}{rerank_label}")
    print(f"  Granularity: {granularity}")
    print(f"{'─' * 60}\n")

    all_recall = []
    per_category = defaultdict(list)
    results_log = []
    total_qa = 0

    start_time = datetime.now()

    for conv_idx, sample in enumerate(data):
        sample_id = sample.get("sample_id", f"conv-{conv_idx}")
        conversation = sample["conversation"]
        qa_pairs = sample["qa"]

        session_summaries = sample.get("session_summary", {})
        sessions = load_conversation_sessions(conversation, session_summaries)
        corpus, corpus_ids, corpus_timestamps = build_corpus_from_sessions(
            sessions, granularity=granularity
        )

        # Palace mode: assign each session to a room via LLM
        room_assignments = {}
        if mode == "palace":
            room_assignments = palace_assign_rooms(
                sessions, sample_id, api_key, palace_cache, model=palace_model
            )
            # Persist updated cache after each conversation
            if _palace_cache_path:
                with open(_palace_cache_path, "w") as f:
                    json.dump(palace_cache, f, indent=2)
            rooms_summary = {}
            for sid, room in room_assignments.items():
                rooms_summary[room] = rooms_summary.get(room, 0) + 1
            print(
                f"  [{conv_idx + 1}/{len(data)}] {sample_id}: "
                f"{len(sessions)} sessions → {len(rooms_summary)} rooms, {len(qa_pairs)} questions"
            )
            print(f"    Rooms: {dict(sorted(rooms_summary.items(), key=lambda x: -x[1]))}")
        else:
            print(
                f"  [{conv_idx + 1}/{len(data)}] {sample_id}: "
                f"{len(sessions)} sessions, {len(corpus)} docs, {len(qa_pairs)} questions"
            )

        tmpdir = tempfile.mkdtemp(prefix="mempal_locomo_")
        palace_path = os.path.join(tmpdir, "palace")

        try:
            client = chromadb.PersistentClient(path=palace_path)
            collection = client.create_collection("mempal_drawers")

            if mode == "aaak":
                from mempalace.dialect import Dialect

                dialect = Dialect()
                docs_to_ingest = [dialect.compress(doc) for doc in corpus]
            else:
                docs_to_ingest = corpus

            corpus_embeddings = _embed(docs_to_ingest, embed_model)
            add_kwargs = dict(
                documents=docs_to_ingest,
                ids=[f"doc_{i}" for i in range(len(corpus))],
                metadatas=[
                    {
                        "corpus_id": cid,
                        "timestamp": ts,
                        "room": room_assignments.get(cid, "general"),
                    }
                    for cid, ts in zip(corpus_ids, corpus_timestamps)
                ],
            )
            if corpus_embeddings is not None:
                add_kwargs["embeddings"] = corpus_embeddings
            collection.add(**add_kwargs)

            for qa in qa_pairs:
                question = qa["question"]
                answer = qa.get("answer", qa.get("adversarial_answer", ""))
                category = qa["category"]
                evidence = qa.get("evidence", [])

                # Extract names + predicate keywords once (used by hybrid, rooms, palace)
                names = _person_names(question) if mode in ("hybrid", "rooms", "palace") else []
                name_words = {n.lower() for n in names}
                all_kws = _kw(question) if mode in ("hybrid", "rooms", "palace") else []
                predicate_kws = [w for w in all_kws if w not in name_words]
                quoted = _quoted_phrases(question) if mode in ("hybrid", "rooms", "palace") else []

                if mode == "palace":
                    # ── True palace navigation ────────────────────────────────
                    # Route using conversation-specific room summaries.
                    # This ensures the same vocabulary used at INDEX TIME (session
                    # summaries) is also used at QUERY TIME — no global taxonomy mismatch.
                    #
                    # Build: room → aggregated summary text for this conversation
                    room_summaries: dict[str, list[str]] = {}
                    for sess in sessions:
                        sess_id = f"session_{sess['session_num']}"
                        room = room_assignments.get(sess_id, "general")
                        summary = sess.get("summary", "")
                        if room not in room_summaries:
                            room_summaries[room] = []
                        if summary:
                            room_summaries[room].append(summary)

                    # Score each room by predicate keyword overlap against its aggregate
                    room_kw_scores = []
                    for room, summaries in room_summaries.items():
                        agg_text = " ".join(summaries)
                        overlap = _kw_overlap(predicate_kws, agg_text) if predicate_kws else 0.0
                        room_kw_scores.append((overlap, room))
                    room_kw_scores.sort(reverse=True)

                    # Take top-3 rooms; if top score is 0, open up to all (no signal)
                    n_rooms_to_search = 3
                    if room_kw_scores and room_kw_scores[0][0] == 0.0:
                        n_rooms_to_search = len(room_kw_scores)
                    target_rooms = [r for _, r in room_kw_scores[:n_rooms_to_search]]

                    # Filter to sessions in those rooms
                    if len(target_rooms) < len(room_summaries):
                        where_filter = {"room": {"$in": target_rooms}}
                    else:
                        where_filter = None  # all rooms — skip filter

                    # How many sessions are in those rooms?
                    sessions_in_rooms = (
                        sum(
                            1
                            for cid in corpus_ids
                            if room_assignments.get(cid, "general") in target_rooms
                        )
                        if where_filter
                        else len(corpus)
                    )
                    n_retrieve = max(top_k, min(sessions_in_rooms, len(corpus)))

                    results_p = _query(
                        collection, question, n_retrieve, embed_model, where=where_filter
                    )
                    raw_ids = [m["corpus_id"] for m in results_p["metadatas"][0]]
                    raw_distances = results_p["distances"][0]
                    raw_docs = results_p["documents"][0]

                    # Hybrid_v5 rerank within the room (small set — clean signal)
                    scored = []
                    for cid, dist, doc in zip(raw_ids, raw_distances, raw_docs):
                        pred_overlap = _kw_overlap(predicate_kws, doc)
                        fused = dist * (1.0 - 0.50 * pred_overlap)
                        q_boost = _quoted_boost(quoted, doc)
                        if q_boost > 0:
                            fused *= 1.0 - 0.60 * q_boost
                        n_boost = _name_boost(names, doc)
                        if n_boost > 0:
                            fused *= 1.0 - 0.20 * n_boost
                        scored.append((cid, dist, doc, fused))
                    scored.sort(key=lambda x: x[3])
                    retrieved_ids = [x[0] for x in scored[:top_k]]
                    retrieved_docs = [x[2] for x in scored[:top_k]]

                elif mode == "rooms":
                    # ── Two-stage palace navigation ──────────────────────────────
                    # Stage 1: route via session summaries to find relevant rooms.
                    #   Score each session's summary by predicate keyword overlap.
                    #   Keep top third of sessions (or at least top_k sessions).
                    n_rooms = max(top_k, len(sessions) // 3)
                    room_scores = []
                    for sess in sessions:
                        summary = sess.get("summary", "")
                        overlap = (
                            _kw_overlap(predicate_kws, summary)
                            if (summary and predicate_kws)
                            else 0.0
                        )
                        room_scores.append((overlap, f"session_{sess['session_num']}"))
                    room_scores.sort(reverse=True)
                    top_room_ids = [sid for _, sid in room_scores[:n_rooms]]

                    # Stage 2: embedding query filtered to those rooms, then hybrid rerank
                    n_in_rooms = min(top_k * 2, len(top_room_ids))
                    where_filter = (
                        {"corpus_id": {"$in": top_room_ids}} if len(top_room_ids) > 1 else None
                    )
                    results_r = _query(
                        collection, question, n_in_rooms, embed_model, where=where_filter
                    )
                    raw_ids = [m["corpus_id"] for m in results_r["metadatas"][0]]
                    raw_distances = results_r["distances"][0]
                    raw_docs = results_r["documents"][0]

                    scored = []
                    for cid, dist, doc in zip(raw_ids, raw_distances, raw_docs):
                        pred_overlap = _kw_overlap(predicate_kws, doc)
                        fused = dist * (1.0 - 0.50 * pred_overlap)
                        q_boost = _quoted_boost(quoted, doc)
                        if q_boost > 0:
                            fused *= 1.0 - 0.60 * q_boost
                        n_boost = _name_boost(names, doc)
                        if n_boost > 0:
                            fused *= 1.0 - 0.20 * n_boost
                        scored.append((cid, dist, doc, fused))
                    scored.sort(key=lambda x: x[3])
                    retrieved_ids = [x[0] for x in scored[:top_k]]
                    retrieved_docs = [x[2] for x in scored[:top_k]]

                else:
                    # ── Standard query + optional hybrid rerank ──────────────────
                    n_retrieve = min(top_k * 3 if mode == "hybrid" else top_k, len(corpus))
                    results = _query(collection, question, n_retrieve, embed_model)
                    raw_ids = [m["corpus_id"] for m in results["metadatas"][0]]
                    raw_distances = results["distances"][0]
                    raw_docs = results["documents"][0]

                    if mode == "hybrid":
                        scored = []
                        for i, (cid, dist, doc) in enumerate(zip(raw_ids, raw_distances, raw_docs)):
                            pred_overlap = _kw_overlap(predicate_kws, doc)
                            fused = dist * (1.0 - 0.50 * pred_overlap)
                            q_boost = _quoted_boost(quoted, doc)
                            if q_boost > 0:
                                fused *= 1.0 - 0.60 * q_boost
                            n_boost = _name_boost(names, doc)
                            if n_boost > 0:
                                fused *= 1.0 - 0.20 * n_boost
                            scored.append((i, cid, dist, doc, fused))
                        scored.sort(key=lambda x: x[4])
                        retrieved_ids = [x[1] for x in scored][:top_k]
                        retrieved_docs = [x[3] for x in scored][:top_k]
                    else:
                        retrieved_ids = raw_ids[:top_k]
                        retrieved_docs = raw_docs[:top_k]

                # LLM rerank
                if llm_rerank_enabled and api_key:
                    rerank_pool = min(10, len(retrieved_ids))
                    retrieved_ids = llm_rerank_locomo(
                        question,
                        retrieved_ids,
                        retrieved_docs,
                        api_key,
                        top_k=rerank_pool,
                        model=llm_model,
                        backend=llm_backend,
                        base_url=llm_base_url,
                    )

                # Compute recall
                if granularity == "dialog":
                    evidence_set = evidence_to_dialog_ids(evidence)
                else:
                    evidence_set = evidence_to_session_ids(evidence)

                recall = compute_retrieval_recall(retrieved_ids, evidence_set)
                all_recall.append(recall)
                per_category[category].append(recall)
                total_qa += 1

                results_log.append(
                    {
                        "sample_id": sample_id,
                        "question": question,
                        "answer": answer,
                        "category": category,
                        "evidence": evidence,
                        "retrieved_ids": retrieved_ids,
                        "recall": recall,
                    }
                )

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    elapsed = (datetime.now() - start_time).total_seconds()

    avg_recall = sum(all_recall) / len(all_recall) if all_recall else 0

    print(f"\n{'=' * 60}")
    print(f"  RESULTS — MemPal ({mode}{rerank_label}, {granularity}, top-{top_k})")
    print(f"{'=' * 60}")
    print(f"  Time:        {elapsed:.1f}s ({elapsed / max(total_qa, 1):.2f}s per question)")
    print(f"  Questions:   {total_qa}")
    print(f"  Avg Recall:  {avg_recall:.3f}")

    print("\n  PER-CATEGORY RECALL:")
    for cat in sorted(per_category.keys()):
        vals = per_category[cat]
        avg = sum(vals) / len(vals)
        name = CATEGORIES.get(cat, f"Cat-{cat}")
        print(f"    {name:25} R={avg:.3f}  (n={len(vals)})")

    perfect = sum(1 for r in all_recall if r >= 1.0)
    partial = sum(1 for r in all_recall if 0 < r < 1.0)
    zero = sum(1 for r in all_recall if r == 0)
    print("\n  RECALL DISTRIBUTION:")
    print(f"    Perfect (1.0):  {perfect:4} ({perfect / len(all_recall) * 100:.1f}%)")
    print(f"    Partial (0-1):  {partial:4} ({partial / len(all_recall) * 100:.1f}%)")
    print(f"    Zero (0.0):     {zero:4} ({zero / len(all_recall) * 100:.1f}%)")

    print(f"\n{'=' * 60}\n")

    if out_file:
        with open(out_file, "w") as f:
            json.dump(results_log, f, indent=2)
        print(f"  Results saved to: {out_file}")


# =============================================================================
# RETRIEVAL HELPERS (used by run_benchmark)
# =============================================================================


def compute_retrieval_recall(retrieved_ids, evidence_ids):
    """What fraction of evidence dialog IDs were retrieved?"""
    if not evidence_ids:
        return 1.0
    found = sum(1 for eid in evidence_ids if eid in retrieved_ids)
    return found / len(evidence_ids)


def evidence_to_dialog_ids(evidence):
    return set(evidence)


def evidence_to_session_ids(evidence):
    sessions = set()
    for eid in evidence:
        match = re.match(r"D(\d+):", eid)
        if match:
            sessions.add(f"session_{match.group(1)}")
    return sessions


# =============================================================================
# CLI
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MemPal × LoCoMo Benchmark")
    parser.add_argument("data_file", help="Path to locomo10.json")
    parser.add_argument("--top-k", type=int, default=50, help="Top-k retrieval (default: 50)")
    parser.add_argument(
        "--mode",
        choices=["raw", "aaak", "hybrid", "rooms", "palace"],
        default="raw",
        help="Retrieval mode: raw, hybrid (v5), rooms (keyword routing), palace (LLM room assignment)",
    )
    parser.add_argument(
        "--palace-cache", default=None, help="Path to palace room assignment cache JSON"
    )
    parser.add_argument(
        "--palace-model",
        default="claude-haiku-4-5-20251001",
        help="Model for palace room assignment (default: haiku)",
    )
    parser.add_argument(
        "--granularity",
        choices=["dialog", "session"],
        default="session",
        help="Corpus granularity: dialog (per turn) or session (per session)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Limit to N conversations")
    parser.add_argument("--out", default=None, help="Output JSON file path")
    parser.add_argument("--llm-rerank", action="store_true", help="Use LLM to rerank top results")
    parser.add_argument(
        "--llm-model",
        default="claude-sonnet-4-6",
        help="Model for LLM rerank (default: claude-sonnet-4-6)",
    )
    parser.add_argument("--llm-key", default="", help="API key (or set ANTHROPIC_API_KEY env var)")
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama"],
        default="anthropic",
        help="Which API for --llm-rerank. 'anthropic' (default) or 'ollama' "
        "(OpenAI-compat /v1/chat/completions — works for local + Ollama Cloud).",
    )
    parser.add_argument(
        "--llm-base-url",
        default="",
        help="Override base URL for --llm-backend ollama. Default: http://localhost:11434.",
    )
    parser.add_argument(
        "--hybrid-weight",
        type=float,
        default=0.30,
        help="Keyword overlap weight for hybrid mode (default: 0.30)",
    )
    parser.add_argument(
        "--embed-model",
        default="default",
        help="Embedding model: 'default' (ChromaDB built-in) or "
        "'BAAI/bge-large-en-v1.5' (requires fastembed)",
    )
    args = parser.parse_args()

    if not args.out:
        rerank_tag = "_llmrerank" if args.llm_rerank else ""
        args.out = (
            f"benchmarks/results_locomo_{args.mode}{rerank_tag}"
            f"_{args.granularity}_top{args.top_k}"
            f"_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        )

    run_benchmark(
        args.data_file,
        args.top_k,
        args.mode,
        args.limit,
        args.granularity,
        args.out,
        args.llm_rerank,
        args.llm_key,
        args.llm_model,
        args.hybrid_weight,
        palace_cache_file=args.palace_cache,
        palace_model=args.palace_model,
        embed_model=args.embed_model,
        llm_backend=args.llm_backend,
        llm_base_url=args.llm_base_url,
    )
