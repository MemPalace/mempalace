"""Calibration scoring: exact-match against single-class label."""
from __future__ import annotations


def score(predicted: str, label: str, classes: list[str]) -> dict:
    """Return {"correct": bool, "predicted_normalized": str}."""
    p = predicted.strip().lower().strip(".,;:'\"")
    target = label.strip().lower()
    correct = p == target
    if not correct:
        for c in classes:
            if p == c.strip().lower():
                # Predicted a valid class, just not the target.
                return {"correct": False, "predicted_normalized": p}
        return {"correct": False, "predicted_normalized": p}
    return {"correct": True, "predicted_normalized": p}
