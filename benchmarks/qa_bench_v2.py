#!/usr/bin/env python3
"""
qa_bench_v2.py — End-to-end QA benchmark on LongMemEval-S using MemPalace's
own retrieval code paths (raw / aaak / rooms), with the LongMemEval-published
calibrated judge protocol.

Context: https://github.com/MemPalace/mempalace/issues/39

What this runner is, concretely:
  1. Retrieval imports build_palace_and_retrieve_{raw,aaak,rooms} from the
     sibling longmemeval_bench module — not a parallel implementation — so
     the retrieval code path cannot drift between this harness and
     `longmemeval_bench.py --mode {raw,aaak,rooms}` runs. aaak mode actually
     calls mempalace.dialect.Dialect().compress(); rooms mode runs
     detect_room_for_text + room-boosted reranking.
  2. Reader defaults to gpt-4o-mini-2024-07-18 for cross-system parity with
     Mem0/Zep/Mastra published numbers. Override via --reader-model.
  3. Judge defaults to gpt-4o-2024-08-06 (the LongMemEval paper's calibrated
     judge, ~97% agreement with human gold). Override via --judge-model.

Reader and judge prompts are verbatim from LongMemEval (Wu et al. 2024)
`src/evaluation/evaluate_qa.py` and `src/generation/run_generation.py`.

Usage:
    python benchmarks/qa_bench_v2.py data/longmemeval_s_cleaned.json --mode raw
    python benchmarks/qa_bench_v2.py data/longmemeval_s_cleaned.json --mode aaak --limit 50
    python benchmarks/qa_bench_v2.py data/longmemeval_s_cleaned.json --mode rooms

Requires OPENAI_API_KEY to be set in the environment.
"""
import os
import sys
import json
import time
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime

# Import the sibling harness so the retrieval code path is shared verbatim.
sys.path.insert(0, str(Path(__file__).parent))

import longmemeval_bench as lmb  # noqa: E402

from openai import OpenAI  # noqa: E402

# =============================================================================
# CONFIG
# =============================================================================
DEFAULT_READER_MODEL = "gpt-4o-mini-2024-07-18"
DEFAULT_JUDGE_MODEL = "gpt-4o-2024-08-06"
TOP_K = 5

if not os.environ.get("OPENAI_API_KEY"):
    print(
        "ERROR: OPENAI_API_KEY is not set. Export it in your environment before running.",
        file=sys.stderr,
    )
    sys.exit(1)

openai_client = OpenAI()

# =============================================================================
# JUDGE PROMPTS — verbatim from LongMemEval evaluate_qa.py
# =============================================================================

def get_anscheck_prompt(qtype, question, answer, response, abstention=False):
    if abstention:
        return (
            "I will give you an unanswerable question, an explanation, and a response from a model. "
            "Please answer yes if the model correctly identifies the question as unanswerable. "
            "The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\n"
            f"Question: {question}\n\nExplanation: {answer}\n\nModel Response: {response}\n\n"
            "Does the model correctly identify the question as unanswerable? Answer yes or no only."
        )
    base = (
        "I will give you a question, a correct answer, and a response from a model. "
        "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
        "If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. "
        "If the response only contains a subset of the information required by the answer, answer no."
    )
    if qtype == "temporal-reasoning":
        base += (
            " In addition, do not penalize off-by-one errors for the number of days. "
            "If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors "
            "(e.g., predicting 19 days when the answer is 18), the model's response is still correct."
        )
    elif qtype == "knowledge-update":
        base += (
            " If the response contains some previous information along with an updated answer, "
            "the response should be considered as correct as long as the updated answer is the required answer."
        )
    elif qtype == "single-session-preference":
        return (
            "I will give you a question, a rubric for desired personalized response, and a response from a model. "
            "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
            "The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\n"
            f"Question: {question}\n\nRubric: {answer}\n\nModel Response: {response}\n\n"
            "Is the model response correct? Answer yes or no only."
        )
    return (
        f"{base}\n\nQuestion: {question}\n\nCorrect Answer: {answer}\n\n"
        f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only."
    )


# =============================================================================
# READER PROMPT — verbatim from run_generation.py
# =============================================================================

def format_history(retrieved, sessions_data):
    """Build chronologically-sorted history string from retrieved (orig_idx, date) tuples."""
    items = sorted(retrieved, key=lambda x: x[1])  # sort by date
    history = ""
    for i, (idx, date) in enumerate(items):
        history += f"\n### Session {i+1}:\nSession Date: {date}\nSession Content:\n"
        for turn in sessions_data[idx]:
            role = turn.get("role", "user")
            content = turn.get("content", "").strip()
            tag = "user" if role == "user" else "A"
            history += f"\n{tag}: {content}\n"
    return history


def build_reader_prompt(history, question, question_date):
    return (
        "I will give you several history chats between you and a user. "
        "Please answer the question based on the relevant chat history.\n\n\n"
        f"History Chats:\n\n{history}\n\n"
        f"Current Date: {question_date}\nQuestion: {question}\nAnswer:"
    )


# =============================================================================
# RETRIEVAL — DELEGATES TO MEMPALACE'S OWN longmemeval_bench
# =============================================================================

def _retrieval_fn(mode):
    if mode == "raw":
        return lmb.build_palace_and_retrieve
    if mode == "aaak":
        return lmb.build_palace_and_retrieve_aaak
    if mode == "rooms":
        return lmb.build_palace_and_retrieve_rooms
    raise ValueError(f"Unknown mode: {mode}")


def retrieve(entry, mode, top_k=TOP_K):
    """Call the sibling module's real retrieval function and map top-k corpus
    indices back to original session indices + dates.

    Returns:
        retrieved: list of (original_session_index, date) for top-k
        sessions_data: full haystack_sessions list (for reader prompt)
    """
    fn = _retrieval_fn(mode)
    ranked, corpus, corpus_ids, corpus_ts = fn(entry, granularity="session", n_results=top_k)

    sess_id_to_orig_idx = {sid: i for i, sid in enumerate(entry["haystack_session_ids"])}

    retrieved = []
    for r in ranked[:top_k]:
        orig_sess_id = corpus_ids[r]
        orig_idx = sess_id_to_orig_idx[orig_sess_id]
        date = corpus_ts[r]
        retrieved.append((orig_idx, date))

    return retrieved, entry["haystack_sessions"]


# =============================================================================
# LLM CALLS
# =============================================================================

# Pricing per million tokens (input, output) — for cost tracking only.
PRICING = {
    "gpt-4o-mini-2024-07-18": (0.15, 0.60),
    "gpt-4o-2024-08-06": (2.50, 10.00),
}

_total_cost = 0.0
_total_in = 0
_total_out = 0


def call_openai(model, prompt, max_tokens, retries=3):
    global _total_cost, _total_in, _total_out
    for attempt in range(retries):
        try:
            r = openai_client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0,
            )
            in_tok = r.usage.prompt_tokens
            out_tok = r.usage.completion_tokens
            p_in, p_out = PRICING.get(model, (0, 0))
            cost = (in_tok * p_in + out_tok * p_out) / 1_000_000
            _total_cost += cost
            _total_in += in_tok
            _total_out += out_tok
            return r.choices[0].message.content or ""
        except Exception as e:
            if attempt == retries - 1:
                return f"[ERROR: {type(e).__name__}: {e}]"
            time.sleep(2 ** attempt)
    return ""


# =============================================================================
# MAIN
# =============================================================================

def run(dataset_path, mode, limit=None, output=None,
        reader_model=DEFAULT_READER_MODEL, judge_model=DEFAULT_JUDGE_MODEL):
    data = json.load(open(dataset_path))
    if limit:
        data = data[:limit]

    fn_name = "build_palace_and_retrieve" if mode == "raw" else f"build_palace_and_retrieve_{mode}"

    print(f"\n{'='*70}")
    print(f"  QA Benchmark v2 — mode={mode}")
    print(f"  N questions: {len(data)}")
    print(f"  Reader: {reader_model}")
    print(f"  Judge:  {judge_model}")
    print(f"  Top-K:  {TOP_K}")
    print(f"  Retrieval: longmemeval_bench.{fn_name}()")
    print(f"{'='*70}\n")

    results = []
    t0 = time.time()
    correct = 0

    for i, entry in enumerate(data, 1):
        qid = entry["question_id"]
        qtype = entry["question_type"]
        question = entry["question"]
        question_date = entry.get("question_date", "")
        ground_truth = entry["answer"]
        is_abstention = "_abs" in qid

        try:
            retrieved, sessions_data = retrieve(entry, mode, top_k=TOP_K)
        except Exception as e:
            retrieved = []
            sessions_data = entry["haystack_sessions"]
            print(f"  [{i}] retrieve error: {e}")

        if not retrieved:
            label = False
            hyp = "[no retrieval results]"
            verdict = "no"
        else:
            history = format_history(retrieved, sessions_data)
            reader_prompt = build_reader_prompt(history, question, question_date)
            hyp = call_openai(reader_model, reader_prompt, max_tokens=200)

            judge_prompt = get_anscheck_prompt(qtype, question, ground_truth, hyp, abstention=is_abstention)
            verdict = call_openai(judge_model, judge_prompt, max_tokens=10)
            label = "yes" in verdict.strip().lower()

        if label:
            correct += 1

        results.append({
            "qid": qid, "qtype": qtype, "question": question,
            "ground_truth": ground_truth, "hypothesis": hyp,
            "verdict": verdict, "label": label,
            "retrieved_indices": [r[0] for r in retrieved],
        })

        elapsed = time.time() - t0
        rate = i / elapsed if elapsed > 0 else 0
        eta_min = (len(data) - i) / rate / 60 if rate > 0 else 0
        marker = "HIT" if label else "miss"
        print(f"  [{i:4}/{len(data)}] {qid[:8]} {qtype[:24]:24}  {marker}  acc={correct/i:.3f} cost=${_total_cost:.3f} eta={eta_min:.0f}m")

    # Aggregate
    total_time = time.time() - t0
    overall = correct / len(results) if results else 0.0
    per_type = defaultdict(list)
    for r in results:
        per_type[r["qtype"]].append(r["label"])

    print(f"\n{'='*70}\n  RESULTS — mode={mode}\n{'='*70}")
    print(f"  Time: {total_time:.0f}s ({total_time/len(results):.1f}s/question)" if results else "  No results.")
    print(f"  Cost: ${_total_cost:.3f}  (in={_total_in} out={_total_out} tokens)")
    print(f"\n  OVERALL QA ACCURACY: {overall:.4f}  ({correct}/{len(results)})")
    print(f"\n  PER-TYPE BREAKDOWN:")
    for qt in sorted(per_type.keys()):
        labels = per_type[qt]
        print(f"    {qt:32} {sum(labels)/len(labels):.4f}  ({sum(labels)}/{len(labels)})")

    if output:
        out_path = output
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        out_path = f"qa_v2_{mode}_{ts}.jsonl"
    with open(out_path, "w") as f:
        f.write(json.dumps({"meta": {
            "mode": mode, "reader": reader_model, "judge": judge_model, "top_k": TOP_K,
            "n": len(results), "overall_accuracy": overall, "time_sec": total_time,
            "cost_usd": round(_total_cost, 4),
            "tokens_in": _total_in, "tokens_out": _total_out,
            "per_type": {k: sum(v)/len(v) for k, v in per_type.items()},
        }}) + "\n")
        for r in results:
            f.write(json.dumps(r) + "\n")
    print(f"\n  Saved: {out_path}\n")
    return overall, {k: sum(v)/len(v) for k, v in per_type.items()}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("dataset", help="Path to longmemeval_s_cleaned.json")
    p.add_argument("--mode", choices=["raw", "aaak", "rooms"], required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--output", default=None)
    p.add_argument("--reader-model", default=DEFAULT_READER_MODEL)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    args = p.parse_args()
    run(args.dataset, args.mode, limit=args.limit, output=args.output,
        reader_model=args.reader_model, judge_model=args.judge_model)
