"""Shared public hallway fields for projection and the final response gate."""

PUBLIC_KEYS = (
    "id",
    "wing",
    "rooms",
    "label",
    "entity_a",
    "entity_b",
    "co_occurrence_count",
    "created_at",
    "created_by",
    "strength",
    "stability",
    "last_activated",
    "access_count",
)
NUMERIC_KEYS = frozenset({"strength", "stability", "access_count", "co_occurrence_count"})


def public_row(row):
    return {key: row[key] for key in PUBLIC_KEYS if key in row}
