"""Request-local hallway authorization before sorting, counting and paging."""

from commonplace.rules import checks
from mempalace.hallway_scope import PAGE_SIZE, descriptor

from .hallway_fields import public_row
from .mirror_access import hallway_visible, reader


def _strength(record):
    try:
        return -int(record.get("co_occurrence_count") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0


def project(gateway, principal, workers, args, *, timeout=660.0):
    limit, offset = args.get("limit", 100), args.get("offset", 0)
    empty = {"hallways": [], "total": 0, "count": 0, "offset": offset, "limit": limit}
    if not getattr(principal, "workspace_id", None):
        return empty
    try:
        gateway.handoff.store_for(principal.workspace_id)
        with checks.deadline(timeout):
            access = reader(gateway, principal)
            allowed, after = [], 0
            while True:
                checks.remaining(0)
                reply = workers.call_tool(
                    principal,
                    "mempalace_list_hallways",
                    {
                        **args,
                        "thread_scope": {"probe": True, "after": after},
                    },
                )
                probe = reply.get("probe") if isinstance(reply, dict) else None
                if not isinstance(probe, dict) or set(probe) != {"rows", "next"}:
                    return empty
                rows, cursor = probe["rows"], probe["next"]
                if (
                    not isinstance(rows, list)
                    or len(rows) > PAGE_SIZE
                    or any(
                        not isinstance(r, dict)
                        or set(r) != {"position", "record"}
                        or type(r["position"]) is not int
                        or r["position"] <= after
                        or not isinstance(r["record"], dict)
                        or descriptor(r["record"]) != r["record"]
                        for r in rows
                    )
                ):
                    return empty
                positions = [r["position"] for r in rows]
                if positions != sorted(set(positions)):
                    return empty
                if cursor is not None and (
                    type(cursor) is not int or len(rows) != PAGE_SIZE or cursor != positions[-1]
                ):
                    return empty
                allowed.extend(
                    r for r in rows if hallway_visible(r["record"], access, principal.policy)
                )
                if cursor is None:
                    break
                after = cursor
            records = []
            for start in range(0, len(allowed), PAGE_SIZE):
                checks.remaining(0)
                batch = allowed[start : start + PAGE_SIZE]
                reply = workers.call_tool(
                    principal,
                    "mempalace_list_hallways",
                    {
                        **args,
                        "thread_scope": {"allow": batch},
                    },
                )
                rows = reply.get("hallways") if isinstance(reply, dict) else None
                if not isinstance(rows, list) or len(rows) > len(batch):
                    return empty
                # A worker may omit changed rows, but cannot add unproved rows
                # or reorder stable ties within the authorized storage sequence.
                remaining = iter(batch)
                for row in rows:
                    if not isinstance(row, dict) or not any(
                        item["record"] == descriptor(row) for item in remaining
                    ):
                        return empty
                records.extend(
                    public_row(row)
                    for row in rows
                    if hallway_visible(row, access, principal.policy)
                )
            checks.remaining(0)
            records.sort(key=_strength)
            page = records[max(0, offset) : max(0, offset) + max(0, limit)]
            return {**empty, "hallways": page, "total": len(records), "count": len(page)}
    except Exception:
        return empty
