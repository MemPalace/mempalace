"""Memory extraction scoring.

Looser than entity F1 because content paraphrasing is expected. We score:
- Type accuracy: per-item, did the model assign the right memory type?
- Coverage: percentage of ground-truth items that have a corresponding
  predicted item with semantic match (cosine similarity > 0.6 on content)
- Hallucination rate: predicted items with no semantic match in truth
"""
from __future__ import annotations

import json

from ...metrics import cosine_similarity, embed_text


def _parse(predicted_text: str) -> list[dict]:
    try:
        data = json.loads(predicted_text)
    except json.JSONDecodeError:
        return []
    items = data.get("memories", []) if isinstance(data, dict) else []
    return [m for m in items if isinstance(m, dict)]


def score(
    predicted_text: str,
    ground_truth: list[dict],
    similarity_threshold: float = 0.6,
    embed_model: str = "nomic-embed-text",
    endpoint: str = "http://localhost:11434",
) -> dict:
    pred = _parse(predicted_text)
    truth = [m for m in ground_truth if isinstance(m, dict)]

    if not truth:
        return {
            "coverage": 0.0,
            "hallucination_rate": 1.0 if pred else 0.0,
            "type_accuracy": 0.0,
            "predicted_count": len(pred),
            "truth_count": 0,
            "valid_json": _is_valid(predicted_text),
        }

    # Pre-compute embeddings once per content string. Cuts API calls from
    # O(P*T) to O(P+T) by separating the embedding step from the pairwise
    # comparison. Important for cloud endpoints where each embed call is
    # an HTTP round-trip.
    truth_embs: list[list[float] | None] = []
    for t in truth:
        t_content = t.get("content", "")
        if isinstance(t_content, str) and t_content:
            truth_embs.append(embed_text(t_content, model=embed_model, endpoint=endpoint))
        else:
            truth_embs.append(None)

    pred_embs: list[list[float] | None] = []
    for p in pred:
        p_content = p.get("content", "")
        if isinstance(p_content, str) and p_content:
            pred_embs.append(embed_text(p_content, model=embed_model, endpoint=endpoint))
        else:
            pred_embs.append(None)

    matched_truth_indices: set[int] = set()
    matched_pred_indices: set[int] = set()
    type_correct = 0
    type_total = 0

    for pi, p in enumerate(pred):
        p_emb = pred_embs[pi]
        if p_emb is None:
            continue
        p_type = p.get("type", "").strip().lower()
        best_sim = 0.0
        best_ti = None
        for ti in range(len(truth)):
            if ti in matched_truth_indices or truth_embs[ti] is None:
                continue
            sim = cosine_similarity(p_emb, truth_embs[ti])
            if sim > best_sim:
                best_sim = sim
                best_ti = ti
        if best_ti is not None and best_sim >= similarity_threshold:
            matched_truth_indices.add(best_ti)
            matched_pred_indices.add(pi)
            type_total += 1
            t_type = truth[best_ti].get("type", "").strip().lower()
            if p_type == t_type:
                type_correct += 1

    coverage = len(matched_truth_indices) / len(truth)
    hallucination_rate = (len(pred) - len(matched_pred_indices)) / max(1, len(pred))
    type_accuracy = type_correct / type_total if type_total else 0.0

    return {
        "coverage": coverage,
        "hallucination_rate": hallucination_rate,
        "type_accuracy": type_accuracy,
        "predicted_count": len(pred),
        "truth_count": len(truth),
        "matched_count": len(matched_pred_indices),
        "valid_json": _is_valid(predicted_text),
    }


def _is_valid(text: str) -> bool:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and isinstance(data.get("memories", None), list)
