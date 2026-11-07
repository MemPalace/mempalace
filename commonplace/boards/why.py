"""Pure, actor-independent reasons a card cannot admit a new agent."""

from .display import _waiting_on_person

CODES = ("blocked_by", "waits_on_person", "not_released", "project_limit")


def text(reason: dict) -> str:
    code = reason["code"]
    if code == "blocked_by":
        return "Waiting on " + ", ".join(
            f"#{b['number']}: {b['title']}" for b in reason["blockers"]
        )
    if code == "waits_on_person":
        return "Waiting on a person"
    if code == "not_released":
        return "Waiting for release" + (": split stuck" if reason.get("split_stuck") else "")
    if code == "project_limit":
        return f"Waiting for a free slot: {reason['used']} of {reason['cap']}"
    raise ValueError("unknown waiting reason")


def waiting(row, *, blockers, released: bool, usage) -> dict | None:
    row = dict(row)
    if row["status"] == "done":
        return None
    if blockers:
        reason = {"code": "blocked_by", "blockers": list(blockers)}
    elif _waiting_on_person(row):
        reason = {"code": "waits_on_person"}
    elif not released and row["status"] in ("todo", "backlog"):
        reason = {"code": "not_released"}
        if row.get("split_stuck"):
            reason["split_stuck"] = True
    elif (
        row["status"] in ("todo", "backlog")
        and usage is not None
        and usage.cap is not None
        and usage.used >= usage.cap
    ):
        reason = {"code": "project_limit", "used": usage.used, "cap": usage.cap}
    else:
        return None
    return {**reason, "text": text(reason)}
