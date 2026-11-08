"""Complete request-local overview projection, shared with search fan-out."""

from commonplace.rules import checks
from mempalace.read_scope import PAGE_SIZE, valid_descriptors

from .mirror_access import reader, unreadable_room


def _taxonomy(rows):
    result = {}
    for row in rows:
        rooms = result.setdefault(row["wing"], {})
        rooms[row["room"]] = rooms.get(row["room"], 0) + 1
    return result


def _valid_map(value, *, wings):
    def count(n):
        return type(n) is int and n > 0

    return isinstance(value, dict) and all(
        isinstance(w, str)
        and (
            count(rooms)
            if wings
            else isinstance(rooms, dict)
            and bool(rooms)
            and all(isinstance(r, str) and count(n) for r, n in rooms.items())
        )
        for w, rooms in value.items()
    )


def _valid_cursor(cursor, after, rows):
    if after is not None and after.startswith("id:") and any(r["id"] <= after[3:] for r in rows):
        return False
    if cursor is None:
        return True
    if not isinstance(cursor, str) or cursor == after:
        return False
    if cursor.startswith("id:"):
        return (
            bool(cursor[3:])
            and (after is None or after.startswith("id:") and cursor > after)
            and all(r["id"] <= cursor[3:] and (after is None or r["id"] > after[3:]) for r in rows)
        )
    if cursor.startswith("offset:"):
        try:
            prior = int(after[7:]) if after and after.startswith("offset:") else 0
            return (after is None or after.startswith("offset:")) and int(
                cursor[7:]
            ) == prior + PAGE_SIZE
        except ValueError:
            return False
    return False


def _pair_payload(value, worker_tool):
    # Only internal status pair pages carry these transport decorations. Drop
    # exactly that vocabulary before applying the unchanged strict validator.
    if worker_tool == "mempalace_status" and isinstance(value, dict):
        return {
            k: v
            for k, v in value.items()
            if k not in {"sqlite_integrity", "library_versions", "updates"}
        }
    return value


def project(gateway, principal, workers, name, *, timeout=660.0, worker_tool=None):
    """Return only current readable sums, or the same shape as empty visibility.

    No public gateway recursion and no process-wide authorization cache. Raw
    worker calls always carry the authenticated principal's workspace. A fault
    anywhere discards all pages, including sums already revalidated. Backup
    pauses propagate WorkerPaused so callers retain their retry contract.
    """
    from commonplace.gateway import WorkerPaused

    wings = name == "mempalace_list_wings"
    key = "wings" if wings else "taxonomy"
    empty = {key: {}}
    if not getattr(principal, "workspace_id", None):
        return empty
    try:
        # Deadline-bound readers only reuse an open store. Initialize the
        # caller's workspace before constructing that request-local reader.
        context = getattr(gateway, "handoff", None)
        if context is not None:
            context.store_for(principal.workspace_id)
        with checks.deadline(timeout):
            access = reader(gateway, principal)
            decisions, seen, allowed = {}, set(), []
            after = None
            while True:
                page = workers.call_tool(
                    principal,
                    worker_tool or "mempalace_get_taxonomy",
                    {
                        "thread_scope": {
                            "probe": True,
                            "after": after,
                            "timeout": checks.remaining(timeout),
                        },
                    },
                )
                page = _pair_payload(page, worker_tool)
                if (
                    not isinstance(page, dict)
                    or set(page) != {"taxonomy", "probe"}
                    or not _valid_map(page["taxonomy"], wings=False)
                ):
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
                    pair = (principal.workspace_id, row["wing"], row["room"])
                    if pair not in decisions:
                        wing, room = row["wing"], row["room"]
                        visible = principal.policy.can_read_wing(wing) and not unreadable_room(
                            access, wing, room
                        )
                        decisions[pair] = bool(visible)
                    if decisions[pair]:
                        allowed.append(row)
                if cursor is None:
                    break
                after = cursor
            output = {}
            for start in range(0, len(allowed), PAGE_SIZE):
                batch = allowed[start : start + PAGE_SIZE]
                result = workers.call_tool(
                    principal,
                    worker_tool or name,
                    {
                        "thread_scope": {"allow": batch, "timeout": checks.remaining(timeout)},
                    },
                )
                result = _pair_payload(result, worker_tool)
                if (
                    not isinstance(result, dict)
                    or set(result) != {key}
                    or not _valid_map(result[key], wings=wings)
                ):
                    return empty
                ceiling = _taxonomy(batch)
                for wing, rooms in result[key].items():
                    if wing not in ceiling:
                        return empty
                    if wings:
                        if rooms > sum(ceiling[wing].values()):
                            return empty
                        output[wing] = output.get(wing, 0) + rooms
                    else:
                        dest = output.setdefault(wing, {})
                        for room, n in rooms.items():
                            if n > ceiling[wing].get(room, 0):
                                return empty
                            dest[room] = dest.get(room, 0) + n
            checks.remaining(0)
            return {key: output}
    except WorkerPaused:
        # Preserve the gateway retry contract instead of reporting zero counts.
        raise
    except Exception:
        return empty


def summary(gateway, principal, workers, name, wing=None, *, timeout=660.0):
    """Room/status consumers of the same complete authorized pair snapshot."""
    from commonplace.gateway import WorkerPaused
    from mempalace.config import sanitize_name, wing_aliases

    if wing is not None:
        try:
            wing = (
                None if isinstance(wing, str) and not wing.strip() else sanitize_name(wing, "wing")
            )
        except ValueError as exc:
            return {"error": str(exc)}
    status = name == "mempalace_status"
    empty = (
        {"total_drawers": 0, "wings": {}, "rooms": {}}
        if status
        else {"wing": wing or "all", "rooms": {}}
    )
    if not getattr(principal, "workspace_id", None):
        return empty
    try:
        with checks.deadline(timeout):
            taxonomy = project(
                gateway,
                principal,
                workers,
                "mempalace_get_taxonomy",
                timeout=checks.remaining(timeout),
                worker_tool="mempalace_status" if status else None,
            )["taxonomy"]
            selected = set(wing_aliases(wing, taxonomy)) if wing else set(taxonomy)
            rooms, wings = {}, {}
            for w, counts in taxonomy.items():
                if not status and w not in selected:
                    continue
                wings[w] = sum(counts.values())
                for room, n in counts.items():
                    rooms[room] = rooms.get(room, 0) + n
            if not status:
                checks.remaining(0)
                return {"wing": wing or "all", "rooms": rooms}
            diagnostics = workers.call_tool(
                principal,
                name,
                {"thread_scope": {"allow": [], "timeout": checks.remaining(timeout)}},
            )
            diagnostics = _pair_payload(diagnostics, name)
            # Only a fixed diagnostic vocabulary crosses this boundary. Counts
            # come exclusively from the current authorized identity snapshot.
            keys = {
                "protocol",
                "aaak_dialect",
                "backend",
                "embedding_device",
                "vector_disabled",
                "vector_disabled_reason",
                "sqlite_integrity_failed",
                "error",
                "partial",
            }
            if not isinstance(diagnostics, dict) or set(diagnostics) - keys:
                return empty
            if diagnostics.get("vector_disabled") is True:
                diagnostics = {
                    **diagnostics,
                    "vector_disabled_reason": "Vector index unavailable; repair required.",
                }
            checks.remaining(0)
            return {
                **diagnostics,
                "total_drawers": sum(wings.values()),
                "wings": wings,
                "rooms": rooms,
            }
    except WorkerPaused:
        # Preserve the gateway retry contract instead of reporting zero counts.
        raise
    except Exception:
        return empty
