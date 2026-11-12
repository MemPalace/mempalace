"""Request-local proof for explicit tunnel endpoints and drawer associations."""

from commonplace.rules import checks
from mempalace.read_scope import valid_descriptors
from mempalace.tunnel_scope import TUNNEL_PAGE, drawer_ids, valid_tunnel

from .mirror_access import reader, unreadable_room


def project(gateway, principal, workers, name, args, *, timeout=660.0, raise_on_failure=False):
    def failed():
        if raise_on_failure:
            raise ValueError("tunnel projection unavailable")
        return []

    if not getattr(principal, "workspace_id", None):
        return failed()
    from mempalace.config import sanitize_name

    try:
        for key in ("wing", "room") if name == "mempalace_follow_tunnels" else ("wing",):
            value = args.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{key} must be a non-empty string")
            if name != "mempalace_follow_tunnels" and not (value or "").strip():
                args[key] = None
            elif name == "mempalace_follow_tunnels" or args.get(key) is not None:
                args[key] = sanitize_name(args.get(key), key)
    except ValueError as exc:
        return {"error": str(exc)}
    try:
        gateway.handoff.store_for(principal.workspace_id)
        with checks.deadline(timeout):
            access = reader(gateway, principal)

            def visible(item, access):
                row = item["tunnel"]
                from .provenance_refs import _THREAD, _private_matches

                label = "" if row.get("label") is None else row["label"]
                if not isinstance(label, str) or any(
                    not access.reference(match[0])
                    for match in [*_THREAD.finditer(label), *_private_matches(label)]
                ):
                    return False
                pairs = [row[k] for k in ("source", "target")] + item["drawers"]
                if principal.policy.has_scope("memory:import:read") and any(
                    not access.preview_allowed(
                        {
                            "drawer_id": d["id"],
                            "connected_wing": d["wing"],
                            "drawer_wing": d["wing"],
                            "drawer_room": d["room"],
                        }
                    )
                    for d in item["drawers"]
                ):
                    return False
                return all(
                    principal.policy.can_read_wing(p["wing"])
                    and not unreadable_room(access, p["wing"], p["room"])
                    for p in pairs
                )

            after, allowed, seen = None, [], set()
            while True:
                result = workers.call_tool(
                    principal,
                    "mempalace_list_tunnels",
                    {
                        **({"wing": args["wing"]} if args.get("wing") else {}),
                        "thread_scope": {
                            "probe": True,
                            "after": after,
                            "timeout": checks.remaining(timeout),
                        },
                    },
                )
                if not isinstance(result, dict) or set(result) != {"probe"}:
                    return failed()
                page = result["probe"]
                if (
                    not isinstance(page, dict)
                    or set(page) != {"rows", "next"}
                    or not isinstance(page["rows"], list)
                    or len(page["rows"]) > TUNNEL_PAGE
                ):
                    return failed()
                for item in page["rows"]:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"tunnel", "drawers"}
                        or not valid_tunnel(item["tunnel"])
                        or not valid_descriptors(item["drawers"])
                        or set(drawer_ids([item["tunnel"]])) != {d["id"] for d in item["drawers"]}
                    ):
                        return failed()
                    checks.remaining(0)
                    key = item["tunnel"]["id"]
                    if key in seen:
                        return failed()
                    seen.add(key)
                    if visible(item, access):
                        allowed.append(item)
                cursor = page["next"]
                if cursor is None:
                    break
                if (
                    not isinstance(cursor, str)
                    or not cursor
                    or after is not None
                    and cursor <= after
                    or any(item["tunnel"]["id"] > cursor for item in page["rows"])
                ):
                    return failed()
                after = cursor
            # Do not reuse authority decisions across the probe/final boundary.
            access = reader(gateway, principal)
            allowed = [item for item in allowed if visible(item, access)]
            output = []
            current_access = None
            for start in range(0, max(1, len(allowed)), TUNNEL_PAGE):
                result = workers.call_tool(
                    principal,
                    name,
                    {
                        **args,
                        "thread_scope": {
                            "allow": allowed[start : start + TUNNEL_PAGE],
                            "timeout": checks.remaining(timeout),
                        },
                    },
                )
                checks.remaining(0)
                if not isinstance(result, list):
                    return failed()
                # Final rows carry the current label; authorize it again after
                # endpoint revalidation, before any public label is returned.
                # One reader serves every final page.
                current_access = current_access or reader(gateway, principal)
                if name == "mempalace_list_tunnels":
                    result = [
                        row
                        for row in result
                        if visible({"tunnel": row, "drawers": []}, current_access)
                    ]
                output.extend(result)
            return output
    except Exception:
        return failed()
