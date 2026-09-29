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
