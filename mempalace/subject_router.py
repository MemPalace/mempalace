"""Deterministic, local-first subject routing for drawer-sized content.

Rooms are stable subjects, not source types or memory categories.  This
module keeps that policy explicit: named subjects may use exact keyword
matches, broader subjects use the palace's local embedding model, and
ambiguous content lands in a visible review room instead of being guessed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import numpy as np

SUBJECT_ROOMS_ENV = "MEMPALACE_SUBJECT_ROOMS_JSON"
_ROOM_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HALL_CATEGORY_ROOMS = {
    "advice",
    "decisions",
    "discoveries",
    "emotional-context",
    "events",
    "facts",
    "milestones",
    "preferences",
    "problems",
}


class SubjectRouterError(ValueError):
    """Raised when a subject-routing policy is missing or malformed."""


@dataclass(frozen=True)
class SubjectRoom:
    name: str
    description: str
    keywords: tuple[str, ...]
    keyword_min_hits: int
    semantic: bool


@dataclass(frozen=True)
class SubjectRoute:
    room: str
    method: str
    score: float


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _normalized_rows(vectors: Iterable[Iterable[float]]) -> np.ndarray:
    matrix = np.asarray(list(vectors), dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[0] == 0:
        raise SubjectRouterError("embedding function returned an invalid matrix")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    if np.any(norms == 0):
        raise SubjectRouterError("embedding function returned a zero-length vector")
    return matrix / norms


class SubjectRouter:
    """Route chunks through exact named subjects, then local semantics."""

    def __init__(
        self,
        policy: dict[str, Any],
        *,
        embedding_function: Callable[[list[str]], list[list[float]]] | None = None,
        embedding_identity: str | None = None,
    ) -> None:
        if not isinstance(policy, dict):
            raise SubjectRouterError("subject routing policy must be an object")
        allowed = {"version", "unfiled_room", "min_similarity", "min_margin", "rooms"}
        if set(policy) != allowed:
            raise SubjectRouterError("subject routing policy fields do not match the schema")

        version = policy.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", version):
            raise SubjectRouterError("subject routing version must be a stable slug")
        unfiled_room = policy.get("unfiled_room")
        self._validate_room_name(unfiled_room, "unfiled_room")

        min_similarity = policy.get("min_similarity")
        min_margin = policy.get("min_margin")
        if (
            isinstance(min_similarity, bool)
            or not isinstance(min_similarity, (int, float))
            or not -1 <= float(min_similarity) <= 1
        ):
            raise SubjectRouterError("min_similarity must be between -1 and 1")
        if (
            isinstance(min_margin, bool)
            or not isinstance(min_margin, (int, float))
            or not 0 <= float(min_margin) <= 2
        ):
            raise SubjectRouterError("min_margin must be between 0 and 2")

        raw_rooms = policy.get("rooms")
        if not isinstance(raw_rooms, list) or not raw_rooms:
            raise SubjectRouterError("subject routing policy needs at least one room")
        rooms: list[SubjectRoom] = []
        seen: set[str] = {unfiled_room}
        for raw in raw_rooms:
            if not isinstance(raw, dict) or set(raw) != {
                "name",
                "description",
                "keywords",
                "keyword_min_hits",
                "semantic",
            }:
                raise SubjectRouterError("subject room fields do not match the schema")
            name = raw.get("name")
            self._validate_room_name(name, "room name")
            if name in seen:
                raise SubjectRouterError("subject room names must be unique")
            seen.add(name)
            description = raw.get("description")
            if not isinstance(description, str) or not description.strip():
                raise SubjectRouterError("subject room description must be non-empty")
            keywords = raw.get("keywords")
            if not isinstance(keywords, list) or not all(
                isinstance(item, str) and item.strip() for item in keywords
            ):
                raise SubjectRouterError("subject room keywords must be non-empty strings")
            threshold = raw.get("keyword_min_hits")
            if type(threshold) is not int or threshold < 1:
                raise SubjectRouterError("keyword_min_hits must be a positive integer")
            semantic = raw.get("semantic")
            if type(semantic) is not bool:
                raise SubjectRouterError("semantic must be a boolean")
            rooms.append(
                SubjectRoom(
                    name=name,
                    description=description.strip(),
                    keywords=tuple(item.strip().lower() for item in keywords),
                    keyword_min_hits=threshold,
                    semantic=semantic,
                )
            )

        semantic_rooms = tuple(room for room in rooms if room.semantic)
        if len(semantic_rooms) < 2:
            raise SubjectRouterError("subject routing needs at least two semantic rooms")

        self.version = version
        self.unfiled_room = unfiled_room
        self.min_similarity = float(min_similarity)
        self.min_margin = float(min_margin)
        self.rooms = tuple(rooms)
        self._keyword_patterns = {
            room.name: tuple(
                re.compile(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])")
                for keyword in room.keywords
            )
            for room in rooms
        }
        if embedding_function is None:
            from .embedding import current_model_name, get_embedding_function

            embedding_identity = f"mempalace:{current_model_name()}"
            embedding_function = get_embedding_function()
        elif embedding_identity is None:
            embedding_identity = (
                f"callable:{embedding_function.__module__}."
                f"{getattr(embedding_function, '__qualname__', type(embedding_function).__qualname__)}"
            )
        self.embedding_identity = embedding_identity
        fingerprint_payload = {
            "schema": "mempalace-subject-policy/v2",
            "policy": policy,
            "embedding_identity": embedding_identity,
        }
        self.fingerprint = (
            "sha256:" + hashlib.sha256(_canonical_json(fingerprint_payload)).hexdigest()
        )
        self._embedding_function = embedding_function
        self._semantic_rooms = semantic_rooms
        self._room_vectors = _normalized_rows(
            embedding_function([room.description for room in semantic_rooms])
        )

    @staticmethod
    def _validate_room_name(value: Any, label: str) -> None:
        if (
            not isinstance(value, str)
            or not _ROOM_RE.fullmatch(value)
            or value in _HALL_CATEGORY_ROOMS
        ):
            raise SubjectRouterError(f"{label} must be a stable subject slug")

    @classmethod
    def from_env(cls) -> "SubjectRouter":
        raw = os.environ.get(SUBJECT_ROOMS_ENV, "").strip()
        if not raw:
            raise SubjectRouterError(f"{SUBJECT_ROOMS_ENV} is required for subject routing")
        try:
            policy = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SubjectRouterError(f"{SUBJECT_ROOMS_ENV} must be valid JSON") from exc
        return cls(policy)

    def _keyword_route(self, text: str) -> SubjectRoute | None:
        lowered = text.lower()
        qualified: dict[str, int] = {}
        for room in self.rooms:
            hits = sum(
                len(pattern.findall(lowered)) for pattern in self._keyword_patterns[room.name]
            )
            if hits >= room.keyword_min_hits:
                qualified[room.name] = hits
        if not qualified:
            return None
        top_score = max(qualified.values())
        winners = [name for name, score in qualified.items() if score == top_score]
        if len(winners) != 1:
            return SubjectRoute(self.unfiled_room, "unfiled", float(top_score))
        return SubjectRoute(winners[0], "keyword", float(top_score))

    def route_many(self, texts: list[str]) -> list[SubjectRoute]:
        if not texts:
            return []
        routes: list[SubjectRoute | None] = [self._keyword_route(text) for text in texts]
        unresolved = [index for index, route in enumerate(routes) if route is None]
        if unresolved:
            raw_vectors = self._embedding_function([texts[index] for index in unresolved])
            try:
                vectors = _normalized_rows(raw_vectors)
            except SubjectRouterError as exc:
                if "zero-length vector" not in str(exc):
                    raise
                vectors = np.zeros((len(unresolved), self._room_vectors.shape[1]), dtype=np.float32)
                valid_rows: list[int] = []
                valid_vectors: list[Iterable[float]] = []
                for row_index, vector in enumerate(raw_vectors):
                    if float(np.linalg.norm(np.asarray(vector, dtype=np.float32))) == 0:
                        routes[unresolved[row_index]] = SubjectRoute(
                            self.unfiled_room, "unfiled", 0.0
                        )
                    else:
                        valid_rows.append(row_index)
                        valid_vectors.append(vector)
                if valid_vectors:
                    normalized = _normalized_rows(valid_vectors)
                    for normalized_index, row_index in enumerate(valid_rows):
                        vectors[row_index] = normalized[normalized_index]
            similarities = vectors @ self._room_vectors.T
            for row_index, text_index in enumerate(unresolved):
                if routes[text_index] is not None:
                    continue
                order = np.argsort(similarities[row_index])
                best_index = int(order[-1])
                second_index = int(order[-2])
                score = float(similarities[row_index, best_index])
                margin = score - float(similarities[row_index, second_index])
                if score < self.min_similarity or margin < self.min_margin:
                    routes[text_index] = SubjectRoute(self.unfiled_room, "unfiled", score)
                else:
                    routes[text_index] = SubjectRoute(
                        self._semantic_rooms[best_index].name,
                        "semantic",
                        score,
                    )
        return [route for route in routes if route is not None]
