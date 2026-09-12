#!/usr/bin/env python3
"""
MemPal × MemBench Benchmark
============================

MemBench (ACL 2025): https://aclanthology.org/2025.findings-acl.989/
Data: https://github.com/import-myself/Membench

MemBench tests memory across multi-turn conversations in multiple categories:
  - highlevel: inferences requiring aggregation across turns ("what kind of X do I prefer?")
  - lowlevel: single-turn fact recall ("what X did I mention?")
  - knowledge_update: facts that change over time
  - comparative: comparing two items mentioned across turns
  - conditional: conditional reasoning over remembered facts
  - noisy: distractors / irrelevant info mixed in
  - aggregative: combining info from multiple turns
  - RecMultiSession: recommendations across multiple topic sessions

Each item has:
  - message_list[0]: list of turns [{user, assistant, time, place}]
  - QA: {question, answer, choices (A/B/C/D), ground_truth, target_step_id}

We measure RETRIEVAL RECALL: is the answer-relevant turn in the top-K retrieved?
We also score ACCURACY: does the top-retrieved turn's context match ground_truth?

Usage:
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent --category highlevel
    python benchmarks/membench_bench.py /tmp/membench/MemData/FirstAgent --limit 50
"""

import sys
import json
import re
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import chromadb

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Shared ephemeral ChromaDB client ──────────────────────────────────────────
_bench_client = chromadb.EphemeralClient()


def _fresh_collection(name="membench_drawers"):
    try:
        _bench_client.delete_collection(name)
    except Exception:
        pass
    return _bench_client.create_collection(name)


# ── Stop words (same as locomo_bench) ─────────────────────────────────────────
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
    "would",
    "could",
    "should",
    "might",
    "can",
    "will",
    "shall",
    "kind",
    "type",
    "like",
    "prefer",
    "enjoy",
    "think",
    "feel",
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
    "I",
    "It",
    "Its",
    "This",
    "That",
    "These",
    "Those",
}


def _kw(text):
    words = re.findall(r"\b[a-z]{3,}\b", text.lower())
    return [w for w in words if w not in STOP_WORDS]


def _kw_overlap(query_kws, doc_text):
    if not query_kws:
        return 0.0
    doc_lower = doc_text.lower()
    hits = sum(1 for kw in query_kws if kw in doc_lower)
    return hits / len(query_kws)


def _person_names(text):
    words = re.findall(r"\b[A-Z][a-z]{2,15}\b", text)
    return list(set(w for w in words if w not in NOT_NAMES))


# ── MemBench data loading ─────────────────────────────────────────────────────

CATEGORY_FILES = {
    "simple": "simple.json",
    "highlevel": "highlevel.json",
    "knowledge_update": "knowledge_update.json",
    "comparative": "comparative.json",
    "conditional": "conditional.json",
    "noisy": "noisy.json",
    "aggregative": "aggregative.json",
    "highlevel_rec": "highlevel_rec.json",
    "lowlevel_rec": "lowlevel_rec.json",
    "RecMultiSession": "RecMultiSession.json",
    "post_processing": "post_processing.json",
}


def load_membench(data_dir: str, categories=None, topic="movie", limit=0):
    """
    Load MemBench questions from the FirstAgent directory.

    Returns list of dicts:
        {category, topic, tid, turns, question, choices, ground_truth, target_step_ids}
    """
    data_dir = Path(data_dir)
    if categories is None:
        categories = list(CATEGORY_FILES.keys())

    items = []
    for cat in categories:
        fname = CATEGORY_FILES.get(cat)
        if not fname:
            continue
        fpath = data_dir / fname
        if not fpath.exists():
            continue
        with open(fpath, encoding="utf-8") as f:
            raw = json.load(f)

        # Files have two formats:
        #   topic-keyed: {"movie": [...], "food": [...], "book": [...]}
        #   role-keyed:  {"roles": [...], "events": [...]}
        # For topic-keyed, filter by topic arg. For role-keyed, use key as the "topic".
        for t, topic_items in raw.items():
            if topic and t not in (topic, "roles", "events"):
                continue
            for item in topic_items:
                turns = item.get("message_list", [])  # pass full message_list (all sessions)
                qa = item.get("QA", {})
                if not turns or not qa:
                    continue
                items.append(
                    {
                        "category": cat,
                        "topic": t,
                        "tid": item.get("tid", 0),
                        "turns": turns,
                        "question": qa.get("question", ""),
                        "choices": qa.get("choices", {}),
                        "ground_truth": qa.get("ground_truth", ""),
                        "answer_text": qa.get("answer", ""),
                        "target_step_ids": qa.get("target_step_id", []),
                    }
                )

    if limit > 0:
        items = items[:limit]
    return items


# ── Indexing ──────────────────────────────────────────────────────────────────


def _turn_text(turn: dict) -> str:
    """Extract text from a turn regardless of field naming convention."""
    user = turn.get("user") or turn.get("user_message", "")
    asst = turn.get("assistant") or turn.get("assistant_message", "")
    time = turn.get("time", "")
    text = f"[User] {user} [Assistant] {asst}"
    if time:
        text = f"[{time}] " + text
    return text


def index_turns(collection, message_list, item_key: str):
    """
    Index all turns from all sessions into the collection.

    message_list can be:
      - Flat list of turns: [turn, turn, ...]  (highlevel.json format)
      - List of sessions: [[turn, turn], [turn, turn], ...]  (simple.json format)

    Each turn keyed by 'sid' if present, else by positional index.
    Returns number of turns indexed.
    """
    docs, ids, metas = [], [], []

    # Normalize: flat list of dicts → wrap as one session
    if message_list and isinstance(message_list[0], dict):
        sessions = [message_list]
    else:
        sessions = message_list

    global_idx = 0
    for s_idx, session in enumerate(sessions):
        if not isinstance(session, list):
            continue
        for t_idx, turn in enumerate(session):
            if not isinstance(turn, dict):
                continue
            sid = turn.get("sid", turn.get("mid"))
            doc_id = f"{item_key}_g{global_idx}"
            text = _turn_text(turn)
            docs.append(text)
            ids.append(doc_id)
            metas.append(
                {
                    "item_key": item_key,
                    "sid": int(sid) if isinstance(sid, (int, float)) else global_idx,
                    "s_idx": s_idx,
                    "t_idx": t_idx,
                    "global_idx": global_idx,
                }
            )
            global_idx += 1

    if docs:
        collection.add(documents=docs, ids=ids, metadatas=metas)
    return len(docs)


# ── Scoring ───────────────────────────────────────────────────────────────────


def run_membench(
    data_dir, categories=None, topic="movie", top_k=5, limit=0, mode="raw", out_file=None
):
    """Run MemBench retrieval evaluation."""

    items = load_membench(data_dir, categories=categories, topic=topic, limit=limit)
    if not items:
        print(f"No items found in {data_dir}")
        return

    print(f"\n{'=' * 58}")
    print("  MemPal × MemBench")
    print(f"{'=' * 58}")
    print(f"  Data dir:    {data_dir}")
    print(f"  Categories:  {', '.join(categories or ['all'])}")
    print(f"  Topic:       {topic or 'all'}")
    print(f"  Items:       {len(items)}")
    print(f"  Top-k:       {top_k}")
    print(f"  Mode:        {mode}")
    print(f"{'-' * 58}\n")

    results = []
    by_cat = defaultdict(lambda: {"hit_at_k": 0, "total": 0})
    total_hit = 0

    for idx, item in enumerate(items, 1):
        item_key = f"{item['category']}_{item['topic']}_{idx}"  # idx ensures unique key
        collection = _fresh_collection()

        # Index all turns from all sessions
        n_indexed = index_turns(collection, item["turns"], item_key)
        if n_indexed < 1:
            continue

        question = item["question"]
        n_retrieve = min(top_k * 3 if mode == "hybrid" else top_k, n_indexed)
        if n_retrieve < 1:
            continue

        # Retrieve
        res = collection.query(
            query_texts=[question],
            n_results=n_retrieve,
            include=["distances", "metadatas", "documents"],
        )
        retrieved_sids = [m["sid"] for m in res["metadatas"][0]]
        retrieved_global = [m["global_idx"] for m in res["metadatas"][0]]
        retrieved_docs = res["documents"][0]
        raw_distances = res["distances"][0]

        # Hybrid re-scoring: predicate keywords (person names excluded)
        if mode == "hybrid":
            names = _person_names(question)
            name_words = {n.lower() for n in names}
            all_kws = _kw(question)
            predicate_kws = [w for w in all_kws if w not in name_words]

            scored = []
            for dist, sid, gidx, doc in zip(
                raw_distances, retrieved_sids, retrieved_global, retrieved_docs
            ):
                pred_overlap = _kw_overlap(predicate_kws, doc)
                fused = dist * (1.0 - 0.50 * pred_overlap)
                scored.append((fused, sid, gidx, doc))
            scored.sort(key=lambda x: x[0])
            retrieved_sids = [x[1] for x in scored[:top_k]]
            retrieved_global = [x[2] for x in scored[:top_k]]
        else:
            retrieved_sids = retrieved_sids[:top_k]
            retrieved_global = retrieved_global[:top_k]

        # Check if any target turn is retrieved.
        # target_step_id format varies: [sid, ?] or [global_idx, ?]
        # Try matching against both sid and global_idx.
        target_sids = set()
        for step in item["target_step_ids"]:
            if isinstance(step, list) and len(step) >= 1:
                target_sids.add(step[0])  # first element is the turn sid/global index

        hit = bool(target_sids & set(retrieved_sids)) or bool(target_sids & set(retrieved_global))
        if hit:
            total_hit += 1
            by_cat[item["category"]]["hit_at_k"] += 1
        by_cat[item["category"]]["total"] += 1

        results.append(
            {
                "category": item["category"],
                "topic": item["topic"],
                "tid": item["tid"],
                "question": question,
                "ground_truth": item["ground_truth"],
                "answer_text": item["answer_text"],
                "target_sids": list(target_sids),
                "retrieved_sids": retrieved_sids,
                "retrieved_global": retrieved_global,
                "hit_at_k": hit,
            }
        )

        if idx % 50 == 0:
            running_pct = total_hit / idx * 100
            print(f"  [{idx:4}/{len(items)}]  running R@{top_k}: {running_pct:.1f}%")

    # Final results
    overall = total_hit / len(items) * 100 if items else 0
    print(f"\n{'=' * 58}")
    print(f"  RESULTS — MemPal on MemBench ({mode} mode, top-{top_k})")
    print(f"{'=' * 58}")
    print(f"\n  Overall R@{top_k}: {overall:.1f}%  ({total_hit}/{len(items)})\n")
    print("  By category:")
    for cat, v in sorted(by_cat.items()):
        pct = v["hit_at_k"] / v["total"] * 100 if v["total"] else 0
        print(f"    {cat:20} {pct:5.1f}%  ({v['hit_at_k']}/{v['total']})")
    print(f"\n{'=' * 58}\n")

    if out_file:
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"  Results saved to: {out_file}")

    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MemPal × MemBench Benchmark")
    parser.add_argument("data_dir", help="Path to MemBench FirstAgent directory")
    parser.add_argument(
        "--category",
        default=None,
        choices=list(CATEGORY_FILES.keys()),
        help="Run a single category (default: all)",
    )
    parser.add_argument(
        "--topic", default="movie", help="Topic filter: movie, food, book (default: movie)"
    )
    parser.add_argument("--top-k", type=int, default=5, help="Retrieval top-k (default: 5)")
    parser.add_argument("--limit", type=int, default=0, help="Limit items (0 = all)")
    parser.add_argument(
        "--mode",
        choices=["raw", "hybrid"],
        default="hybrid",
        help="Retrieval mode (default: hybrid)",
    )
    parser.add_argument("--out", default=None, help="Output JSON file (default: auto-named)")
    args = parser.parse_args()

    if not args.out:
        cat_tag = f"_{args.category}" if args.category else "_all"
        args.out = (
            f"benchmarks/results_membench_{args.mode}{cat_tag}_{args.topic}"
            f"_top{args.top_k}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        )

    cats = [args.category] if args.category else None
    run_membench(
        args.data_dir,
        categories=cats,
        topic=args.topic,
        top_k=args.top_k,
        limit=args.limit,
        mode=args.mode,
        out_file=args.out,
    )
