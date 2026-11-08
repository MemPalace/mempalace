"""Authorize graph inputs before aggregation, traversal, ranking and counts."""

import re

from commonplace.rules import checks
from mempalace import palace_graph
from mempalace.config import sanitize_name
from mempalace.graph_scope import graph_descriptor, valid_sort
from mempalace.read_scope import PAGE_SIZE, valid_descriptors

from .mirror_access import reader, unreadable_room
from .overview import _valid_cursor
from .tunnels import project as project_tunnels


def _result(name, args, rows, tunnels, *, grouped=False):
    graph = palace_graph._nodes_edges_from_grouped_rows(
        (m["room"], m["wing"], m["hall"], 1, m["date"]) for m in rows
    )
    if name == "mempalace_traverse":
        return palace_graph.traverse(
            args["start_room"], max_hops=args["max_hops"], graph_data=graph
        )
    if name == "mempalace_find_tunnels":
        return palace_graph.find_tunnels(args.get("wing_a"), args.get("wing_b"), graph_data=graph)
    if grouped:
        # Grouped stats have their own room-name tie break, independently of
        # the backend's traversal/discovery enumeration order.
        graph = (dict(sorted(graph[0].items())), graph[1])
    return palace_graph.graph_stats(graph_data=graph, explicit_tunnels=tunnels)


def empty_result(name, args):
    start = args.get("start_room")
    return _result(
        name, {"start_room": start if isinstance(start, str) else "", "max_hops": 2}, [], []
    )


class GraphArgumentError(ValueError):
    """A client validation error, rather than a projection failure."""


def validate_arguments(name, args, schema):
    """Apply the worker's graph schema before any metadata probe."""
    properties = schema["properties"]
    unknown = [key for key in args if key not in properties and key != "wait_for_previous"]
    if unknown:
        quoted = ", ".join(f"'{key}'" for key in unknown)
        word = "parameter" if len(unknown) == 1 else "parameters"
        raise GraphArgumentError(f"Unknown {word} {quoted} for tool {name}")
    checked = {}
    for key, value in args.items():
        if value is None or key == "wait_for_previous":
            continue
        kind = properties[key].get("type")
        if kind == "integer":
            try:
                if isinstance(value, float) and value.is_integer():
                    value = int(value)
                elif isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
                    value = int(value.strip())
            except (ValueError, OverflowError):
                raise GraphArgumentError(
                    f"Invalid params for tool {name}: '{key}' is too large"
                ) from None
            if isinstance(value, bool) or not isinstance(value, int):
                raise GraphArgumentError(
                    f"Invalid params for tool {name}: '{key}' must be an integer"
                )
        elif kind == "string":
            value = str(value) if type(value) is int else value
            if not isinstance(value, str):
                raise GraphArgumentError(
                    f"Invalid params for tool {name}: '{key}' must be a string"
                )
        checked[key] = value
    missing = [key for key in schema.get("required", []) if key not in checked]
    if missing:
        quoted = ", ".join(f"'{key}'" for key in missing)
        word = "parameter" if len(missing) == 1 else "parameters"
        raise GraphArgumentError(f"Missing required {word} {quoted} for tool {name}")
    return checked


def _arguments(name, args):
    args = dict(args)
    if name == "mempalace_traverse":
        if not isinstance(args.get("start_room"), str):
            raise ValueError("start_room must be a string")
        hops = args.get("max_hops", 2)
        if type(hops) is not int:
            raise ValueError("max_hops must be an integer")
        args["max_hops"] = max(1, min(hops, 10))
    elif name == "mempalace_find_tunnels":
        for key in ("wing_a", "wing_b"):
            value = args.get(key)
            if isinstance(value, str) and not value.strip():
                args[key] = None
            elif value is not None:
                args[key] = sanitize_name(value, key)
    return args


def project(gateway, principal, workers, name, args, *, timeout=660.0):
    try:
        args = _arguments(name, args)
    except ValueError as exc:
        return {"error": str(exc)}
    empty = _result(name, args, [], [])
    if not getattr(principal, "workspace_id", None):
        return empty
    try:
        gateway.handoff.store_for(principal.workspace_id)
        with checks.deadline(timeout):

            def visible(access, row):
                return principal.policy.can_read_wing(row["wing"]) and not unreadable_room(
                    access, row["wing"], row["room"]
                )

            access = reader(gateway, principal)
            after, allowed, seen = None, [], set()
            order = None
            while True:
                page = workers.call_tool(
                    principal,
                    name,
                    {
                        **args,
                        "thread_scope": {
                            "probe": True,
                            "after": after,
                            "timeout": checks.remaining(timeout),
                        },
                    },
                )
                if (
                    not isinstance(page, dict)
                    or set(page) != {"rows", "next", "order"}
                    or page["order"] not in ([], ["room", "wing", "hall"], ["wing", "room", "hall"])
                    or order is not None
                    and page["order"] != order
                    or not valid_descriptors(page["rows"])
                    or not _valid_cursor(page["next"], after, page["rows"])
                ):
                    return empty
                order = page["order"]
                for row in page["rows"]:
                    checks.remaining(0)
                    if row["id"] in seen:
                        return empty
                    seen.add(row["id"])
                    if visible(access, row):
                        allowed.append(row)
                after = page["next"]
                if after is None:
                    break
            access = reader(gateway, principal)
            allowed = [row for row in allowed if visible(access, row)]
            rows = []
            for start in range(0, max(1, len(allowed)), PAGE_SIZE):
                batch = allowed[start : start + PAGE_SIZE]
                page = workers.call_tool(
                    principal,
                    name,
                    {
                        **args,
                        "thread_scope": {"allow": batch, "timeout": checks.remaining(timeout)},
                    },
                )
                checks.remaining(0)
                if (
                    not isinstance(page, dict)
                    or set(page) != {"rows"}
                    or not isinstance(page["rows"], list)
                ):
                    return empty
                returned = []
                for item in page["rows"]:
                    if (
                        not isinstance(item, dict)
                        or set(item) != {"descriptor", "metadata", "sort"}
                        or not valid_sort(item["sort"])
                        or bool(order) != (item["sort"] is not None)
                        or item["descriptor"] not in batch
                        or item["descriptor"] in returned
                        or not isinstance(item["metadata"], dict)
                        or set(item["metadata"]) != {"wing", "room", "hall", "date"}
                        or any(not isinstance(v, str) for v in item["metadata"].values())
                        or any(
                            item["metadata"][k] != item["descriptor"][k] for k in ("wing", "room")
                        )
                    ):
                        return empty
                    if (
                        graph_descriptor(item["descriptor"]["id"], item["metadata"], item["sort"])
                        != item["descriptor"]
                    ):
                        return empty
                    returned.append(item["descriptor"])
                    rows.append({**item["metadata"], "_sort": item["sort"]})
            tunnels = []
            if name == "mempalace_graph_stats":
                tunnels = project_tunnels(
                    gateway,
                    principal,
                    workers,
                    "mempalace_list_tunnels",
                    {},
                    timeout=checks.remaining(timeout),
                    raise_on_failure=True,
                )
                if not isinstance(tunnels, list):
                    return empty
            checks.remaining(0)
            access = reader(gateway, principal)
            rows = [row for row in rows if visible(access, row)]
            if order:
                rows.sort(key=lambda m: m["_sort"])
            result = _result(name, args, rows, tunnels, grouped=bool(order))
            checks.remaining(0)
            return result
    except Exception:
        return empty
