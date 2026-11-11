"""Pure, actor-independent reasons a card is not moving, with one plain sentence each.

The card view, the board summary, the claim refusals and the app all read these, so
the sentences exist only here."""

from .display import _waiting_on_person

CODES = (
    "blocked_by",
    "waits_on_person",
    "claim_expired",
    "plan_review",
    "not_released",
    "no_agent",
    "project_limit",
    "checks_pending",
)

# Reasons that stop an explicit claim or start; the others only explain what the board shows.
REFUSING = ("blocked_by", "waits_on_person", "plan_review", "not_released")


def text(reason: dict) -> str:
    code = reason["code"]
    if code == "blocked_by":
        return "Waiting on " + ", ".join(
            f"#{b['number']}: {b['title']}" for b in reason["blockers"]
        )
    if code == "waits_on_person":
        return "Waiting on a person"
    if code == "claim_expired":
        return "Claim expired: the agent stopped reporting; its next update renews it, or hand the card off"
    if code == "plan_review":
        return "Waiting for plan review"
    if code == "not_released":
        return "Waiting for release" + (": split stuck" if reason.get("split_stuck") else "")
    if code == "no_agent":
        return "No agent is connected to this project yet"
    if code == "project_limit":
        return f"Waiting for a free slot: {reason['used']} of {reason['cap']}"
    if code == "checks_pending":
        return "Waiting for checks to finish"
    raise ValueError("unknown waiting reason")


def waiting(
    row,
    *,
    blockers,
    released: bool,
    usage,
    claim_expired: bool = False,
    plan_review: bool = False,
    agents: bool | None = None,
) -> dict | None:
    """The one reason ``row`` is not moving, or None.

    ``agents`` is whether any agent connection can work on the card's project; None means
    unknown, which never produces a reason."""
    row = dict(row)
    status = row["status"]
    if status == "done":
        return None
    open_work = status in ("todo", "backlog")
    if blockers:
        reason = {"code": "blocked_by", "blockers": list(blockers)}
    elif _waiting_on_person(row):
        reason = {"code": "waits_on_person"}
    elif status == "doing" and claim_expired:
        reason = {"code": "claim_expired"}
    elif plan_review and not released and open_work:
        reason = {"code": "plan_review"}
    elif not released and open_work:
        reason = {"code": "not_released"}
        if row.get("split_stuck"):
            reason["split_stuck"] = True
    elif status == "todo" and agents is False:
        reason = {"code": "no_agent"}
    elif open_work and usage is not None and usage.cap is not None and usage.used >= usage.cap:
        reason = {"code": "project_limit", "used": usage.used, "cap": usage.cap}
    elif status in ("doing", "review") and row.get("checks") == "pending":
        reason = {"code": "checks_pending"}
    else:
        return None
    return {**reason, "text": text(reason)}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def notes(summary: dict, *, used: int | None = None, cap: int | None = None) -> list[dict]:
    """Board-level sentences from a ``{code: count}`` summary: why cards wait and why few
    agents are working. Only codes with cards behind them appear, in ``CODES`` order."""
    lines = []
    for code in CODES:
        n = summary.get(code, 0)
        if not n:
            continue
        cards = _plural(n, "card", "cards")
        if code == "blocked_by":
            line = f"{cards} wait for other cards to be Done."
        elif code == "waits_on_person":
            line = f"{cards} wait for a person to answer."
        elif code == "claim_expired":
            line = f"{cards} have an expired claim: the agent stopped reporting."
        elif code == "plan_review":
            line = f"{cards} wait for plan review."
        elif code == "not_released":
            line = f"{cards} wait for release."
        elif code == "no_agent":
            line = f"No agent is connected, so {cards} cannot start."
        elif code == "project_limit":
            line = f"Agent limit reached ({used} of {cap} working): {cards} wait for a free slot."
        else:
            line = f"{cards} wait for checks to finish."
        lines.append({"code": code, "count": n, "text": line})
    return lines
