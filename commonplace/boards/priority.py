"""Card priority: a small named scale the workspace owner sets, ordered before position.

Stored as the rank (0 is most urgent) so SQL can order by it; every public card
and tool reply carries the name. Every card has one: the default is ``normal``.
"""

from commonplace.extensions import ToolError

UNSET = object()
PRIORITIES = ("urgent", "high", "normal", "low")
DEFAULT = "normal"
DEFAULT_RANK = PRIORITIES.index(DEFAULT)


def rank(name) -> int:
    return PRIORITIES.index(name)


def name(value) -> str:
    """Public name for a stored rank; anything unreadable falls back to the default."""
    if type(value) is int and 0 <= value < len(PRIORITIES):
        return PRIORITIES[value]
    return DEFAULT


def validate(value) -> str:
    if not isinstance(value, str) or value not in PRIORITIES:
        raise ToolError("no: priority must be one of " + ", ".join(PRIORITIES))
    return value


def owner_value(principal, value) -> str:
    if not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can set a priority")
    return validate(value)
