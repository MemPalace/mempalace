import json

import pytest

from mempalace.subject_router import (
    SUBJECT_ROOMS_ENV,
    SubjectRouter,
    SubjectRouterError,
)


def policy():
    return {
        "version": "private-subjects-v1",
        "unfiled_room": "unfiled",
        "min_similarity": 0.5,
        "min_margin": 0.1,
        "rooms": [
            {
                "name": "mempalace",
                "description": "palace description",
                "keywords": ["mempalace", "wing_daphne"],
                "keyword_min_hits": 1,
                "semantic": False,
            },
            {
                "name": "music",
                "description": "music description",
                "keywords": ["song", "lyrics"],
                "keyword_min_hits": 3,
                "semantic": True,
            },
            {
                "name": "health",
                "description": "health description",
                "keywords": ["therapy", "doctor"],
                "keyword_min_hits": 3,
                "semantic": True,
            },
        ],
    }


def embedding_function(texts):
    vectors = {
        "music description": [1.0, 0.0],
        "health description": [0.0, 1.0],
        "A melody I cannot stop singing.": [0.95, 0.05],
        "The doctor changed my medication.": [0.05, 0.95],
        "Ambiguous ordinary material.": [0.7, 0.7],
    }
    return [vectors[text] for text in texts]


def test_routes_named_subjects_before_semantics():
    router = SubjectRouter(policy(), embedding_function=embedding_function)

    [route] = router.route_many(["MemPalace keeps this in wing_daphne."])

    assert route.room == "mempalace"
    assert route.method == "keyword"
    assert route.score == 2


def test_routes_general_subjects_with_local_embeddings():
    router = SubjectRouter(policy(), embedding_function=embedding_function)

    routes = router.route_many(
        ["A melody I cannot stop singing.", "The doctor changed my medication."]
    )

    assert [(route.room, route.method) for route in routes] == [
        ("music", "semantic"),
        ("health", "semantic"),
    ]


def test_ambiguous_content_goes_to_visible_review_room():
    router = SubjectRouter(policy(), embedding_function=embedding_function)

    [route] = router.route_many(["Ambiguous ordinary material."])

    assert route.room == "unfiled"
    assert route.method == "unfiled"


def test_policy_fingerprint_changes_with_the_taxonomy():
    first = SubjectRouter(policy(), embedding_function=embedding_function)
    changed = policy()
    changed["version"] = "private-subjects-v2"
    second = SubjectRouter(changed, embedding_function=embedding_function)

    assert first.fingerprint.startswith("sha256:")
    assert first.fingerprint != second.fingerprint


def test_policy_fingerprint_changes_with_the_embedding_model():
    first = SubjectRouter(
        policy(), embedding_function=embedding_function, embedding_identity="model-a"
    )
    second = SubjectRouter(
        policy(), embedding_function=embedding_function, embedding_identity="model-b"
    )

    assert first.fingerprint != second.fingerprint


def test_tied_named_subjects_go_to_visible_review_room():
    router = SubjectRouter(policy(), embedding_function=embedding_function)

    [route] = router.route_many(["song lyrics song therapy doctor therapy"])

    assert route.room == "unfiled"
    assert route.method == "unfiled"


def test_zero_vector_goes_to_visible_review_room():
    def zero_embedding(texts):
        vectors = {
            "music description": [1.0, 0.0],
            "health description": [0.0, 1.0],
            "empty semantic content": [0.0, 0.0],
        }
        return [vectors[text] for text in texts]

    router = SubjectRouter(policy(), embedding_function=zero_embedding)

    [route] = router.route_many(["empty semantic content"])

    assert route.room == "unfiled"
    assert route.score == 0.0


def test_from_env_fails_closed_without_policy(monkeypatch):
    monkeypatch.delenv(SUBJECT_ROOMS_ENV, raising=False)

    with pytest.raises(SubjectRouterError, match="is required"):
        SubjectRouter.from_env()


def test_from_env_rejects_hall_categories(monkeypatch):
    invalid = policy()
    invalid["rooms"][0]["name"] = "facts"
    monkeypatch.setenv(SUBJECT_ROOMS_ENV, json.dumps(invalid))

    with pytest.raises(SubjectRouterError, match="stable subject slug"):
        SubjectRouter.from_env()
