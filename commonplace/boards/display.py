"""Card links and readable names for people, added on the way out.

Cards store actor refs (``user:<id>``, ``conn:<name>``). Replies show a
person by name (or email) and every card carries its short link.
"""

import re
from datetime import datetime, timedelta, timezone

_USER = re.compile(r"\buser:(usr_[0-9a-f]+)\b")

# What a card needs from the person reading it, strongest first. ``done`` is
# terminal and wins over everything; the rest apply in this order.
NEEDS = ("you", "plan", "merge", "checks", "expired", "working", "idle", "done")
STALE_MINUTES = 30


def stale_before(at=None) -> str:
    """ISO cutoff: a doing card last seen before this has a stale heartbeat."""
    at = at or datetime.now(timezone.utc)
    return (at - timedelta(minutes=STALE_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_ready(card, cutoff) -> bool:
    """A fresh, explicit holder report; legacy or incomplete reports fail closed."""
    head = card.get("pr_head")
    return bool(
        card["status"] in ("doing", "review")
        and card.get("pr_url")
        and card.get("pr_state") == "open"
        and isinstance(head, str)
        and re.fullmatch(r"[0-9a-f]{40}([0-9a-f]{24})?", head)
        and card.get("checks") == "passing"
        and card.get("pr_reported_at")
        and card["pr_reported_at"] > cutoff
    )


def need(card, *, me=None, owner=False, now=None, person_release: bool | None = None) -> str:
    """One of ``NEEDS`` for a public card dict, seen by actor ``me``.

    An ask with no recipient goes to the workspace owner. A question to someone
    else is not a plan to approve. ``now`` is a datetime (default: the server clock).
    """
    status = card["status"]
    if status == "done":
        return "done"
    if card.get("proposal_status") == "pending":
        return (
            "you"
            if card.get("proposed_to") == me or (owner and person_release is not False)
            else "plan"
        )
    ask_to = card.get("ask_to")
    if card.get("ask") and (ask_to == me if ask_to else owner):
        return "you"
    if "plan" in (card.get("labels") or ()) or (status == "review" and not card.get("ask")):
        return "plan"
    cutoff = stale_before(now)
    if merge_ready(card, cutoff):
        return "merge"
    if card.get("checks") == "pending":
        return "checks"
    if status == "doing" and card.get("claim_until"):
        at = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
        if card["claim_until"] <= at:
            return "expired"
    if status == "doing" and (card.get("heartbeat_at") or card["updated_at"]) >= cutoff:
        return "working"
    return "idle"


def need_for(card, principal, now=None, *, person_release: bool | None = None) -> str:
    """``need`` as ``principal`` sees it: only a signed-in person is ever ``you``."""
    person = bool(principal and principal.person)
    return need(
        card,
        me=principal.actor
        if principal and (person or card.get("proposal_status") == "pending")
        else None,
        owner=person and principal.role == "owner",
        now=now,
        person_release=person_release,
    )


def card_url(ctx, number) -> str:
    path = f"/app/#/card/{number}"
    try:
        from commonplace import urls
    except ModuleNotFoundError as exc:
        if exc.name != "commonplace.urls":
            raise
        import os

        base = os.environ.get("COMMONPLACE_APP_URL") or os.environ.get(
            "COMMONPLACE_BASE_URL", "http://127.0.0.1:8765"
        )
        return base.rstrip("/") + path
    return urls.share_link(ctx.db, ctx.principal.workspace_id, path)


def with_urls(ctx, value):
    """Every card dict in ``value`` (nested) gains ``url``."""
    if isinstance(value, list):
        return [with_urls(ctx, v) for v in value]
    if isinstance(value, dict):
        out = {k: with_urls(ctx, v) for k, v in value.items()}
        if isinstance(out.get("number"), int) and "status" in out and "board" in out:
            out.setdefault("url", card_url(ctx, out["number"]))
        return out
    return value


def people(ctx, text: str) -> str:
    """Replace ``user:<id>`` with that member's name or email."""
    if "user:" not in text:
        return text
    names = {}
    for member in ctx.db.members(ctx.principal.workspace_id):
        try:
            name = ctx.db.user(member["id"])["name"]
        except Exception:
            name = ""
        names[member["id"]] = name or member["email"]
    return _USER.sub(lambda m: names.get(m.group(1), m.group(0)), text)
