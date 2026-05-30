#!/usr/bin/env python3
"""
MemPalace × BEAM 100K Benchmark
================================

Evaluates MemPalace as a RAG memory backend against the BEAM 100K benchmark
(Tavakoli et al., 2024  - https://github.com/mohammadtavakoli78/BEAM).

Pipeline:
  1. Ingest all user messages for a conversation into ChromaDB (MemPalace style)
  2. For each question, retrieve top-K relevant chunks
  3. Synthesize an answer from retrieved chunks using an LLM
  4. Score with BEAM's official 3-tier rubric judge (1.0 / 0.5 / 0.0)

This is a fair end-to-end comparison: MemPalace handles retrieval,
an LLM handles synthesis, and the official BEAM judge scores.

Usage:
    # Single conversation (quick test, auto-downloads dataset on first run)
    python benchmarks/beam_100k_bench.py

    # Full 20-conversation run
    python benchmarks/beam_100k_bench.py --full

    # With pre-downloaded dataset
    python benchmarks/beam_100k_bench.py data/beam-100k.json --full

    # Custom settings
    python benchmarks/beam_100k_bench.py --top-k 10 --conv 3

    # With custom embedding model
    python benchmarks/beam_100k_bench.py --embed-model bge-large --full

Environment:
    AZURE_OPENAI_API_KEY         - Azure OpenAI API key
    AZURE_OPENAI_ENDPOINT        - Azure endpoint URL
    AZURE_OPENAI_API_VERSION     - API version (default: 2025-04-28)
    AZURE_OPENAI_CHAT_MODEL      - Chat model name (default: gpt-5.4-mini)
    OPENAI_API_KEY               - Standard OpenAI key (fallback)
"""

import os
import re
import sys
import json
import time
import argparse
import urllib.request
from pathlib import Path
from collections import defaultdict
from datetime import datetime

import chromadb

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

# Shared dataset utilities (download, parse, cache)
# Note: SSL verification is bypassed only inside _beam_utils.download_beam_parquet()
# for the HuggingFace download. LLM API calls (OpenAI, Anthropic) keep default
# certificate verification.
from _beam_utils import ensure_beam_dataset


# =============================================================================
# LLM CLIENT (Azure OpenAI or standard OpenAI)
# =============================================================================


def _create_llm_client():
    """Create an OpenAI-compatible client (Azure or standard)."""
    try:
        from openai import AzureOpenAI, OpenAI
    except ImportError:
        print("ERROR: openai package not installed. Run: pip install openai")
        sys.exit(1)

    azure_key = os.environ.get("AZURE_OPENAI_API_KEY")
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")

    if azure_key and azure_endpoint:
        api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-28")
        model = os.environ.get("AZURE_OPENAI_CHAT_MODEL", "gpt-5.4-mini")
        client = AzureOpenAI(
            api_key=azure_key,
            azure_endpoint=azure_endpoint,
            api_version=api_version,
        )
        print(f"  LLM: Azure OpenAI ({model} via {azure_endpoint})")
        return client, model

    openai_key = os.environ.get("OPENAI_API_KEY")
    if openai_key:
        model = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
        client = OpenAI(api_key=openai_key)
        print(f"  LLM: OpenAI ({model})")
        return client, model

    print("ERROR: No LLM credentials found.")
    print("  Set AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT")
    print("  Or set OPENAI_API_KEY")
    sys.exit(1)


def llm_chat(client, model, messages, max_tokens=512, temperature=0.0, json_mode=False):
    """Call the LLM with retry."""
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    # Newer models (gpt-5.x, o-series) use max_completion_tokens
    if "gpt-5" in model or model.startswith("o"):
        kwargs["max_completion_tokens"] = max_tokens
    else:
        kwargs["max_tokens"] = max_tokens
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    for attempt in range(3):
        try:
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content
        except Exception as e:
            if attempt < 2:
                wait = 2 ** (attempt + 1)
                print(f"    LLM error (retry in {wait}s): {e}")
                time.sleep(wait)
            else:
                print(f"    LLM failed after 3 attempts: {e}")
                return ""


# =============================================================================
# CHROMADB SETUP
# =============================================================================

# Lazy-initialized so that `import beam_100k_bench` has no side effects.
# The client is created on first call to _get_bench_client(). This matches
# the pattern in convomem_bench.py (which creates its client inside the
# per-evaluation function) rather than longmemeval_bench.py (which creates
# its client at module load).
_bench_client = None
_bench_embed_fn = None


def _get_bench_client():
    """Return the shared EphemeralClient, creating it on first use."""
    global _bench_client
    if _bench_client is None:
        _bench_client = chromadb.EphemeralClient()
    return _bench_client


def _make_embed_fn(model_name):
    """Return a ChromaDB-compatible embedding function."""
    if not model_name or model_name == "default":
        return None

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
                print(f"  Loading embedding model: {name}...")
                self._model = TextEmbedding(name)
                print("  Model ready.")

            def __call__(self, input: Documents) -> Embeddings:
                return [list(vec) for vec in self._model.embed(input)]

        return _FastEmbedFn(hf_name)
    except ImportError:
        print("WARN: fastembed not installed, using default embeddings")
        return None


def _fresh_collection(name="mempalace_beam"):
    """Delete and recreate collection for a clean conversation."""
    global _bench_embed_fn
    client = _get_bench_client()
    try:
        client.delete_collection(name)
    except Exception:
        pass
    if _bench_embed_fn is not None:
        return client.create_collection(name, embedding_function=_bench_embed_fn)
    return client.create_collection(name)


# =============================================================================
# BEAM OFFICIAL JUDGE PROMPT (verbatim from BEAM paper)
# =============================================================================

BEAM_JUDGE_PROMPT = """You are an expert evaluator tasked with judging whether the LLM's response demonstrates compliance with the specified RUBRIC CRITERION.

## EVALUATION INPUTS
- QUESTION (what the user asked): <question>
- RUBRIC CRITERION (what to check): <rubric_item>
- RESPONSE TO EVALUATE: <llm_response>

## EVALUATION RUBRIC:
The rubric defines a specific requirement, constraint, or expected behavior that the LLM response should demonstrate.

**IMPORTANT**: Pay careful attention to whether the rubric specifies:
- **Positive requirements** (things the response SHOULD include/do)
- **Negative constraints** (things the response SHOULD NOT include/do, often indicated by "no", "not", "avoid", "absent")

## RESPONSIVENESS REQUIREMENT (anchored to the QUESTION)
A compliant response must be **on-topic with respect to the QUESTION** and attempt to answer it.
- If the response does not address the QUESTION, score **0.0** and stop.
- For negative constraints, both must hold: (a) the response is responsive to the QUESTION, and (b) the prohibited element is absent.

## SEMANTIC TOLERANCE RULES:
Judge by meaning, not exact wording.
- Accept **paraphrases** and **synonyms** that preserve intent.
- **Case/punctuation/whitespace** differences must be ignored.
- **Numbers/currencies/dates** may appear in equivalent forms (e.g., "$68,000", "68k", "68,000 USD", or "sixty-eight thousand dollars"). Treat them as equal when numerically equivalent.
- If the rubric expects a number or duration, prefer **normalized comparison** (extract and compare values) over string matching.

## STYLE NEUTRALITY (prevents style contamination):
Ignore tone, politeness, length, and flourish unless the rubric explicitly requires a format/structure (e.g., "itemized list", "no citations", "one sentence").
- Do **not** penalize hedging, voice, or verbosity if content satisfies the rubric.
- Only evaluate format when the rubric **explicitly** mandates it.

## SCORING SCALE:
- **1.0 (Complete Compliance)**: Fully complies with the rubric criterion.
  - Positive: required element present, accurate, properly executed (allowing semantic equivalents).
  - Negative: prohibited element **absent** AND response is **responsive**.

- **0.5 (Partial Compliance)**: Partially complies.
  - Positive: element present but minor inaccuracies/incomplete execution.
  - Negative: generally responsive and mostly avoids the prohibited element but with minor/edge violations.

- **0.0 (No Compliance)**: Fails to comply.
  - Positive: required element missing or incorrect.
  - Negative: prohibited element present **or** response is non-responsive/evasive even if the element is absent.

## EVALUATION INSTRUCTIONS:
1. **Understand the Requirement**: Determine if the rubric is asking for something to be present (positive) or absent (negative/constraint).

2. **Parse Compound Statements**: If the rubric contains multiple elements connected by "and" or commas, evaluate whether:
   - **All elements** must be present for full compliance (1.0)
   - **Some elements** present indicates partial compliance (0.5)
   - **No elements** present indicates no compliance (0.0)

3. **Check Compliance**:
   - For positive requirements: Look for the presence and quality of the required element
   - For negative constraints: Look for the absence of the prohibited element

4. **Assign Score**: Based on compliance with the specific rubric criterion according to the scoring scale above.

5. **Provide Reasoning**: Explain whether the rubric criterion was satisfied and justify the score.

## OUTPUT FORMAT:
Return your evaluation in JSON format with two fields:

{
   "score": [your score: 1.0, 0.5, or 0.0],
   "reason": "[detailed explanation]"
}

NOTE: ONLY output the json object, without any explanation before or after that"""


# =============================================================================
# SYNTHESIS PROMPT
# =============================================================================

SYNTHESIS_SYSTEM = """You are a helpful AI assistant with access to a conversational memory system.
Answer the user's question based ONLY on the retrieved memory notes below.
If the notes don't contain enough information to answer, say so clearly.
Be concise and direct. Include specific details, names, dates, and numbers from the notes."""

# AAAK spec from mempalace/mcp_server.py AAAK_SPEC (verbatim)
AAAK_SYNTHESIS_SYSTEM = """You are a helpful AI assistant with access to a conversational memory system.
The retrieved memory notes below are in AAAK format, a compressed memory dialect.

AAAK FORMAT GUIDE:
  ENTITIES: 3-letter uppercase codes. e.g. ALC=Alice, JOR=Jordan.
  EMOTIONS: *action markers* before/during text. *warm*=joy, *fierce*=determined, *raw*=vulnerable.
  STRUCTURE: Pipe-separated fields. FAM: family | PROJ: projects.
  DATES: ISO format (2026-03-31). COUNTS: Nx = N mentions.
  IMPORTANCE: one to five star marks (1-5 scale).

Read AAAK naturally. Expand entity codes mentally, treat *markers* as emotional context.

Answer the user's question based ONLY on the retrieved memory notes below.
If the notes don't contain enough information to answer, say so clearly.
Be concise and direct. Include specific details, names, dates, and numbers from the notes."""


def synthesize_answer(client, model, question, retrieved_chunks, aaak=False):
    """Use LLM to synthesize an answer from retrieved chunks."""
    if not retrieved_chunks:
        return "I don't have any relevant information to answer this question."

    notes_text = "\n\n".join(
        f"--- Memory Note {i+1} ---\n{chunk}"
        for i, chunk in enumerate(retrieved_chunks)
    )

    system = AAAK_SYNTHESIS_SYSTEM if aaak else SYNTHESIS_SYSTEM

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": f"Retrieved memory notes:\n{notes_text}\n\nQuestion: {question}",
        },
    ]

    return llm_chat(client, model, messages, max_tokens=512, temperature=0.0)


def judge_rubric(client, model, question, answer, rubric_item):
    """Score answer against a single rubric item using BEAM official judge."""
    prompt = (
        BEAM_JUDGE_PROMPT
        .replace("<question>", question)
        .replace("<rubric_item>", rubric_item)
        .replace("<llm_response>", answer[:12000])
    )

    messages = [{"role": "user", "content": prompt}]
    response = llm_chat(client, model, messages, max_tokens=256, temperature=0.0, json_mode=True)

    try:
        parsed = json.loads(response)
        score = float(parsed.get("score", 0))
        if score >= 0.75:
            return 1.0
        elif score >= 0.25:
            return 0.5
        else:
            return 0.0
    except (json.JSONDecodeError, ValueError, TypeError):
        return 0.0


# =============================================================================
# INGEST + RETRIEVE
# =============================================================================


def ingest_conversation(conv, collection, aaak=False):
    """Ingest all user messages from a BEAM conversation into ChromaDB."""
    dialect = None
    if aaak:
        try:
            from mempalace.dialect import Dialect
            dialect = Dialect()
        except ImportError:
            print("    WARN: mempalace.dialect not available, falling back to raw")

    docs = []
    ids = []
    metadatas = []

    for i, msg in enumerate(conv["user_messages"]):
        content = msg["content"].strip()
        if not content:
            continue

        time_anchor = msg.get("time_anchor", "")

        # Prefix with time anchor for temporal context
        if time_anchor:
            doc = f"[{time_anchor}] {content}"
        else:
            doc = content

        # AAAK compression before indexing (same as longmemeval_bench.py aaak mode)
        if dialect:
            doc = dialect.compress(doc)

        docs.append(doc)
        ids.append(f"msg_{i}")
        metadatas.append({
            "time_anchor": time_anchor,
            "turn_index": i,
        })

    if not docs:
        return 0

    # ChromaDB add in batches (max 41666 per batch for default model)
    batch_size = 500
    for start in range(0, len(docs), batch_size):
        end = min(start + batch_size, len(docs))
        collection.add(
            documents=docs[start:end],
            ids=ids[start:end],
            metadatas=metadatas[start:end],
        )

    return len(docs)


def retrieve(collection, question, top_k=10, mode="raw", llm_rerank_key=None, llm_rerank_model="claude-haiku-4-5-20251001"):
    """Retrieve top-K chunks from the collection.

    Modes:
        raw    - vanilla ChromaDB semantic search (baseline)
        hybrid - semantic search + keyword overlap re-ranking (MemPalace hybrid mode)

    If llm_rerank_key is set, an additional LLM reranking pass runs after
    the initial retrieval (same as longmemeval_bench.py --llm-rerank).
    """
    try:
        count = collection.count()
        # Hybrid and rerank retrieve a larger pool then re-rank
        need_pool = mode == "hybrid" or llm_rerank_key
        n_retrieve = min(top_k * 5, count) if need_pool else min(top_k, count)

        results = collection.query(
            query_texts=[question],
            n_results=n_retrieve,
            include=["documents", "distances", "metadatas"],
        )

        docs = results["documents"][0]
        dists = results["distances"][0]

        if not docs:
            return [], []

        if mode == "hybrid":
            docs, dists = _hybrid_rerank(question, docs, dists, results["metadatas"][0], top_k)
        else:
            docs = docs[:top_k]
            dists = dists[:top_k]

        # Optional LLM rerank pass (Anthropic Claude, same as longmemeval_bench.py)
        if llm_rerank_key and docs:
            docs, dists = _llm_rerank_chunks(question, docs, dists, llm_rerank_key, llm_rerank_model)

        return docs, dists

    except Exception as e:
        print(f"    Retrieve error: {e}")
        return [], []


# Stop words for keyword extraction (matches longmemeval_bench.py)
_HYBRID_STOP_WORDS = {
    "what", "when", "where", "who", "how", "which", "did", "do", "was", "were",
    "have", "has", "had", "is", "are", "the", "a", "an", "my", "me", "i", "you",
    "your", "their", "it", "its", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "ago", "last", "that", "this", "there", "about", "get", "got",
    "give", "gave", "buy", "bought", "made", "make", "can", "could", "would",
    "should", "will", "tell", "told", "know", "many", "much", "been",
}


def _extract_keywords(text):
    """Extract meaningful keywords from text, stripping stop words."""
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [w for w in words if w not in _HYBRID_STOP_WORDS]


def _hybrid_rerank(question, docs, dists, metadatas, top_k, hybrid_weight=0.30):
    """Re-rank retrieved docs by fusing semantic distance with keyword overlap.

    This is the core of MemPalace's hybrid retrieval mode, ported from
    longmemeval_bench.py build_palace_and_retrieve_hybrid().
    """
    query_keywords = _extract_keywords(question)

    scored = []
    for doc, dist, meta in zip(docs, dists, metadatas):
        # Keyword overlap score
        if query_keywords:
            doc_lower = doc.lower()
            hits = sum(1 for kw in query_keywords if kw in doc_lower)
            overlap = hits / len(query_keywords)
        else:
            overlap = 0.0

        # Fused distance: lower = better. Reduce distance for keyword overlap.
        fused_dist = dist * (1.0 - hybrid_weight * overlap)
        scored.append((doc, fused_dist))

    # Sort by fused distance (ascending = most relevant first)
    scored.sort(key=lambda x: x[1])

    top_docs = [doc for doc, _ in scored[:top_k]]
    top_dists = [dist for _, dist in scored[:top_k]]
    return top_docs, top_dists


def _llm_rerank_chunks(question, docs, dists, api_key, model="claude-haiku-4-5-20251001"):
    """Use Claude to rerank retrieved chunks by relevance.

    Ported from longmemeval_bench.py llm_rerank(). Sends top chunks to Claude,
    asks which is most relevant, promotes the winner to position 0.
    """
    if len(docs) <= 1:
        return docs, dists

    # Format chunks for the prompt (first 500 chars each)
    chunk_blocks = []
    for i, doc in enumerate(docs):
        text = doc[:500].replace("\n", " ").strip()
        chunk_blocks.append(f"Chunk {i + 1}:\n{text}")

    chunks_text = "\n\n".join(chunk_blocks)

    prompt = (
        f"Question: {question}\n\n"
        f"Below are {len(docs)} text chunks from a conversation history. "
        f"Which single chunk is most likely to contain the answer to the question above? "
        f"Reply with ONLY a number between 1 and {len(docs)}. Nothing else.\n\n"
        f"{chunks_text}\n\n"
        f"Most relevant chunk number:"
    )

    payload = json.dumps({
        "model": model,
        "max_tokens": 8,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

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

    for _attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read())
            raw = result["content"][0]["text"].strip()
            m = re.search(r"\b(\d+)\b", raw)
            if m:
                pick = int(m.group(1)) - 1  # 1-indexed to 0-indexed
                if 0 <= pick < len(docs):
                    # Promote the picked chunk to position 0
                    reranked_docs = [docs[pick]] + [d for i, d in enumerate(docs) if i != pick]
                    reranked_dists = [dists[pick]] + [d for i, d in enumerate(dists) if i != pick]
                    return reranked_docs, reranked_dists
            return docs, dists
        except Exception as e:
            if _attempt < 2:
                time.sleep(2 ** (_attempt + 1))
            else:
                print(f"    LLM rerank failed: {e}")
                return docs, dists

    return docs, dists


# =============================================================================
# MAIN EVALUATION
# =============================================================================


def eval_conversation(conv, client, model, top_k=10, mode="raw", aaak=False, aaak_spec=False, llm_rerank_key=None, llm_rerank_model="claude-haiku-4-5-20251001", debug_file=None):
    """Evaluate a single conversation. Returns (checks_passed, checks_total, ability_scores)."""
    conv_id = conv["id"]
    category = conv["category"]
    num_msgs = len(conv["user_messages"])
    num_qs = len(conv["questions"])

    print(f"\n{'=' * 70}")
    print(f"BEAM 100K  - Conv {conv_id} [{category}]: {num_msgs} msgs, {num_qs} questions")
    print(f"{'=' * 70}")

    # --- Ingest ---
    ingest_start = time.time()
    collection = _fresh_collection()
    ingested = ingest_conversation(conv, collection, aaak=aaak)
    ingest_secs = time.time() - ingest_start
    print(f"  Ingested {ingested} messages in {ingest_secs:.1f}s")

    # --- Query + Synthesize + Judge ---
    ability_scores = defaultdict(lambda: [0, 0])  # {ability: [passed, total]}
    total_passed = 0
    total_checks = 0

    for qi, q in enumerate(conv["questions"]):
        question = q["question"]
        ability = q["ability"]
        rubric = q.get("rubric", [])
        ref_answer = q.get("reference_answer", "")

        if isinstance(rubric, str):
            rubric = [rubric]

        # Retrieve
        q_start = time.time()
        chunks, distances = retrieve(collection, question, top_k=top_k, mode=mode, llm_rerank_key=llm_rerank_key, llm_rerank_model=llm_rerank_model)

        # Synthesize
        answer = synthesize_answer(client, model, question, chunks, aaak=(aaak and aaak_spec))
        q_secs = time.time() - q_start

        print(f"\n  [{ability}] Q{qi+1}: {question[:80]}")
        print(f"  A ({q_secs:.1f}s): {answer[:200]}")

        # Judge each rubric item
        q_scores = []
        for ri, rubric_item in enumerate(rubric):
            score = judge_rubric(client, model, question, answer, rubric_item)
            q_scores.append(score)

            ability_scores[ability][1] += 1
            total_checks += 1

            if score >= 0.5:
                ability_scores[ability][0] += 1
                total_passed += 1

            label = "FULL" if score >= 1.0 else ("PART" if score >= 0.5 else "FAIL")
            print(f"    [{label}] R{ri+1}: {score:.1f} ({rubric_item[:70]})")

        if not rubric:
            # BEAM 100K questions all have rubric items (verified 0/400 lack one),
            # so this branch should never fire. If a future dataset variant ships
            # questions without rubrics, skip them rather than guess at correctness
            # from answer length, which would inflate scores on hallucinated answers.
            print(f"    [SKIP] Q{qi+1}: question has no rubric, cannot score")

        beam_score = sum(q_scores) / len(q_scores) if q_scores else 0.0
        print(f"    -> Q{qi+1} BEAM score: {beam_score:.2f}")

        # JSONL debug
        if debug_file:
            debug_entry = {
                "conv_id": conv_id,
                "category": category,
                "q_index": qi,
                "ability": ability,
                "question": question,
                "reference_answer": ref_answer,
                "system_answer": answer,
                "num_chunks_retrieved": len(chunks),
                "top_distance": distances[0] if distances else None,
                "rubric_scores": [
                    {"item": r, "score": s}
                    for r, s in zip(rubric, q_scores)
                ],
                "beam_score": beam_score,
                "query_time_s": q_secs,
            }
            debug_file.write(json.dumps(debug_entry) + "\n")
            debug_file.flush()

    return total_passed, total_checks, dict(ability_scores)


def print_results(header, total_passed, total_checks, ability_scores):
    """Print final results table."""
    print(f"\n{'=' * 70}")
    print(header)
    print(f"{'=' * 70}")

    pass_rate = total_passed / total_checks * 100 if total_checks > 0 else 0
    print(f"  Rubric checks: {total_passed}/{total_checks} passed (>= 0.5)")
    print(f"  Pass rate:     {pass_rate:.1f}%")
    print(f"\n  {'Ability':<30} {'Passed':>8} {'Rate':>8}")
    print(f"  {'-' * 48}")

    for ability in sorted(ability_scores.keys()):
        p, t = ability_scores[ability]
        rate = p / t * 100 if t > 0 else 0
        print(f"  {ability:<30} {p:>4}/{t:<4} {rate:>6.0f}%")


def main():
    parser = argparse.ArgumentParser(description="BEAM 100K benchmark for MemPalace")
    parser.add_argument("dataset", nargs="?", default=None,
                        help="Path to beam-100k.json (auto-downloads from HuggingFace if omitted)")
    parser.add_argument("--full", action="store_true", help="Run all 20 conversations")
    parser.add_argument("--conv", type=int, default=0, help="Conversation index for single run")
    parser.add_argument("--top-k", type=int, default=10, help="Number of chunks to retrieve")
    parser.add_argument("--mode", type=str, default="raw", choices=["raw", "hybrid", "aaak"],
                        help="Retrieval mode: raw (vanilla ChromaDB), hybrid (keyword re-ranking), or aaak (AAAK compression before indexing)")
    parser.add_argument("--aaak-spec", action="store_true",
                        help="Include AAAK dialect spec in synthesis prompt (only relevant with --mode aaak)")
    parser.add_argument("--llm-rerank", action="store_true",
                        help="Enable LLM reranking of retrieved chunks (requires Anthropic API key)")
    parser.add_argument("--llm-model", type=str, default="claude-haiku-4-5-20251001",
                        help="Claude model for LLM reranking (default: haiku)")
    parser.add_argument("--api-key", type=str, default=None,
                        help="Anthropic API key for --llm-rerank (or set ANTHROPIC_KEY env var)")
    parser.add_argument("--embed-model", type=str, default="default", help="Embedding model")
    parser.add_argument("--debug-dir", type=str, default=None, help="JSONL debug output directory")
    args = parser.parse_args()

    # Load dataset (auto-download from HuggingFace if not provided)
    print("Loading BEAM 100K dataset...")
    dataset = ensure_beam_dataset(args.dataset)

    print(f"  Split: {dataset['split']}, Conversations: {dataset['num_conversations']}, "
          f"Questions: {dataset['total_questions']}")

    # Setup embedding model
    global _bench_embed_fn
    _bench_embed_fn = _make_embed_fn(args.embed_model)
    print(f"  Embedding: {args.embed_model}")
    print(f"  Mode: {args.mode}")
    print(f"  Top-K: {args.top_k}")

    # LLM rerank setup
    rerank_key = None
    rerank_model = args.llm_model
    if args.llm_rerank:
        rerank_key = args.api_key or os.environ.get("ANTHROPIC_KEY") or os.environ.get("ANTHROPIC_API_KEY")
        if not rerank_key:
            print("ERROR: --llm-rerank requires an Anthropic API key.")
            print("  Set ANTHROPIC_KEY env var or pass --api-key")
            sys.exit(1)
        print(f"  LLM rerank: {rerank_model}")

    # Setup LLM
    client, model = _create_llm_client()

    # Setup debug output
    debug_dir = args.debug_dir or str(Path(__file__).parent / "results")
    os.makedirs(debug_dir, exist_ok=True)
    run_ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    debug_path = os.path.join(debug_dir, f"beam-mempalace-{run_ts}.jsonl")
    debug_file = open(debug_path, "w")
    print(f"  Debug log: {debug_path}")

    # Run
    full_start = time.time()

    if args.full:
        conversations = dataset["conversations"]
    else:
        conversations = [dataset["conversations"][args.conv]]
        print(f"  Running single conversation: index {args.conv}")

    grand_passed = 0
    grand_checks = 0
    grand_abilities = defaultdict(lambda: [0, 0])

    for conv in conversations:
        passed, checks, abilities = eval_conversation(
            conv, client, model, top_k=args.top_k, mode=args.mode,
            aaak=(args.mode == "aaak"), aaak_spec=args.aaak_spec,
            llm_rerank_key=rerank_key, llm_rerank_model=rerank_model,
            debug_file=debug_file
        )
        grand_passed += passed
        grand_checks += checks
        for ability, (p, t) in abilities.items():
            grand_abilities[ability][0] += p
            grand_abilities[ability][1] += t

        # Running total
        if args.full:
            rate = grand_passed / grand_checks * 100 if grand_checks > 0 else 0
            print(f"\n  Running total: {grand_passed}/{grand_checks} ({rate:.1f}%)")

    total_secs = time.time() - full_start
    debug_file.close()

    mode = "FULL (20 convs)" if args.full else f"SINGLE (conv {args.conv})"
    header = (
        f"BEAM 100K  - MemPalace {mode}\n"
        f"  Methodology: BEAM official rubric judge (3-tier: 1.0/0.5/0.0)\n"
        f"  Retrieval: ChromaDB top-{args.top_k}, Mode: {args.mode}"
        f"{f', LLM rerank: {rerank_model}' if rerank_key else ''}"
        f", Embedding: {args.embed_model}\n"
        f"  Synthesis: {model}\n"
        f"  Time: {total_secs/60:.0f}m {total_secs%60:.0f}s"
    )
    print_results(header, grand_passed, grand_checks, dict(grand_abilities))
    print(f"\n  Debug log: {debug_path}")


if __name__ == "__main__":
    main()
