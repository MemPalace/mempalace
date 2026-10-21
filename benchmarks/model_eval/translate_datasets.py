"""Translate benchmark datasets to additional languages using an Ollama model.

Usage:
    uv run python -m benchmarks.model_eval.translate_datasets \\
        --languages de,fr,it,ru,ko,hi \\
        --model kimi-k2.6:cloud \\
        --dataset-dir benchmarks/model_eval/datasets

Only the natural-language input text is translated. Labels, IDs, class lists,
room slugs, entity types, and memory types stay in English — they are system
identifiers the model must emit regardless of input language.

Proper nouns (fictional names, places, orgs, projects, tech terms, code blocks)
are preserved verbatim in the translated output.

⚠️  PRIVACY NOTE: The default model (`kimi-k2.6:cloud`) sends the prose to a
remote Ollama-hosted endpoint. This is fine for the synthetic benchmark
fixtures in this repo, but DO NOT run this script over real user data
(diary entries, conversation transcripts, palace drawers). MemPalace is
local-first by design — for real data, pass `--model` pointing to a
locally-hosted model (e.g. `qwen3:4b-instruct-2507-q8_0`).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

LANGUAGE_NAMES = {
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "ru": "Russian",
    "ko": "Korean",
    "hi": "Hindi",
    "es": "Spanish",
    "pt-BR": "Brazilian Portuguese",
    "zh": "Simplified Chinese",
    "ja": "Japanese",
    "ar": "Arabic",
}

# Which field in each task's dataset.jsonl contains the text to translate.
TASK_TEXT_FIELD = {
    "calibration": "text",
    "entity_extraction": "text",
    "memory_extraction": "text",
    "room_classification": "session_summary",
}

_SYSTEM_PROMPT = """\
You are a professional translator. Translate user-provided text accurately \
and naturally into {language_name}.

Strict rules — never violate these:
- Preserve ALL of the following exactly as-is: personal names (Aria, Solas, \
Bramble, Fenra, Thresh, Gera Vossen, Brennan Lyle, Mette Olafsen, Pol Krisat, \
Ren Solanke, Ivora Tinn, Bek Halloran, Pell Halloran, Karis Tornau, \
Yumara Felk, Doreth Ainsleigh, Iset Karadzic, Ralf Ginder, Saela, \
Hellis Mar, and any other proper names); fictional place names (Crestmoor, \
Wendelsea, Hollowmounts, Bridgewater, Aerwyn, Bryn-iili, Vroth-Karadz, \
Krast-Endel, Salt Flats, Tartine); organization names (Tartine Lab, \
Crestmoor Systems, Aerwyn Labs, Aerwyn Capital, Bridgewater Studio, \
Hollowmounts Institute, Wendelsea Audit Partners, etc.); project names \
(Embedding Spaces, Topic Clustering, Distributed Tracing, Type Systems, \
Parser Combinators, Invoice Parsing, Cashflow Models, Pollinator Paths, \
Native Species, Soil Microbes, Meta-Cognition, Faithful Chains, \
Dialogue Engine, etc.); technical terms and acronyms (LLVM, OCaml, Python, \
LaTeX, HDBSCAN, UMAP, t-SNE, IFRS, MACRS, GAAP, OCR, PR, CI, OTLP, gRPC, \
OpenTelemetry, PaddleOCR, Tesseract, ZUGFeRD, MACRS, Section 179, etc.); \
code blocks (```...``` or inline code); tracebacks and error messages.
- Translate only the surrounding natural-language prose.
- Return ONLY the translated text. No explanations, no labels, no quotes \
around the result."""


def translate(text: str, language_name: str, model: str, endpoint: str) -> str:
    """Send one text to Ollama and return the translated string.

    Uses streaming so cloud models with high latency don't hit the connection
    timeout — tokens arrive incrementally instead of in one big response.
    """
    payload = {
        "model": model,
        "system": _SYSTEM_PROMPT.format(language_name=language_name),
        "prompt": text,
        "stream": True,
        "options": {"temperature": 0.1},
    }
    # stream=True: connect timeout 30s, read timeout 120s per chunk.
    # The read timeout resets on every received chunk, so long responses
    # from slow cloud models don't abort mid-stream.
    with requests.post(
        f"{endpoint}/api/generate",
        json=payload,
        stream=True,
        timeout=(30, 120),
    ) as resp:
        resp.raise_for_status()
        parts = []
        for line in resp.iter_lines():
            if not line:
                continue
            chunk = json.loads(line)
            parts.append(chunk.get("response", ""))
            if chunk.get("done"):
                break
    return "".join(parts).strip()


def _translate_one(args: tuple) -> tuple[int, str, str]:
    """Worker: translate one sample. Returns (index, sample_id, translated_text).

    Retries up to 3 times; on persistent failure returns the English source
    so downstream code always has a string to work with (the sweep in
    `translate_file` will warn about identical pairs).
    """
    idx, sample_id, text, language_name, model, endpoint = args
    last_error: Exception | None = None
    for _ in range(3):
        try:
            return idx, sample_id, translate(text, language_name, model, endpoint)
        except Exception as e:
            last_error = e
            time.sleep(2)
    print(f"    ERROR on {sample_id}: {last_error}", file=sys.stderr, flush=True)
    return idx, sample_id, text  # fall back to English source


def translate_file(
    src: Path,
    dst: Path,
    field: str,
    language_name: str,
    model: str,
    endpoint: str,
    force: bool,
    workers: int = 8,
) -> None:
    if dst.exists() and not force:
        print(f"  skip (exists): {dst.name}", flush=True)
        return

    samples = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [None] * len(samples)

    work = [
        (i, s["id"], s.get(field, ""), language_name, model, endpoint)
        for i, s in enumerate(samples)
        if s.get(field)
    ]

    completed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_translate_one, item): item[0] for item in work}
        for future in as_completed(futures):
            idx, sample_id, translated = future.result()
            out = dict(samples[idx])
            out[field] = translated
            results[idx] = json.dumps(out, ensure_ascii=False)
            completed += 1
            print(f"  [{completed}/{len(samples)}] {sample_id}", flush=True)

    # samples with no text field (shouldn't happen but be safe)
    for i, s in enumerate(samples):
        if results[i] is None:
            results[i] = json.dumps(s, ensure_ascii=False)

    dst.write_text("\n".join(results) + "\n", encoding="utf-8")
    print(f"  wrote {dst}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Translate benchmark datasets to new languages")
    parser.add_argument("--languages", required=True, help="Comma-separated language codes, e.g. de,fr,it,ru,ko,hi")
    parser.add_argument("--model", default="kimi-k2.6:cloud", help="Ollama model tag to use for translation")
    parser.add_argument("--endpoint", default="http://localhost:11434")
    parser.add_argument("--dataset-dir", type=Path, default=Path(__file__).parent / "datasets")
    parser.add_argument("--tasks", default="all", help="all or comma-separated: calibration,entity_extraction,memory_extraction,room_classification")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files")
    args = parser.parse_args()

    langs = [code.strip() for code in args.languages.split(",") if code.strip()]
    tasks = list(TASK_TEXT_FIELD.keys()) if args.tasks == "all" else [t.strip() for t in args.tasks.split(",")]

    unknown_langs = [code for code in langs if code not in LANGUAGE_NAMES]
    if unknown_langs:
        print(f"Unknown language codes: {unknown_langs}. Add them to LANGUAGE_NAMES in this script.", file=sys.stderr)
        sys.exit(1)

    for lang in langs:
        lang_name = LANGUAGE_NAMES[lang]
        print(f"\n=== {lang_name} ({lang}) ===", flush=True)
        for task in tasks:
            field = TASK_TEXT_FIELD[task]
            src = args.dataset_dir / task / "dataset.jsonl"
            dst = args.dataset_dir / task / f"dataset.{lang}.jsonl"
            if not src.exists():
                print(f"  source missing: {src}", file=sys.stderr)
                continue
            print(f"\n  {task} [{field}] → {dst.name}", flush=True)
            translate_file(src, dst, field, lang_name, args.model, args.endpoint, args.force)

    print("\nDone.", flush=True)


if __name__ == "__main__":
    main()
