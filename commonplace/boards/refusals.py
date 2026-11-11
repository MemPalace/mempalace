"""Plain reasons and next steps for refused Done moves and handoffs.

The owner web UI shows the REST ``error`` text as is, so a refusal must say why and what to do
next. This only rewords the refusal text: who may move or hand off a card is decided in the
service, and the HTTP status stays what the service raised.
"""

from contextlib import contextmanager

from commonplace.extensions import ToolError

from .service import ParentError, VersionClash
from .store import CardRefused

RELEASE = (
    "Release it first: the board's coordinator does that, or an owner can when the board has "
    "no active coordinator. Then try again."
)
DECIDE = "Answer it under Needs me (Decide) instead; moving it by hand is not allowed."
PROPOSAL = "Approve or reject the proposal under Needs me first, then try again."
RELOAD = (
    "Reload the card to see its current state and try again. If it is still refused, ask the "
    "workspace owner or the board's coordinator."
)
SCOPES = {"done": "moved to Done", "handoff": "handed off"}
NOT_FOUND = ("card not found", "board not found")


def _known(kind, message, person):
    """The ``(reason, next step)`` for a refusal the service is known to raise, else None."""
    scope = SCOPES[kind]
    if "restricted-origin work needs" in message:
        return (
            f"This card came from a restricted connection and has not been released, so it cannot be {scope}.",
            RELEASE,
        )
    if "proposal needs parent approval" in message:
        return (
            f"This card is a proposal that has not been approved, so it cannot be {scope}.",
            PROPOSAL,
        )
    if "waiting on a person" in message or "use decide to move a card from review" in message:
        return (
            f"This card is waiting for a person's decision, so it cannot be {scope} by hand.",
            DECIDE,
        )
    if message == "this move requires a person or a handoff":
        if kind == "done" and person:
            return (
                "A card can only be moved to Done from Doing.",
                "Move it to Doing first, or hand it off to whoever should finish it.",
            )
        return (
            f"This card cannot be {scope} from its current state.",
            "Hand it off, or ask a person to move it.",
        )
    if message == "only todo or doing cards can be handed off":
        return (
            "Only cards in Todo or Doing can be handed off.",
            "Move the card to Todo or Doing first, then hand it off.",
        )
    if message == "take this card before handing it off":
        return (
            "An agent must hold a card before handing it off.",
            "Take the card first, then hand it off.",
        )
    if message == "options require a person":
        return (
            "Options can only be sent to a person.",
            "Hand off to a person, or clear the options.",
        )
    return None


def explain(kind, message, *, person=False):
    """``(reason, next step)`` for a refusal text of the Done (``"done"``) or handoff path."""
    message = message.removeprefix("no: ")
    known = _known(kind, message, person)
    if known:
        return known
    reason = message[:1].upper() + message[1:]
    return (reason if reason.endswith((".", "!", "?")) else reason + "."), RELOAD


@contextmanager
def explained(kind, *, person=False):
    """Re-raise the service's refusals of a Done move or handoff with a reason and a next step.

    ``CardRefused`` (it has its own code), a version clash, a parent error and the not-found answers
    keep their own answer.
    """
    try:
        yield
    except (CardRefused, ParentError, VersionClash):
        raise
    except ToolError as exc:
        message = str(exc).removeprefix("no: ")
        if message in NOT_FOUND:
            raise
        reason, step = explain(kind, message, person=person)
        raise CardRefused(f"{kind}_refused", reason, status=403, next_step=step) from exc
