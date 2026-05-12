"""Room classification scoring.

Closed-set: exact match against the labeled room slug, with "other" treated
as a valid answer when ground truth is "other".

Open-set: cosine similarity between the embedding of the predicted slug
and the embedding of the preferred-open label. Scored 0-1.
"""
from __future__ import annotations

from ...metrics import label_similarity


def score_closed(predicted: str, target_label: str, rooms: list[str]) -> dict:
    p = predicted.strip().lower().strip(".,;:'\"")
    t = target_label.strip().lower()
    correct = p == t
    in_room_list = p == "other" or any(p == r.strip().lower() for r in rooms)
    return {
        "correct": correct,
        "predicted_normalized": p,
        "in_room_list": in_room_list,
    }


def score_open(
    predicted: str,
    preferred_label: str,
    embed_model: str = "nomic-embed-text",
    endpoint: str = "http://localhost:11434",
) -> dict:
    p = predicted.strip().lower().strip(".,;:'\"")
    sim = label_similarity(p, preferred_label, embed_model=embed_model, endpoint=endpoint)
    return {
        "predicted_normalized": p,
        "similarity": sim,
        "exact_match": p == preferred_label.strip().lower(),
    }


def score_clustering(
    predicted_labels: list[str], ground_truth_themes: list[str]
) -> dict:
    """Compute clustering quality across the corpus.

    Reports unique-label count, label collision rate, and a simple
    homogeneity proxy (samples sharing a ground-truth theme should share
    a predicted label).
    """
    if len(predicted_labels) != len(ground_truth_themes):
        raise ValueError("predicted_labels and ground_truth_themes must have the same length")

    pred_norm = [p.strip().lower() for p in predicted_labels]
    distinct_predicted = len(set(pred_norm))
    distinct_themes = len(set(ground_truth_themes))

    by_theme: dict[str, list[str]] = {}
    for theme, pred in zip(ground_truth_themes, pred_norm):
        by_theme.setdefault(theme, []).append(pred)

    homogeneity_scores = []
    for theme, preds in by_theme.items():
        if len(preds) <= 1:
            continue
        most_common_count = max(preds.count(p) for p in set(preds))
        homogeneity_scores.append(most_common_count / len(preds))
    homogeneity = sum(homogeneity_scores) / len(homogeneity_scores) if homogeneity_scores else 0.0

    return {
        "distinct_predicted_labels": distinct_predicted,
        "distinct_ground_truth_themes": distinct_themes,
        "label_compression_ratio": distinct_themes / max(1, distinct_predicted),
        "theme_homogeneity": homogeneity,
    }
