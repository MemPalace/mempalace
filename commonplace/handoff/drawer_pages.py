"""Request-local physical membership for logical drawer pages."""

from commonplace.rules import checks
from mempalace.read_scope import PAGE_SIZE, valid_descriptors

from .mirror_access import reader, unreadable_room
from .overview import _taxonomy, _valid_cursor


class PageArgumentError(ValueError):
    """The worker rejected a public drawer-page argument."""

    def __init__(self, error):
        super().__init__(error["message"])
        self.error = error


def _prepare_page(principal, workers, args, timeout):
    """Use the worker's normal validation without reading any drawer rows."""
    from commonplace.gateway import _tool_result

    response = workers.post(
        principal,
        {
            "jsonrpc": "2.0",
            "id": "commonplace-page-validation",
            "method": "tools/call",
            "params": {
                "name": "mempalace_list_drawers",
                "arguments": {**args, "thread_scope": {"allow": [], "timeout": timeout}},
            },
        },
    )
    error = response.get("error") if isinstance(response, dict) else None
    if (
        isinstance(error, dict)
        and error.get("code") == -32602
        and isinstance(error.get("message"), str)
    ):
        raise PageArgumentError(error)
    return _tool_result(response)


def project(gateway, principal, workers, args, *, timeout=660.0):
    empty = {"drawers": [], "total": 0, "count": 0, "offset": 0, "limit": 20}
    try:
        # Keep a safe envelope even if acquisition fails before validation.
        for key, minimum, maximum in (("offset", 0, None), ("limit", 1, 100)):
            value = args.get(key)
            if type(value) is int:
                empty[key] = (
                    max(minimum, value) if maximum is None else max(minimum, min(maximum, value))
                )
        if not getattr(principal, "workspace_id", None):
            return empty
        with checks.deadline(timeout):
            prepared = _prepare_page(principal, workers, args, checks.remaining(timeout))
            if (
                isinstance(prepared, dict)
                and set(prepared) == {"error"}
                and isinstance(prepared["error"], str)
            ):
                return prepared
            if (
                not isinstance(prepared, dict)
                or set(prepared) != set(empty)
                or prepared["drawers"] != []
                or prepared["count"] != 0
                or prepared["total"] != 0
                or type(prepared["offset"]) is not int
                or prepared["offset"] < 0
                or type(prepared["limit"]) is not int
                or not 1 <= prepared["limit"] <= 100
            ):
                return empty
            empty = prepared
            context = getattr(gateway, "handoff", None)
            if context is not None:
                context.store_for(principal.workspace_id)
            access = reader(gateway, principal)
            after, seen, allowed, decisions = None, set(), [], {}
            while True:
                page = workers.call_tool(
                    principal,
                    "mempalace_get_taxonomy",
                    {
                        "thread_scope": {
                            "probe": True,
                            "after": after,
                            "timeout": checks.remaining(timeout),
                        }
                    },
                )
                if not isinstance(page, dict) or set(page) != {"taxonomy", "probe"}:
                    return empty
                probe = page["probe"]
                if (
                    not isinstance(probe, dict)
                    or set(probe) != {"rows", "next"}
                    or not valid_descriptors(probe["rows"])
                ):
                    return empty
                rows, cursor = probe["rows"], probe["next"]
                if not _valid_cursor(cursor, after, rows) or page["taxonomy"] != _taxonomy(rows):
                    return empty
                for row in rows:
                    checks.remaining(0)
                    if row["id"] in seen:
                        return empty
                    seen.add(row["id"])
                    pair = (row["wing"], row["room"])
                    if pair not in decisions:
                        decisions[pair] = principal.policy.can_read_wing(
                            pair[0]
                        ) and not unreadable_room(access, *pair)
                    if decisions[pair]:
                        allowed.append(row)
                if cursor is None:
                    break
                after = cursor
            result = workers.call_tool(
                principal,
                "mempalace_list_drawers",
                {
                    **args,
                    "thread_scope": {
                        "allow": [
                            allowed[n : n + PAGE_SIZE] for n in range(0, len(allowed), PAGE_SIZE)
                        ],
                        "timeout": checks.remaining(timeout),
                    },
                },
            )
            checks.remaining(0)
            if (
                isinstance(result, dict)
                and set(result) == {"error"}
                and isinstance(result["error"], str)
            ):
                return result
            if not isinstance(result, dict) or set(result) != set(empty):
                return empty
            return result
    except PageArgumentError:
        raise
    except Exception:
        return empty


def finish_page(original, cleaned):
    """Preserve proved totals after metadata scrubbing, never after row drops."""
    if not isinstance(original, dict) or "total" not in original or not isinstance(cleaned, dict):
        return cleaned
    before, after = original.get("drawers"), cleaned.get("drawers")
    if not isinstance(before, list) or not isinstance(after, list):
        return cleaned
    if len(before) != len(after):
        return {
            "drawers": [],
            "total": 0,
            "count": 0,
            "offset": original["offset"],
            "limit": original["limit"],
        }
    return {**cleaned, "total": original["total"]} if "total" not in cleaned else cleaned
