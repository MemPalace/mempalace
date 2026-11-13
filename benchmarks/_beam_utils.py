"""
Shared BEAM 100K dataset utilities.

Used by both `beam_100k_bench.py` (the benchmark runner) and `convert_beam.py`
(the standalone CLI converter). Centralizes parquet download + JSON conversion
so parsing logic only lives in one place.

Dataset: https://huggingface.co/datasets/Mohammadta/BEAM
Paper: Tavakoli et al., "BEAM: Benchmark for Evaluating AI Memory" (2024)
"""

import ast
import json
import os
import re
import ssl
import sys
import urllib.request


# HuggingFace dataset URL
HF_BEAM_URL = "https://huggingface.co/datasets/Mohammadta/BEAM/resolve/main/data/train-00000-of-00001.parquet"


def _unverified_ssl_context():
    """Return an SSL context that skips certificate verification.

    Scoped to BEAM downloads only. Avoids polluting the process-wide SSL
    context for other HTTPS calls (LLM APIs, telemetry, etc.).
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def download_beam_parquet(cache_dir):
    """Download BEAM parquet from HuggingFace if not cached. Returns the path."""
    parquet_path = os.path.join(cache_dir, "beam-100k.parquet")
    if os.path.exists(parquet_path):
        return parquet_path

    os.makedirs(cache_dir, exist_ok=True)
    print("  Downloading BEAM 100K from HuggingFace...")
    print(f"  URL: {HF_BEAM_URL}")
    try:
        ctx = _unverified_ssl_context()
        with urllib.request.urlopen(HF_BEAM_URL, context=ctx, timeout=120) as resp:
            with open(parquet_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
        size_mb = os.path.getsize(parquet_path) / (1024 * 1024)
        print(f"  Downloaded: {size_mb:.1f} MB")
    except Exception as e:
        print(f"  Download failed: {e}")
        print(f"  You can manually download from: {HF_BEAM_URL}")
        sys.exit(1)
    return parquet_path


def convert_parquet_to_json(parquet_path, json_path=None):
    """Convert BEAM parquet to the JSON format the benchmark expects.

    If json_path is provided, the result is also written to disk.
    Returns the parsed dict regardless.
    """
    try:
        import pandas as pd
    except ImportError:
        print("ERROR: pandas and pyarrow are required for BEAM dataset conversion.")
        print("  pip install pandas pyarrow")
        sys.exit(1)

    print("  Converting parquet to JSON...")
    df = pd.read_parquet(parquet_path)

    conversations = []
    total_questions = 0

    for _, row in df.iterrows():
        conv_id = str(row.get("conversation_id", row.name))

        # Parse chat turns (parquet stores them as a Python literal string)
        chat_raw = row.get("chat", "[]")
        if isinstance(chat_raw, str):
            try:
                chat = ast.literal_eval(chat_raw)
            except (ValueError, SyntaxError):
                chat = json.loads(chat_raw)
        else:
            chat = chat_raw

        user_messages = []
        for turn in chat:
            if isinstance(turn, dict):
                role = turn.get("role", "")
                content = turn.get("content", "")
                time_anchor = turn.get("time_anchor", "")
            elif isinstance(turn, (list, tuple)) and len(turn) >= 2:
                role, content = turn[0], turn[1]
                time_anchor = turn[2] if len(turn) > 2 else ""
            else:
                continue

            if role == "user" and content.strip():
                if time_anchor:
                    time_anchor = re.sub(r"(->)+$", "", time_anchor).strip()
                user_messages.append({
                    "role": "user",
                    "content": content.strip(),
                    "time_anchor": time_anchor,
                })

        # Parse probing questions
        questions_raw = row.get("probing_questions", "[]")
        if isinstance(questions_raw, str):
            try:
                questions_parsed = ast.literal_eval(questions_raw)
            except (ValueError, SyntaxError):
                questions_parsed = json.loads(questions_raw)
        else:
            questions_parsed = questions_raw

        questions = []
        for q in questions_parsed:
            if isinstance(q, dict):
                rubric = q.get("rubric", [])
                if isinstance(rubric, str):
                    rubric = [rubric]
                questions.append({
                    "ability": q.get("ability", "unknown"),
                    "question": q.get("question", ""),
                    "reference_answer": q.get("reference_answer", ""),
                    "rubric": rubric,
                })

        total_questions += len(questions)
        conversations.append({
            "id": conv_id,
            "category": str(row.get("category", "unknown")),
            "title": str(row.get("title", "")),
            "user_messages": user_messages,
            "total_turns": len(user_messages),
            "questions": questions,
        })

    output = {
        "split": "100K",
        "num_conversations": len(conversations),
        "total_questions": total_questions,
        "conversations": conversations,
    }

    if json_path:
        with open(json_path, "w") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)

    avg_msgs = sum(len(c["user_messages"]) for c in conversations) / max(len(conversations), 1)
    print(f"  Converted: {len(conversations)} conversations, {total_questions} questions, {avg_msgs:.0f} avg msgs/conv")
    return output


def ensure_beam_dataset(dataset_path=None, cache_dir=None):
    """
    Ensure the BEAM dataset is available. Downloads and converts if needed.

    If `dataset_path` points to an existing JSON file, load it directly.
    Otherwise, download the parquet from HuggingFace, convert to JSON,
    and cache the result in `cache_dir`.
    """
    if cache_dir is None:
        cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".beam_cache")

    if dataset_path and os.path.exists(dataset_path):
        print(f"  Loading dataset: {dataset_path}")
        with open(dataset_path) as f:
            return json.load(f)

    cached_json = os.path.join(cache_dir, "beam-100k.json")
    if os.path.exists(cached_json):
        print(f"  Loading cached dataset: {cached_json}")
        with open(cached_json) as f:
            return json.load(f)

    print("  BEAM dataset not found locally. Downloading from HuggingFace...")
    parquet_path = download_beam_parquet(cache_dir)
    return convert_parquet_to_json(parquet_path, cached_json)
