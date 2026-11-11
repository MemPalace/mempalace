"""Blocks are stored as references and hydrated on read with the reader's own permissions."""

from commonplace import redact
from commonplace.boards import why
from commonplace.boards.service import BoardService
from commonplace.extensions import ToolContext, ToolError


class Reader:
    """The person a thread is read for; every lookup runs as them."""

    def __init__(self, gateway, principal):
        self.gateway, self.principal = gateway, principal
        self._boards = None

    @property
    def boards(self) -> BoardService:
        if self._boards is None:
            self._boards = BoardService(ToolContext(principal=self.principal, gateway=self.gateway))
        return self._boards


def _clean(value):
    return redact.redact(value)[0] if isinstance(value, str) else value


def _card(reader, number):
    """The card as this person sees it, or None when it is gone or hidden from them."""
    if type(number) is not int:
        return None
    try:
        return reader.boards.task_show(number)
    except ToolError:
        return None


def work_status(card) -> tuple[str, str]:
    """The only status words the UI knows, and the plain reason when it is blocked."""
    reason = card.get("waiting")
    note = why.text(reason) if reason else ""
    if card["status"] == "done":
        return "done", ""
    if card["status"] == "review":
        if card.get("review_kind") != "decision":
            return "ready", ""
        if card.get("need") == "you":
            return "needs_you", ""
        return "blocked", "Waiting for an answer"
    if reason:  # open blockers and every other waiting reason; only present when the card has them
        return "blocked", note
    return ("working" if card["status"] == "doing" else "queued"), note


def work(reader, stored) -> dict:
    card = _card(reader, stored.get("card"))
    if card is None:
        return {**stored, "gone": True}
    status, note = work_status(card)
    links = [{"label": "Pull request", "url": card["pr_url"]}] if card.get("pr_url") else []
    return {
        **stored,
        "title": _clean(card["title"]),
        "status": status,
        "note": _clean(note or card.get("status_line") or ""),
        "links": links,
        "version": card["version"],
    }


def decision(reader, stored) -> dict:
    card = _card(reader, stored.get("card"))
    if card is None:
        return {**stored, "gone": True}
    answers = [e for e in card["events"] if e["kind"] == "decide"]
    answer = answers[-1] if answers else None
    return {
        **stored,
        "question": _clean(card["ask"] or card["title"]),
        "context": _clean(card.get("review_context") or ""),
        "options": [_clean(option) for option in card["ask_options"]],
        "version": card["version"],
        "open": card["status"] == "review" and card.get("review_kind") == "decision",
        "answer": _clean(answer["choice"] or answer["text"]) if answer else None,
    }


def action(reader, stored) -> dict:
    return {**stored, "label": _clean(stored.get("label", ""))}


def setup(reader, stored) -> dict:
    return {**stored, "can_configure": reader.principal.role == "owner"}


HYDRATORS = {"work": work, "decision": decision, "action": action, "setup": setup}


def hydrate(reader, blocks) -> list[dict]:
    """Each stored block through its hydrator; an unknown type comes back as stored."""
    return [
        HYDRATORS.get(block.get("type"), lambda _reader, stored: stored)(reader, block)
        for block in blocks
    ]
