"""Metadata-only worker replies for standalone mediator fixtures."""

from mempalace.read_scope import descriptor


def overview_result(name, args, rooms):
    """Answer raw, probe and revalidated overview calls for fixed room pairs."""
    if name not in {"mempalace_list_wings", "mempalace_get_taxonomy"}:
        return None
    rows = [
        descriptor(str(i), {"wing": wing, "room": room})
        for i, (wing, room) in enumerate(rooms.items())
    ]
    scope = args.get("thread_scope", {})
    if "allow" in scope:
        rows = [row for row in rows if row in scope["allow"]]
    taxonomy = {row["wing"]: {row["room"]: 1} for row in rows}
    if scope.get("probe"):
        assert scope["after"] is None
        return {"taxonomy": taxonomy, "probe": {"rows": rows, "next": None}}
    if name == "mempalace_list_wings":
        return {"wings": {wing: sum(counts.values()) for wing, counts in taxonomy.items()}}
    return {"taxonomy": taxonomy}
