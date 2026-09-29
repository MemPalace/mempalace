"""Entity extraction scoring: F1 over (name, type) pairs.

Strict matching: predicted (name, type) must exactly equal a ground-truth
(name, type) pair to count as true positive. Case-insensitive on both
name and type. Names are normalized for whitespace.
"""
from __future__ import annotations

import json


def _norm(name: str) -> str:
    return " ".join(name.split()).lower()


def _parse(predicted_text: str) -> list[tuple[str, str]]:
    try:
        data = json.loads(predicted_text)
    except json.JSONDecodeError:
        return []
    entities = data.get("entities", []) if isinstance(data, dict) else []
    out = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        name = e.get("name", "")
        etype = e.get("type", "")
        if isinstance(name, str) and isinstance(etype, str) and name and etype:
            out.append((_norm(name), etype.strip().lower()))
    return out


def score(predicted_text: str, ground_truth: list[dict]) -> dict:
    """Return F1 metrics. ground_truth is a list of {"name": ..., "type": ...}."""
    pred = set(_parse(predicted_text))
    truth = {(_norm(e["name"]), e["type"].strip().lower()) for e in ground_truth}

    tp = len(pred & truth)
    fp = len(pred - truth)
    fn = len(truth - pred)

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "f1": f1,
        "precision": precision,
        "recall": recall,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "valid_json": len(pred) > 0 or _is_valid_empty(predicted_text),
    }


def _is_valid_empty(text: str) -> bool:
    """Empty entity list is still valid output if JSON parses."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(data, dict) and isinstance(data.get("entities", None), list)
