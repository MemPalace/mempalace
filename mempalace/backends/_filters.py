"""Chroma-style ``where`` / ``where_document`` evaluation shared by every backend.

Backends that cannot push a filter down to their engine evaluate it here, on
the decoded metadata of each candidate row. The semantics follow Chroma:

- a bare ``{"key": value}`` is equality;
- a missing key reads as ``None``, so ``$ne`` and ``$nin`` match rows that do
  not carry the key, and ``$eq`` / ``$in`` / range operators do not;
- booleans compare as ``0`` / ``1``, the way Chroma stores them;
- a range comparison between incomparable types (``"1" > 0``) is false, not
  an error;
- ``$and`` / ``$or`` take lists of clauses.

Translating a filter into an engine's own language (SQL, Qdrant, Milvus
expressions) stays in each backend, which knows what its engine can push down.
"""

from typing import Any, Optional

from .base import UnsupportedFilterError

SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)


def validate_where(where: Optional[dict]) -> None:
    """Raise ``UnsupportedFilterError`` for any unknown operator in ``where``.

    RFC 001 section 1.4: an unknown operator must never be dropped silently.
    """
    if not where:
        return
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key.startswith("$") and key not in SUPPORTED_OPERATORS:
                raise UnsupportedFilterError(f"operator {key!r} not supported")
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))


def _comparable(value: Any) -> Any:
    if isinstance(value, bool):
        return int(value)
    return value


def _compare(actual: Any, op: str, expected: Any) -> bool:
    actual = _comparable(actual)
    expected = _comparable(expected)
    if op == "$eq":
        return actual == expected
    if op == "$ne":
        return actual != expected
    if op == "$in":
        return actual in (expected or [])
    if op == "$nin":
        return actual not in (expected or [])
    if op == "$contains":
        return str(expected) in str(actual or "")
    try:
        if op == "$gt":
            return actual > expected
        if op == "$gte":
            return actual >= expected
        if op == "$lt":
            return actual < expected
        if op == "$lte":
            return actual <= expected
    except TypeError:
        return False
    raise UnsupportedFilterError(f"operator {op!r} not supported")


def matches_where(meta: dict, where: Optional[dict]) -> bool:
    """Whether a row's metadata satisfies ``where``."""
    if not where:
        return True
    if not isinstance(where, dict):
        return False
    for key, expected in where.items():
        if key == "$and":
            if not all(matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key == "$or":
            if not any(matches_where(meta, clause) for clause in expected or []):
                return False
            continue
        if key.startswith("$"):
            raise UnsupportedFilterError(f"operator {key!r} not supported")
        actual = meta.get(key)
        if isinstance(expected, dict):
            for op, operand in expected.items():
                if not _compare(actual, op, operand):
                    return False
        elif actual != expected:
            return False
    return True


def matches_where_document(document: str, where_document: Optional[dict]) -> bool:
    """Whether a row's document satisfies ``where_document``."""
    if not where_document:
        return True
    if not isinstance(where_document, dict):
        return False
    for key, value in where_document.items():
        if key == "$contains":
            if str(value) not in document:
                return False
            continue
        if key == "$and":
            if not all(matches_where_document(document, clause) for clause in value or []):
                return False
            continue
        if key == "$or":
            if not any(matches_where_document(document, clause) for clause in value or []):
                return False
            continue
        raise UnsupportedFilterError(f"where_document operator {key!r} not supported")
    return True
