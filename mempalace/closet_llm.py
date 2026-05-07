"""
closet_llm.py — Generate closets via a user-configured LLM for richer indexing.

The regex-based closet extraction catches action verbs, headers, and proper
nouns — but misses implicit topics, foreign-language content, and contextual
references. An LLM reads everything and produces better closets.

This module is **OPTIONAL and opt-in**. Regex closets are always created by
the miner; this path regenerates them afterward using whatever LLM the user
chooses. Core memory operations remain API-free by design (see CLAUDE.md,
"Local-first, zero API").

## Bring-your-own-LLM configuration

The endpoint is any OpenAI-compatible Chat Completions URL:

    LLM_ENDPOINT=http://localhost:11434/v1   # Ollama
    LLM_ENDPOINT=http://localhost:8000/v1    # vLLM, llama.cpp
    LLM_ENDPOINT=https://api.openai.com/v1
    LLM_ENDPOINT=https://openrouter.ai/api/v1
    LLM_ENDPOINT=https://api.anthropic.com/v1  # when proxied through a compat layer

Set:
    LLM_ENDPOINT — base URL (required)
    LLM_KEY      — bearer token (optional; local inference usually doesn't need it)
    LLM_MODEL    — model name (required), e.g. "gpt-4o-mini", "llama3:8b", "qwen2.5:7b"

Or pass flags on the CLI (flags win over env):

    python -m mempalace.closet_llm \\
        --palace ~/.mempalace/palace \\
        --endpoint http://localhost:11434/v1 \\
        --model llama3:8b

No vendor lock-in. No hidden dependency on any specific provider. Zero deps
added to pyproject — uses stdlib urllib.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional

from .palace import (
    NORMALIZE_VERSION,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)

MAX_CONTENT_CHARS = 30000
MAX_OUTPUT_TOKENS = 1500
HTTP_TIMEOUT_S = 60

PROMPT_TEMPLATE = """You are reading content filed in a memory palace. Generate a
topic-dense index that will be used to find this content later when someone searches.

Source: {source_file}
Wing: {wing} | Room: {room}

CONTENT:
{content}

---

Output a JSON object with EXACTLY these fields:

{{
  "topics": ["distinctive_word_or_phrase_1", "topic_2", ...],
  "quotes": ["[Speaker] verbatim quote", ...],
  "summary": "2-3 sentences describing what this content is about."
}}

RULES:
- Topics: 8-15 entries. Include proper nouns (names, places, projects),
  distinctive technical terms, and key concepts. NOT generic words like
  "conversation" or "discussion".
- Quotes: 2-5 entries. EXACT verbatim from the content, not paraphrased.
  Attribute with [Speaker] prefix if speaker is identifiable.
- Summary: mention WHO, WHAT, and WHY. No filler.
- Write in the same language as the content.
- Output valid JSON only. No code fences. No commentary.
"""


class LLMConfig:
    """Resolved LLM connection config. CLI flags > env vars."""

    def __init__(
        self,
        endpoint: Optional[str] = None,
        key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        self.endpoint = (endpoint or os.environ.get("LLM_ENDPOINT", "")).rstrip("/")
        self.key = key or os.environ.get("LLM_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "")
        if self.endpoint:
            # Privacy-by-architecture: reject file:// and other non-HTTP schemes
            # so a misconfigured endpoint cannot exfiltrate local files.
            scheme = urllib.parse.urlparse(self.endpoint).scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"LLM_ENDPOINT must use http:// or https:// (got scheme {scheme!r})"
                )

    def missing(self) -> list:
        missing = []
        if not self.endpoint:
            missing.append("LLM_ENDPOINT (or --endpoint)")
        if not self.model:
            missing.append("LLM_MODEL (or --model)")
        # key is optional — local inference servers (Ollama, vLLM) often don't require one
        return missing


def _call_llm(cfg: LLMConfig, source_file: str, wing: str, room: str, content: str):
    """Single LLM call via OpenAI-compatible /chat/completions.

    Returns (parsed_json_dict_or_None, usage_dict_or_None).
    """
    try:
        from mempalace.i18n import t

        lang_instruction = t("aaak.instruction")
    except Exception:
        lang_instruction = ""

    prompt = PROMPT_TEMPLATE.format(
        source_file=source_file[:100],
        wing=wing,
        room=room,
        content=content[:MAX_CONTENT_CHARS],
    )
    if lang_instruction and "english" not in lang_instruction.lower():
        prompt += f"\n\nLanguage instruction: {lang_instruction}"

    body = json.dumps(
        {
            "model": cfg.model,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if cfg.key:
        headers["Authorization"] = f"Bearer {cfg.key}"

    url = f"{cfg.endpoint}/chat/completions"

    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8")
            payload = json.loads(raw)

            text = payload["choices"][0]["message"]["content"].strip()
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            parsed = json.loads(text)
            return parsed, payload.get("usage")
        except json.JSONDecodeError:
            return None, None
        except urllib.error.HTTPError as e:
            # 429 / 503 = retry with backoff
            if e.code in (429, 503) and attempt < 2:
                time.sleep(2**attempt)
                continue
            return None, None
        except Exception as e:
            if "rate" in str(e).lower() and attempt < 2:
                time.sleep(2**attempt)
                continue
            return None, None
    return None, None


def _parsed_to_closet_lines(parsed, drawer_ids, entities_str):
    """Convert LLM's JSON output to closet pointer lines."""
    lines = []
    drawer_ref = ",".join(drawer_ids[:3])

    for topic in parsed.get("topics", [])[:15]:
        lines.append(f"{topic}|{entities_str}|→{drawer_ref}")
    for quote in parsed.get("quotes", [])[:5]:
        lines.append(f"{quote}|{entities_str}|→{drawer_ref}")
    summary = parsed.get("summary", "")
    if summary:
        lines.append(f"{summary[:200]}|{entities_str}|→{drawer_ref}")

    return lines


def regenerate_closets(
    palace_path,
    wing=None,
    sample=0,
    dry_run=False,
    cfg: Optional[LLMConfig] = None,
):
    """Regenerate closets using a configured LLM for richer topic extraction.

    Reads existing drawers, sends content to the configured endpoint,
    replaces regex closets with LLM-generated ones. Regex closets remain
    as the fallback whenever the call fails.
    """
    if cfg is None:
        cfg = LLMConfig()
    missing = cfg.missing()
    if missing:
        print("Error: missing configuration: " + ", ".join(missing))
        print("Set env vars LLM_ENDPOINT / LLM_MODEL (and optionally LLM_KEY),")
        print("or pass --endpoint / --model / --key on the CLI.")
        return {"error": "missing-config", "missing": missing}

    drawers_col = get_collection(palace_path, create=False)
    closets_col = get_closets_collection(palace_path)

    total = drawers_col.count()
    if total == 0:
        print("No drawers in palace.")
        return {"processed": 0}

    # Paginate the fetch — a single get(limit=total, ...) blows through
    # SQLite's SQLITE_MAX_VARIABLE_NUMBER (32766) on large palaces and
    # crashes inside chromadb (see #802, #850, #1073).
    by_source: dict = {}
    batch_size = 5000
    offset = 0
    while offset < total:
        batch = drawers_col.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        ids = batch["ids"]
        if not ids:
            break
        for doc_id, doc, meta in zip(ids, batch["documents"], batch["metadatas"]):
            meta = meta or {}
            source = meta.get("source_file", "unknown")
            w = meta.get("wing", "")
            if wing and w != wing:
                continue
            if source not in by_source:
                by_source[source] = {"drawer_ids": [], "content": [], "meta": meta}
            by_source[source]["drawer_ids"].append(doc_id)
            by_source[source]["content"].append(doc)
        offset += len(ids)

    sources = list(by_source.keys())
    if sample > 0:
        sources = sources[:sample]

    print(
        f"Regenerating closets for {len(sources)} source files via {cfg.endpoint} ({cfg.model})..."
    )
    if dry_run:
        print("DRY RUN — no changes will be written")

    processed = 0
    failed = 0
    total_input = 0
    total_output = 0

    for i, source in enumerate(sources, 1):
        data = by_source[source]
        content = "\n\n".join(data["content"])
        meta = data["meta"]
        w = meta.get("wing", "")
        r = meta.get("room", "")
        entities = meta.get("entities", "")

        if dry_run:
            print(f"  [{i}/{len(sources)}] {os.path.basename(source)} ({len(content)} chars)")
            continue

        parsed, usage = _call_llm(cfg, source, w, r, content)
        if not parsed:
            failed += 1
            print(f"  [{i}/{len(sources)}] ✗ {os.path.basename(source)} — LLM failed")
            continue

        if usage:
            total_input += usage.get("prompt_tokens", 0)
            total_output += usage.get("completion_tokens", 0)

        lines = _parsed_to_closet_lines(parsed, data["drawer_ids"], entities)
        # Use os.path.basename so Windows-style paths survive unchanged;
        # the naive split('/') would leave a bare path component on Windows
        # and collide across different files under different drives.
        closet_id_base = f"closet_{w}_{r}_{os.path.basename(source)[:30]}"

        # Serialize with concurrent mine operations on the same source —
        # otherwise a regex closet rebuild mid-regenerate races with our
        # purge+upsert cycle and leaves mixed regex/LLM lines.
        with mine_lock(source):
            purge_file_closets(closets_col, source)
            upsert_closet_lines(
                closets_col,
                closet_id_base,
                lines,
                {
                    "wing": w,
                    "room": r,
                    "source_file": source,
                    "generated_by": f"llm:{cfg.model}",
                    "filed_at": datetime.now().isoformat(),
                    "entities": entities,
                    # Stamp so the miner's stale-drawer gate doesn't treat
                    # LLM closets as leftovers and rebuild over them next run.
                    "normalize_version": NORMALIZE_VERSION,
                },
            )

        processed += 1
        n_topics = len(parsed.get("topics", []))
        print(f"  [{i}/{len(sources)}] ✓ {os.path.basename(source)} — {n_topics} topics")

    print(f"\nDone. {processed} regenerated, {failed} failed.")
    if total_input or total_output:
        print(f"Tokens: {total_input:,} in + {total_output:,} out (cost depends on provider)")

    return {
        "processed": processed,
        "failed": failed,
        "input_tokens": total_input,
        "output_tokens": total_output,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Regenerate closets via a user-configured LLM (OpenAI-compatible API)"
    )
    parser.add_argument(
        "--palace",
        default=os.path.expanduser("~/.mempalace/palace"),
        help="Path to the palace",
    )
    parser.add_argument("--wing", default=None, help="Limit to one wing")
    parser.add_argument("--sample", type=int, default=0, help="Only process first N source files")
    parser.add_argument("--dry-run", action="store_true", help="List work without calling the LLM")
    parser.add_argument(
        "--endpoint",
        default=None,
        help="LLM base URL (overrides $LLM_ENDPOINT), e.g. http://localhost:11434/v1",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="LLM bearer token (overrides $LLM_KEY). Optional for local inference.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help='LLM model name (overrides $LLM_MODEL), e.g. "gpt-4o-mini" or "llama3:8b"',
    )
    args = parser.parse_args()

    cfg = LLMConfig(endpoint=args.endpoint, key=args.key, model=args.model)
    regenerate_closets(
        args.palace, wing=args.wing, sample=args.sample, dry_run=args.dry_run, cfg=cfg
    )
