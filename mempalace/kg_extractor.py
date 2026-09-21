"""
kg_extractor.py — Opt-in LLM extraction for the temporal knowledge graph.

Drawers remain the canonical verbatim memory. This module derives structured
triples from existing drawers with an explicitly configured LLM and stores
each fact with source drawer provenance so users can audit the extraction.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .config import normalize_wing_name, sanitize_iso_temporal, sanitize_kg_value, sanitize_name
from .knowledge_graph import KnowledgeGraph
from .palace import get_collection

MAX_CONTENT_CHARS = 12000
MAX_OUTPUT_TOKENS = 512
MAX_FACTS_PER_DRAWER = 8
HTTP_TIMEOUT_S = 60
DEFAULT_EXCLUDED_ROOMS = {"_registry", "diary"}
ALLOWED_PREDICATES = {
    "auto_includes",
    "blocks_connection",
    "built_with",
    "configured_with",
    "connects_to",
    "decodes_format",
    "depends_on",
    "fixed_issue",
    "has_component",
    "has_error",
    "requires_env",
    "resolved_by",
    "runs_on",
    "stores_in",
    "streams_response",
    "supports_model",
    "targets_platform",
    "uses_api",
    "uses_format",
    "uses_sdk",
    "uses_tool",
    "uses_ui_framework",
    "works_on",
}

PROMPT_TEMPLATE = """You are extracting candidate temporal knowledge-graph facts
from one verbatim memory drawer. Return only stable facts explicitly supported
by the numbered evidence snippets. Do not infer, summarize, or add outside
knowledge.

Source: {source_file}
Drawer ID: {drawer_id}
Wing: {wing}
Room: {room}

EVIDENCE SNIPPETS:
{snippets}

Return a JSON object with exactly this shape:

{{
  "facts": [
    {{
      "subject": "entity name",
      "predicate": "snake_case_relationship",
      "object": "entity or value",
      "valid_from": "YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ or null",
      "valid_to": "YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ or null",
      "confidence": 0.9,
      "evidence_id": 1
    }}
  ]
}}

Rules:
- Extract only facts that are directly stated in CONTENT.
- Return at most {max_facts} facts. Prefer precision over coverage.
- Prefer durable project/person/tool relationships, decisions, ownership,
  status, configuration, dates, and working agreements.
- Skip generic conversation metadata and vague opinions.
- Use ONLY these predicates; map wording to the closest one:
  {allowed_predicates}
- If the text says "app", "project", "client", or "this", use "{project_entity}"
  as the subject when that is the actual project being discussed.
- valid_from and valid_to must be null unless an explicit date/time appears.
- evidence_id must be one of the numbered snippets above.
- Never copy placeholder words from the schema.
- Do not emit facts whose subject is a whole sentence or action phrase.
- Skip agent action narration such as "Let me...", "Now...", "I'll...",
  "Next...", or "Checking..." unless the snippet states a completed result.
- For has_error facts, the object must include the actual problem, not only
  the file/component where it happened.
- If no good facts are present, return {{"facts":[]}}.
- Return valid JSON only. No code fences. No commentary.
"""


@dataclass(frozen=True)
class LLMConfig:
    """Resolved OpenAI-compatible chat-completions config."""

    endpoint: str = ""
    key: str = ""
    model: str = ""
    provider: str = "openai"
    no_think: bool = False
    max_output_tokens: int = MAX_OUTPUT_TOKENS

    def __init__(
        self,
        endpoint: Optional[str] = None,
        key: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        no_think: Optional[bool] = None,
        max_output_tokens: Optional[int] = None,
    ):
        object.__setattr__(
            self, "endpoint", (endpoint or os.environ.get("LLM_ENDPOINT", "")).rstrip("/")
        )
        object.__setattr__(self, "key", key or os.environ.get("LLM_KEY", ""))
        object.__setattr__(self, "model", model or os.environ.get("LLM_MODEL", ""))
        resolved_provider = (provider or os.environ.get("LLM_PROVIDER", "openai")).strip().lower()
        if resolved_provider not in {"openai", "ollama"}:
            raise ValueError("LLM_PROVIDER must be one of: openai, ollama")
        object.__setattr__(self, "provider", resolved_provider)
        object.__setattr__(
            self,
            "no_think",
            _env_truthy("LLM_NO_THINK") if no_think is None else bool(no_think),
        )
        raw_max_tokens = max_output_tokens or os.environ.get("LLM_MAX_OUTPUT_TOKENS")
        if raw_max_tokens in (None, ""):
            resolved_max_tokens = MAX_OUTPUT_TOKENS
        else:
            resolved_max_tokens = max(64, min(2048, int(raw_max_tokens)))
        object.__setattr__(self, "max_output_tokens", resolved_max_tokens)
        if self.endpoint:
            scheme = urllib.parse.urlparse(self.endpoint).scheme.lower()
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"LLM_ENDPOINT must use http:// or https:// (got scheme {scheme!r})"
                )

    def missing(self) -> list[str]:
        missing = []
        if not self.endpoint:
            missing.append("LLM_ENDPOINT (or --endpoint)")
        if not self.model:
            missing.append("LLM_MODEL (or --model)")
        return missing


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LLMCallResult:
    parsed: dict | None
    usage: dict | None = None
    error: str | None = None


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _preview_text(text: str, limit: int = 160) -> str:
    preview = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(preview) > limit:
        preview = preview[: limit - 1] + "…"
    return preview or "<empty>"


def _ollama_chat_url(endpoint: str) -> str:
    parsed = urllib.parse.urlparse(endpoint)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[: -len("/v1")]
    path = path.rstrip("/") + "/api/chat"
    return urllib.parse.urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def _parse_model_json(text: str, usage: dict | None) -> LLMCallResult:
    try:
        return LLMCallResult(json.loads(_strip_code_fence(text)), usage)
    except json.JSONDecodeError as exc:
        preview = _preview_text(text)
        return LLMCallResult(
            None,
            usage,
            f"invalid_model_json: {exc.msg}; preview={preview!r}",
        )


def _call_ollama_native(cfg: LLMConfig, prompt: str, timeout_s: float) -> LLMCallResult:
    body = {
        "model": cfg.model,
        "stream": False,
        "format": "json",
        "messages": [{"role": "user", "content": prompt}],
        "options": {"temperature": 0, "num_predict": cfg.max_output_tokens},
    }
    if cfg.no_think:
        body["think"] = False
    data = json.dumps(body).encode("utf-8")

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                _ollama_chat_url(cfg.endpoint),
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                return LLMCallResult(None, error=f"invalid_http_json: {exc.msg}")
            try:
                text = payload["message"]["content"]
            except (KeyError, TypeError) as exc:
                return LLMCallResult(None, error=f"missing_chat_content: {type(exc).__name__}")
            usage = {
                "prompt_tokens": int(payload.get("prompt_eval_count", 0) or 0),
                "completion_tokens": int(payload.get("eval_count", 0) or 0),
            }
            return _parse_model_json(text, usage)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 2:
                time.sleep(2**attempt)
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            suffix = f": {detail[:200]}" if detail else ""
            return LLMCallResult(None, error=f"http_{exc.code}{suffix}")
        except TimeoutError:
            return LLMCallResult(None, error=f"timeout_after_{timeout_s:g}s")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return LLMCallResult(None, error=f"url_error: {reason}")
        except Exception as exc:
            return LLMCallResult(None, error=f"{type(exc).__name__}: {exc}")
    return LLMCallResult(None, error="unknown_llm_failure")


def _call_llm(
    cfg: LLMConfig,
    *,
    source_file: str,
    drawer_id: str,
    wing: str,
    room: str,
    content: str,
    snippets: list[tuple[int, str]] | None = None,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> LLMCallResult:
    snippets = snippets or _build_evidence_snippets(content)
    prompt = PROMPT_TEMPLATE.format(
        source_file=source_file[:200],
        drawer_id=drawer_id,
        wing=wing,
        room=room,
        snippets=_format_evidence_snippets(snippets),
        project_entity=wing or "project",
        allowed_predicates=", ".join(sorted(ALLOWED_PREDICATES)),
        max_facts=MAX_FACTS_PER_DRAWER,
    )
    if cfg.provider == "ollama":
        return _call_ollama_native(cfg, prompt, timeout_s)
    if cfg.no_think:
        prompt = "/no_think\n" + prompt
    body = json.dumps(
        {
            "model": cfg.model,
            "temperature": 0,
            "max_tokens": cfg.max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if cfg.key:
        headers["Authorization"] = f"Bearer {cfg.key}"

    for attempt in range(3):
        try:
            req = urllib.request.Request(
                f"{cfg.endpoint}/chat/completions",
                data=body,
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                raw = resp.read().decode("utf-8")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                return LLMCallResult(None, error=f"invalid_http_json: {exc.msg}")
            try:
                text = payload["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError) as exc:
                return LLMCallResult(None, error=f"missing_chat_content: {type(exc).__name__}")
            return _parse_model_json(text, payload.get("usage"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 2:
                time.sleep(2**attempt)
                continue
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            suffix = f": {detail[:200]}" if detail else ""
            return LLMCallResult(None, error=f"http_{exc.code}{suffix}")
        except TimeoutError:
            return LLMCallResult(None, error=f"timeout_after_{timeout_s:g}s")
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            return LLMCallResult(None, error=f"url_error: {reason}")
        except Exception as exc:
            return LLMCallResult(None, error=f"{type(exc).__name__}: {exc}")
    return LLMCallResult(None, error="unknown_llm_failure")


def _normalize_predicate(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return sanitize_name(value, "predicate")


def _looks_like_sentence(value: str) -> bool:
    words = [word for word in re.split(r"\s+", value.strip()) if word]
    if len(words) > 6:
        return True
    return value.strip().endswith((".", "!", "?"))


def _entity_slug(value: str) -> str:
    return normalize_wing_name(str(value or "").replace(".", "_"))


def _postprocess_project_fact(fact: dict, project_entity: str | None) -> dict | None:
    """Normalize project-subject facts and drop progress noise."""
    if not project_entity:
        return fact
    project_slug = normalize_wing_name(project_entity)
    if _entity_slug(fact["subject"]) == project_slug:
        fact["subject"] = project_slug

    obj_l = fact["object"].lower()
    if fact["predicate"] == "uses_sdk" and "liquid glass" in obj_l:
        fact["predicate"] = "uses_ui_framework"
    if fact["predicate"] == "uses_sdk" and "xcode" in obj_l:
        fact["predicate"] = "configured_with"
    if fact["predicate"] == "uses_ui_framework" and "api" in obj_l:
        fact["predicate"] = "uses_api"
    if fact["predicate"] == "has_component" and "api" in obj_l:
        fact["predicate"] = "uses_api"
    if fact["predicate"] == "runs_on" and _object_mentions_platform(fact["object"]):
        fact["predicate"] = "targets_platform"

    if fact["predicate"] == "works_on" and fact["subject"] == project_slug:
        return None

    return fact


def _evidence_supports_object(evidence: str, obj: str) -> bool:
    evidence_l = evidence.lower()
    obj_l = obj.lower()
    if obj_l in evidence_l:
        return True
    tokens = [
        token for token in re.findall(r"[a-z0-9]+", obj_l) if token.isdigit() or len(token) > 2
    ]
    if not tokens:
        return True
    return all(token in evidence_l for token in tokens)


def _evidence_is_progress_chatter(evidence: str) -> bool:
    """Reject transcript narration that describes what the agent is about to do."""
    normalized = re.sub(r"\s+", " ", evidence.strip().lower())
    normalized = re.sub(r"^>+\s*", "", normalized)
    prefixes = (
        "checking ",
        "i'll ",
        "i will ",
        "let me ",
        "let's ",
        "next ",
        "now ",
        "verifying ",
    )
    if normalized.startswith(prefixes):
        return True
    return bool(re.search(r"\bi have\b.*\bopen\b.*\byou just need\b", normalized))


def _looks_like_file_reference(value: str) -> bool:
    return bool(re.fullmatch(r"[\w./@:+ -]+\.[A-Za-z0-9]{1,12}", value.strip()))


def _underspecified_error_fact(predicate: str, obj: str) -> bool:
    if predicate != "has_error":
        return False
    if not _looks_like_file_reference(obj):
        return False
    return not re.search(
        r"\b(error|exception|fail(?:ed|ure)?|bug|crash|issue|mismatch|blocked|missing)\b",
        obj,
        re.IGNORECASE,
    )


def _unproven_fixed_issue_fact(predicate: str, evidence: str) -> bool:
    if predicate != "fixed_issue":
        return False
    return not re.search(
        r"\b(fix(?:ed|es)?|resolved?|addressed|now builds|builds successfully|passes?|working)\b",
        evidence,
        re.IGNORECASE,
    )


def _invalid_platform_fact(predicate: str, obj: str) -> bool:
    if predicate != "targets_platform":
        return False
    return bool(re.search(r"\b(xcode|sdk|api)\b", obj, re.IGNORECASE))


def _object_mentions_platform(obj: str) -> bool:
    return bool(re.search(r"\b(ios|macos|visionos|simulator)\b", obj, re.IGNORECASE))


def _sanitize_temporal(value, field_name: str):
    if value in (None, ""):
        return None
    return sanitize_iso_temporal(str(value), field_name)


def _build_evidence_snippets(content: str, max_snippets: int = 80) -> list[tuple[int, str]]:
    """Split drawer text into exact snippets the LLM can cite by id."""
    snippets: list[str] = []
    for paragraph in re.split(r"\n{2,}", content[:MAX_CONTENT_CHARS]):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z`>])", paragraph)
        for part in parts:
            part = part.strip()
            if len(part) < 20:
                continue
            if len(part) > 500:
                for line in part.splitlines():
                    line = line.strip()
                    if len(line) >= 20:
                        snippets.append(line[:500])
            else:
                snippets.append(part)
            if len(snippets) >= max_snippets:
                break
        if len(snippets) >= max_snippets:
            break
    return list(enumerate(snippets, 1))


def _format_evidence_snippets(snippets: list[tuple[int, str]]) -> str:
    if not snippets:
        return "[1] (no extractable snippets)"
    return "\n".join(f"[{idx}] {snippet}" for idx, snippet in snippets)


def _canonical_evidence(content: str, evidence: str) -> str | None:
    """Return the exact substring from content supporting evidence, if found.

    LLMs often preserve the words but collapse line breaks/spaces. Accept that
    narrow formatting drift, but store the canonical substring from the drawer
    so KG provenance remains verbatim.
    """
    if not evidence:
        return None
    if evidence in content:
        return evidence
    tokens = [token for token in re.split(r"\s+", evidence.strip()) if token]
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, content)
    if not match:
        return None
    return match.group(0)


def _parse_fact_candidates(
    parsed: dict,
    content: str,
    min_confidence: float,
    snippets: list[tuple[int, str]] | None = None,
    project_entity: str | None = None,
) -> tuple[list[dict], list[dict]]:
    facts = []
    rejected = []
    snippet_map = {idx: text for idx, text in snippets or []}
    if not isinstance(parsed, dict):
        return facts, [{"reason": "invalid_json_shape", "raw": parsed}]

    for raw in parsed.get("facts", []) or []:
        if not isinstance(raw, dict):
            rejected.append({"reason": "invalid_fact_shape", "raw": raw})
            continue
        try:
            confidence = float(raw.get("confidence", 0.0))
            evidence = str(raw.get("evidence") or "").strip()
            if confidence < min_confidence:
                rejected.append({"reason": "low_confidence", "raw": raw})
                continue
            canonical_evidence = None
            evidence_id = raw.get("evidence_id")
            if evidence_id not in (None, ""):
                try:
                    canonical_evidence = snippet_map[int(evidence_id)]
                except (KeyError, TypeError, ValueError):
                    rejected.append({"reason": "invalid_evidence_id", "raw": raw})
                    continue
            else:
                canonical_evidence = _canonical_evidence(content, evidence)
            if canonical_evidence is None:
                rejected.append({"reason": "evidence_not_found_verbatim", "raw": raw})
                continue
            if _evidence_is_progress_chatter(canonical_evidence):
                rejected.append({"reason": "progress_evidence", "raw": raw})
                continue
            subject = sanitize_kg_value(str(raw.get("subject") or ""), "subject")
            predicate = _normalize_predicate(str(raw.get("predicate") or ""))
            obj = raw.get("object")
            if isinstance(obj, bool):
                rejected.append({"reason": "boolean_object", "raw": raw})
                continue
            obj = sanitize_kg_value(str(obj or ""), "object")
            if _looks_like_sentence(subject):
                rejected.append({"reason": "sentence_subject", "raw": raw})
                continue
            if predicate not in ALLOWED_PREDICATES:
                rejected.append({"reason": "unsupported_predicate", "raw": raw})
                continue
            if (
                predicate == "works_on"
                and project_entity
                and _entity_slug(subject) == normalize_wing_name(project_entity)
            ):
                rejected.append({"reason": "project_progress_fact", "raw": raw})
                continue
            if _underspecified_error_fact(predicate, obj):
                rejected.append({"reason": "underspecified_error", "raw": raw})
                continue
            if _unproven_fixed_issue_fact(predicate, canonical_evidence):
                rejected.append({"reason": "unproven_fixed_issue", "raw": raw})
                continue
            if _invalid_platform_fact(predicate, obj):
                rejected.append({"reason": "non_platform_target", "raw": raw})
                continue
            if not _evidence_supports_object(canonical_evidence, obj):
                rejected.append({"reason": "object_not_supported_by_evidence", "raw": raw})
                continue

            fact = {
                "subject": subject,
                "predicate": predicate,
                "object": obj,
                "valid_from": _sanitize_temporal(raw.get("valid_from"), "valid_from"),
                "valid_to": _sanitize_temporal(raw.get("valid_to"), "valid_to"),
                "confidence": confidence,
                "evidence": canonical_evidence,
            }
            fact = _postprocess_project_fact(fact, project_entity)
            if fact is None:
                rejected.append({"reason": "project_progress_fact", "raw": raw})
                continue
        except (TypeError, ValueError):
            rejected.append({"reason": "invalid_fact_fields", "raw": raw})
            continue
        facts.append(fact)
    return facts, rejected


def _parse_facts(parsed: dict, content: str, min_confidence: float) -> list[dict]:
    facts, _rejected = _parse_fact_candidates(parsed, content, min_confidence)
    return facts


def _print_fact(prefix: str, fact: dict) -> None:
    print(
        f"      {prefix} {fact['subject']} --{fact['predicate']}--> "
        f"{fact['object']} (confidence {fact['confidence']:.2f})"
    )
    print(f"        evidence: {fact['evidence']}")


def _print_rejection(rejection: dict) -> None:
    raw = rejection.get("raw")
    if not isinstance(raw, dict):
        print(f"      REJECTED [{rejection.get('reason')}]: {raw!r}")
        return
    subject = raw.get("subject", "")
    predicate = raw.get("predicate", "")
    obj = raw.get("object", "")
    print(f"      REJECTED [{rejection.get('reason')}]: {subject} --{predicate}--> {obj}")
    evidence = raw.get("evidence")
    if evidence:
        print(f"        evidence: {evidence}")


def _coerce_llm_result(result) -> tuple[dict | None, dict | None, str | None]:
    if isinstance(result, LLMCallResult):
        return result.parsed, result.usage, result.error
    parsed, usage = result
    return parsed, usage, None


def _extract_drawer_candidates(
    cfg: LLMConfig,
    *,
    drawer_id: str,
    content: str,
    meta: dict,
    min_confidence: float,
    timeout_s: float,
) -> dict:
    source_file = meta.get("source_file", "unknown")
    drawer_wing = meta.get("wing", "")
    drawer_room = meta.get("room", "")
    snippets = _build_evidence_snippets(content)

    call_started = time.monotonic()
    llm_result = _call_llm(
        cfg,
        source_file=source_file,
        drawer_id=drawer_id,
        wing=drawer_wing,
        room=drawer_room,
        content=content,
        snippets=snippets,
        timeout_s=timeout_s,
    )
    elapsed_s = time.monotonic() - call_started
    parsed, usage, llm_error = _coerce_llm_result(llm_result)
    input_tokens = int((usage or {}).get("prompt_tokens", 0) or 0)
    output_tokens = int((usage or {}).get("completion_tokens", 0) or 0)

    if not parsed:
        return {
            "drawer_id": drawer_id,
            "wing": drawer_wing,
            "room": drawer_room,
            "source_file": source_file,
            "elapsed_s": elapsed_s,
            "success": False,
            "error": llm_error or "llm_failed",
            "raw_facts": 0,
            "facts": [],
            "rejected": [],
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }

    raw_fact_count = len(parsed.get("facts", []) or []) if isinstance(parsed, dict) else 0
    facts, rejected = _parse_fact_candidates(
        parsed,
        content,
        min_confidence,
        snippets=snippets,
        project_entity=drawer_wing,
    )
    return {
        "drawer_id": drawer_id,
        "wing": drawer_wing,
        "room": drawer_room,
        "source_file": source_file,
        "elapsed_s": elapsed_s,
        "success": True,
        "error": None,
        "raw_facts": raw_fact_count,
        "facts": facts,
        "rejected": rejected,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _iter_drawers(collection, *, wing=None, room=None, limit=0, excluded_rooms=None):
    excluded_rooms = set(excluded_rooms or ())
    total = collection.count()
    yielded = 0
    offset = 0
    batch_size = 1000
    while offset < total:
        batch = collection.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
        ids = batch.get("ids") or []
        if not ids:
            break
        for drawer_id, doc, meta in zip(
            ids, batch.get("documents") or [], batch.get("metadatas") or []
        ):
            meta = meta or {}
            if wing and meta.get("wing") != wing:
                continue
            if room and meta.get("room") != room:
                continue
            if not room and meta.get("room") in excluded_rooms:
                continue
            yield drawer_id, doc or "", meta
            yielded += 1
            if limit and yielded >= limit:
                return
        offset += len(ids)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _count_errors(drawer_results: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for result in drawer_results:
        error = result.get("error")
        if not error:
            continue
        counts[error] = counts.get(error, 0) + 1
    return [{"error": error, "count": count} for error, count in sorted(counts.items())]


def _summarize_benchmark_model(model: str, drawer_results: list[dict]) -> dict:
    elapsed_values = [float(result.get("elapsed_s", 0.0) or 0.0) for result in drawer_results]
    processed = len(drawer_results)
    failed = sum(1 for result in drawer_results if not result.get("success"))
    accepted = sum(len(result.get("facts") or []) for result in drawer_results)
    rejected = sum(len(result.get("rejected") or []) for result in drawer_results)
    raw = sum(int(result.get("raw_facts", 0) or 0) for result in drawer_results)
    input_tokens = sum(int(result.get("input_tokens", 0) or 0) for result in drawer_results)
    output_tokens = sum(int(result.get("output_tokens", 0) or 0) for result in drawer_results)
    elapsed_total = sum(elapsed_values)
    return {
        "model": model,
        "processed": processed,
        "successful_drawers": processed - failed,
        "failed": failed,
        "raw_facts": raw,
        "candidate_facts": accepted,
        "rejected_facts": rejected,
        "elapsed_s": elapsed_total,
        "avg_s": elapsed_total / processed if processed else 0.0,
        "p50_s": _median(elapsed_values),
        "max_s": max(elapsed_values) if elapsed_values else 0.0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "errors": _count_errors(drawer_results),
        "drawers": drawer_results,
    }


def _print_benchmark_summary(model_results: list[dict]) -> None:
    print("\nKG benchmark summary:")
    print("  model                          ok/total  avg_s  p50_s  max_s  raw  acc  rej  fail")
    for result in model_results:
        ok_total = f"{result['successful_drawers']}/{result['processed']}"
        print(
            f"  {result['model'][:30]:30} {ok_total:>8} "
            f"{result['avg_s']:6.1f} {result['p50_s']:6.1f} {result['max_s']:6.1f} "
            f"{result['raw_facts']:4d} {result['candidate_facts']:4d} "
            f"{result['rejected_facts']:4d} {result['failed']:5d}"
        )
        if result["errors"]:
            failures = ", ".join(f"{item['error']} x{item['count']}" for item in result["errors"])
            print(f"    failures: {failures}")


def benchmark_kg_models(
    palace_path: str,
    *,
    models: list[str],
    endpoint: str | None = None,
    key: str | None = None,
    provider: str | None = None,
    wing: str | None = None,
    room: str | None = None,
    samples: int = 3,
    min_confidence: float = 0.0,
    no_think: bool | None = None,
    max_output_tokens: int | None = None,
    include_diary: bool = False,
    timeout_s: float = 30.0,
    show_details: bool = False,
    show_facts: bool = False,
    show_rejected: bool = False,
) -> dict:
    """Benchmark one or more local LLMs against the KG extraction task.

    This is always dry-run. It never opens or writes the SQLite knowledge graph.
    """

    cfg_probe = LLMConfig(
        endpoint=endpoint,
        key=key,
        model=models[0] if models else None,
        provider=provider,
        no_think=no_think,
        max_output_tokens=max_output_tokens,
    )
    missing = cfg_probe.missing()
    if missing:
        print("Error: missing configuration: " + ", ".join(missing))
        print("Set LLM_ENDPOINT / LLM_MODEL, or pass --endpoint / --model.")
        return {"success": False, "error": "missing-config", "missing": missing}

    if not models and cfg_probe.model:
        models = [cfg_probe.model]
    models = [model.strip() for model in models if model and model.strip()]
    if not models:
        print("Error: no models provided. Pass --model or set LLM_MODEL.")
        return {"success": False, "error": "missing-models"}

    collection = get_collection(palace_path, create=False)
    excluded_rooms = {"_registry"} if include_diary else set(DEFAULT_EXCLUDED_ROOMS)
    sample_limit = max(1, samples)
    sample_drawers = list(
        _iter_drawers(
            collection,
            wing=wing,
            room=room,
            limit=sample_limit,
            excluded_rooms=excluded_rooms,
        )
    )
    if not sample_drawers:
        print("Error: no matching drawers found for KG benchmark.")
        return {"success": False, "error": "no-drawers"}

    print("Benchmarking KG extraction (dry-run only; no triples will be written)")
    print(f"Endpoint: {cfg_probe.endpoint}")
    print(f"Provider: {cfg_probe.provider}")
    print(f"Models: {', '.join(models)}")
    print(
        f"Sample: {len(sample_drawers)} drawer(s)"
        + (f" from wing={wing}" if wing else "")
        + (f" room={room}" if room else "")
    )
    print(
        f"Timeout: {timeout_s:g}s per drawer, max_tokens={cfg_probe.max_output_tokens}, "
        f"no_think={str(cfg_probe.no_think).lower()}"
    )
    if excluded_rooms:
        print("Skipping rooms by default: " + ", ".join(sorted(excluded_rooms)))

    interrupted = False
    model_results = []
    try:
        for model in models:
            cfg = LLMConfig(
                endpoint=endpoint,
                key=key,
                model=model,
                provider=provider,
                no_think=no_think,
                max_output_tokens=max_output_tokens,
            )
            print(f"\nModel: {model}", flush=True)
            drawer_results = []
            for index, (drawer_id, content, meta) in enumerate(sample_drawers, 1):
                if show_details:
                    print(f"  [{index}/{len(sample_drawers)}] {drawer_id}...", flush=True)
                drawer_result = _extract_drawer_candidates(
                    cfg,
                    drawer_id=drawer_id,
                    content=content,
                    meta=meta,
                    min_confidence=min_confidence,
                    timeout_s=timeout_s,
                )
                drawer_results.append(drawer_result)
                if not show_details:
                    continue
                if drawer_result["success"]:
                    print(
                        f"    OK {len(drawer_result['facts'])} accepted / "
                        f"{len(drawer_result['rejected'])} rejected / "
                        f"{drawer_result['raw_facts']} raw "
                        f"({drawer_result['elapsed_s']:.1f}s)"
                    )
                    if show_facts:
                        for fact in drawer_result["facts"]:
                            _print_fact("ACCEPTED", fact)
                    if show_rejected:
                        for rejection in drawer_result["rejected"]:
                            _print_rejection(rejection)
                else:
                    print(f"    FAIL {drawer_result['error']} ({drawer_result['elapsed_s']:.1f}s)")
            model_results.append(_summarize_benchmark_model(model, drawer_results))
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted. Benchmark stopped before all models completed.")

    _print_benchmark_summary(model_results)
    return {
        "success": bool(model_results) and not interrupted,
        "interrupted": interrupted,
        "dry_run": True,
        "sample_count": len(sample_drawers),
        "sample_drawers": [
            {
                "drawer_id": drawer_id,
                "wing": (meta or {}).get("wing", ""),
                "room": (meta or {}).get("room", ""),
                "source_file": (meta or {}).get("source_file", "unknown"),
            }
            for drawer_id, _content, meta in sample_drawers
        ],
        "models": model_results,
    }


def extract_kg_triples(
    palace_path: str,
    *,
    wing: str | None = None,
    room: str | None = None,
    limit: int = 0,
    dry_run: bool = False,
    min_confidence: float = 0.0,
    cfg: LLMConfig | None = None,
    show_facts: bool = False,
    show_rejected: bool = False,
    include_diary: bool = False,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> dict:
    """Extract and store KG triples from existing drawers.

    This is intentionally opt-in. It reads drawers already in the palace and
    writes only derived triples, each linked back to the source drawer.
    """

    cfg = cfg or LLMConfig()
    missing = cfg.missing()
    if missing:
        print("Error: missing configuration: " + ", ".join(missing))
        print("Set LLM_ENDPOINT / LLM_MODEL, or pass --endpoint / --model.")
        return {"success": False, "error": "missing-config", "missing": missing}

    collection = get_collection(palace_path, create=False)
    kg = KnowledgeGraph(
        db_path=os.path.join(os.path.expanduser(palace_path), "knowledge_graph.sqlite3")
    )

    processed = 0
    failed = 0
    candidates = 0
    raw_facts = 0
    rejected_facts = 0
    written = 0
    total_input = 0
    total_output = 0
    interrupted = False
    errors: list[dict] = []
    excluded_rooms = {"_registry"} if include_diary else set(DEFAULT_EXCLUDED_ROOMS)

    print(f"Extracting KG triples via {cfg.endpoint} ({cfg.model})...")
    if excluded_rooms:
        print("Skipping rooms by default: " + ", ".join(sorted(excluded_rooms)))
    if dry_run:
        print("DRY RUN - no triples will be written")

    try:
        for drawer_id, content, meta in _iter_drawers(
            collection, wing=wing, room=room, limit=limit, excluded_rooms=excluded_rooms
        ):
            processed += 1

            print(f"  [{processed}] processing {drawer_id}...", flush=True)
            drawer_result = _extract_drawer_candidates(
                cfg,
                drawer_id=drawer_id,
                content=content,
                meta=meta,
                min_confidence=min_confidence,
                timeout_s=timeout_s,
            )
            elapsed_s = drawer_result["elapsed_s"]
            total_input += drawer_result["input_tokens"]
            total_output += drawer_result["output_tokens"]
            if not drawer_result["success"]:
                failed += 1
                error = drawer_result["error"]
                errors.append({"drawer_id": drawer_id, "error": error})
                print(f"  [{processed}] [FAIL] {drawer_id} - {error} ({elapsed_s:.1f}s)")
                continue

            raw_fact_count = drawer_result["raw_facts"]
            facts = drawer_result["facts"]
            rejected = drawer_result["rejected"]
            raw_facts += raw_fact_count
            candidates += len(facts)
            rejected_facts += len(rejected)
            if dry_run:
                print(
                    f"  [{processed}] [DRY] {drawer_id} - "
                    f"{len(facts)} accepted / {len(rejected)} rejected / "
                    f"{raw_fact_count} raw ({elapsed_s:.1f}s)"
                )
                if show_facts:
                    for fact in facts:
                        _print_fact("ACCEPTED", fact)
                if show_rejected:
                    for rejection in rejected:
                        _print_rejection(rejection)
                continue

            for fact in facts:
                kg.add_triple(
                    fact["subject"],
                    fact["predicate"],
                    fact["object"],
                    valid_from=fact["valid_from"],
                    valid_to=fact["valid_to"],
                    confidence=fact["confidence"],
                    source_closet=fact["evidence"],
                    source_file=drawer_result["source_file"],
                    source_drawer_id=drawer_id,
                    adapter_name=f"kg-llm:{cfg.model}",
                )
                written += 1
            print(
                f"  [{processed}] [OK] {drawer_id} - "
                f"{len(facts)} accepted / {len(rejected)} rejected ({elapsed_s:.1f}s)"
            )
            if show_facts:
                for fact in facts:
                    _print_fact("WROTE", fact)
            if show_rejected:
                for rejection in rejected:
                    _print_rejection(rejection)
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted. No additional drawers will be processed.")
    finally:
        kg.close()

    result = {
        "success": failed == 0 and not interrupted,
        "interrupted": interrupted,
        "processed": processed,
        "failed": failed,
        "raw_facts": raw_facts,
        "candidate_facts": candidates,
        "rejected_facts": rejected_facts,
        "written": 0 if dry_run else written,
        "dry_run": dry_run,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "errors": errors,
    }
    print(
        f"\nDone. {processed} drawers processed, {raw_facts} raw facts, "
        f"{candidates} accepted, {rejected_facts} rejected, "
        f"{result['written']} written, {failed} failed."
    )
    return result
