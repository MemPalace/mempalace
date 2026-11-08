"""Deterministic speech from the already scoped Needs you queue."""

from xml.sax.saxutils import escape

from .display import NEEDS

_COUNTS = (
    "Zero One Two Three Four Five Six Seven Eight Nine Ten Eleven Twelve Thirteen "
    "Fourteen Fifteen Sixteen Seventeen Eighteen Nineteen Twenty"
).split()


def digest(groups):
    """Reuse queue membership and card needs; never rewrite a card's words."""
    cards = {
        c["number"]: c for bucket in groups.values() if isinstance(bucket, list) for c in bucket
    }
    # An owner token's cards never say need:you. The existing addressed-ask
    # buckets still identify the owner's urgent questions without redoing
    # recipient or wing policy here. Keep each card's derived need intact.
    asks = {
        c["number"] for key in ("asks", "done_review") for c in groups.get(key, []) if c.get("ask")
    }
    ordered = sorted(
        cards.values(),
        key=lambda c: (0 if c["number"] in asks else NEEDS.index(c["need"]), c["number"]),
    )
    count = len(ordered)
    items = [{k: c[k] for k in ("number", "title", "need", "board")} for c in ordered[:5]]
    for item, c in zip(items, ordered):
        if c.get("approval_kind") in ("origin", "origin_configuration"):
            item.update({k: c[k] for k in ("why", "approval_kind", "actions", "can_approve")})
    remaining = count - len(items)
    text = _text(count, items, remaining)
    return {"text": text, "count": count, "items": items, "remaining": remaining}


def _text(count, items, remaining, encode=str):
    if not count:
        return "Nothing needs you today."
    total = _COUNTS[count] if count < len(_COUNTS) else str(count)
    lines = ["One thing needs you." if count == 1 else f"{total} things need you."]
    lines.extend(f"{encode(item['title'])} on {encode(item['board'])}." for item in items)
    if remaining:
        lines.append(f"And {remaining} more.")
    return "\n".join(lines)


def ssml(result):
    """The same words as plain text, with user content escaped as XML text."""

    def xml(value):
        return escape(value, {'"': "&quot;", "'": "&apos;"})

    text = _text(result["count"], result["items"], result["remaining"], encode=xml)
    return f"<speak>{text}</speak>"
