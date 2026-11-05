"""Phone notification categories derived from the ordinary board service."""

from commonplace.boards import service
from commonplace.extensions import ToolContext

_NEEDS_EVENTS = {
    "card.created",
    "card.updated",
    "card.moved",
    "card.handoff",
    "card.review_needed",
    "card.decided",
}


def current(gateway, owner) -> dict[int, set[str]]:
    report = service.BoardService(ToolContext(principal=owner, gateway=gateway)).needs_me()
    result = {}
    for bucket in ("asks", "done_review", "backlog_from_agents", "plans", "stale"):
        category = "blocked" if bucket == "stale" else "needs_you"
        for card in report[bucket]:
            result.setdefault(card["number"], set()).add(category)
    return result


def applies(gateway, owner, number, category, *, cur) -> bool:
    if category in ("needs_you", "blocked"):
        return category in cur.get(number, ())
    if category != "done":
        return False
    board = service.BoardService(ToolContext(principal=owner, gateway=gateway))
    with board.store.connection() as conn:
        row = conn.execute(
            "SELECT status, created_by, return_to, ask_to, assignee FROM tasks WHERE id=?",
            (number,),
        ).fetchone()
    recipient = f"user:{owner.user_id}"
    return row is not None and row["status"] == "done" and recipient in tuple(row)[1:]


def enters(events, number, category, *, after_seq) -> list:
    result = []
    for event in events:
        if event["seq"] <= after_seq or event["data"].get("number") != number:
            continue
        kind = event["kind"]
        if category == "needs_you" and kind in _NEEDS_EVENTS:
            result.append(event)
        elif category == "done" and (
            kind == "card.created"
            or (kind in ("card.moved", "card.decided") and event["data"].get("to") == "done")
        ):
            result.append(event)
    return result
