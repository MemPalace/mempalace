"""Who reviews a card's completion: an optional choice of the workspace owner.

A card stores at most one reviewer, a workspace member's actor ref (``user:<id>``).
Without one, completion review behaves as it always has: the workspace owner is
asked and only an owner may answer. With one, every move to Done opens a
completion review addressed to that member, and only that member may answer it.
Only a signed-in workspace owner chooses, changes or clears a reviewer, so a
member reviewing work they did themselves is always an explicit owner decision.
A reviewer who has left the workspace counts as no reviewer.
"""

from commonplace.extensions import ToolError

UNSET = object()


def _member_refs(members):
    return {f"user:{member['id']}" for member in members}


def owner_value(principal, value, members):
    """The validated reviewer ref, or None to clear; owner persons only."""
    if not principal.person or principal.role != "owner":
        raise ToolError("no: only a signed-in workspace owner can choose who reviews a card")
    if value is None or value == "":
        return None
    if not isinstance(value, str) or value not in _member_refs(members):
        raise ToolError("no: the reviewer must be a member of this workspace (user:<id>)")
    return value


def effective(row, members):
    """The card's reviewer while they are still a workspace member, else None."""
    ref = row["reviewer"] if "reviewer" in row.keys() else None
    return ref if ref and ref in _member_refs(members) else None


def gates_done(row, members, actor):
    """Whether a move to Done by ``actor`` must go through the chosen reviewer."""
    chosen = effective(row, members)
    return chosen is not None and chosen != actor


def may_decide(row, members, principal):
    """Whether ``principal`` may answer this card's completion review."""
    chosen = effective(row, members)
    if chosen is None:
        return principal.person and principal.role == "owner"
    return principal.person and principal.actor == chosen
