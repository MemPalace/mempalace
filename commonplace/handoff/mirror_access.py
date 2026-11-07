"""Apply thread read boundaries to every outgoing mirror drawer."""

import re

from commonplace.policy import canonical_wing
from commonplace.rules import checks

from . import provenance_refs
from .models import MIRROR_WING
from .recall import Recall

_DROP = object()
_THREAD_ROOM = re.compile(r"thr_[0-9a-f]+\Z")


class _Reader:
    def __init__(self, gateway, principal):
        self.gateway, self.principal = gateway, principal
        self.allowed, self.parsed, self.private = {}, {}, {}

    def __call__(self, room):
        if room not in self.allowed:
            self.allowed[room] = False
            try:
                ctx = self.gateway.handoff
                recall = ctx.recall
                if checks._deadline.get() is not None:
                    checks.remaining(0)
                    store = ctx._stores.get(self.principal.workspace_id)
                    if store is None:
                        return False
                    recall = Recall(lambda _workspace: store, private=recall._private)
                self.allowed[room] = recall.can_read(self.principal, room)
            except Exception:
                pass
        return self.allowed[room]

    def parse(self, value):
        if not isinstance(value, str):
            return provenance_refs.refs(value)
        if value not in self.parsed:
            self.parsed[value] = provenance_refs.refs(value)
        return self.parsed[value]

    def reference(self, token):
        if token.lower().startswith("thr_"):
            return self(token)
        mode = "exact" if "#sha256:" in token.lower() else "family"
        key = (self.principal.workspace_id, mode, token)
        if key not in self.private:
            self.private[key] = False
            try:
                ctx = self.gateway.handoff
                checks.remaining(0)
                store = (
                    ctx._stores.get(self.principal.workspace_id)
                    if checks._deadline.get() is not None
                    else ctx.store_for(self.principal.workspace_id)
                )
                if store is None:
                    return False
                if mode == "exact":
                    found = store.by_source(self.principal.workspace_id, token)
                    rows = [found] if found is not None else []
                else:
                    rows = store.by_source_family(self.principal.workspace_id, token)
                self.private[key] = any(self(row.id) for row in rows)
            except Exception:
                pass
        return self.private[key]


def reader(gateway, principal):
    """Access and provenance caches live for one request/response only."""
    return _Reader(gateway, principal)


def unreadable_room(can_read, wing, room):
    mirror = isinstance(wing, str) and canonical_wing(wing) == MIRROR_WING
    thread = isinstance(room, str) and _THREAD_ROOM.fullmatch(room)
    return bool((mirror or thread) and (not isinstance(room, str) or not can_read(room)))


def fact_visible(candidate, can_read):
    return (
        _scrub_fact(
            {
                "wing": candidate["wing"],
                "fact": "",
                "sources": [{"source_thread": t} for t in candidate["threads"]],
            },
            can_read,
        )
        is not _DROP
    )


def valid_fact_page(page, after=None):
    from mempalace.knowledge_graph import valid_fact_entries

    if (
        not isinstance(page, dict)
        or set(page) != {"facts", "next"}
        or not valid_fact_entries(page["facts"])
    ):
        return False
    ids = [row["id"] for row in page["facts"]]
    if ids != sorted(set(ids)) or (after is not None and any(i <= after for i in ids)):
        return False
    next_id = page["next"]
    return next_id is None or (
        isinstance(next_id, str)
        and bool(ids)
        and next_id == ids[-1]
        and (after is None or next_id > after)
    )


def filter_room_keys(rule, result, can_read):
    if not isinstance(result, dict):
        return result
    if rule == "wing_list" and isinstance(result.get("taxonomy"), dict):
        taxonomy = {}
        for wing, rooms in result["taxonomy"].items():
            if not isinstance(rooms, dict):
                taxonomy[wing] = rooms
                continue
            kept = {
                r: count for r, count in rooms.items() if not unreadable_room(can_read, wing, r)
            }
            if kept or not rooms:
                taxonomy[wing] = kept
        return result if taxonomy == result["taxonomy"] else {**result, "taxonomy": taxonomy}
    if rule == "read_wing" and isinstance(result.get("rooms"), dict):
        rooms = {
            r: count
            for r, count in result["rooms"].items()
            if not (_THREAD_ROOM.fullmatch(r) and not can_read(r))
        }
        return result if rooms == result["rooms"] else {**result, "rooms": rooms}
    return result


def scrub_provenance(node, access):
    out = node
    for key in provenance_refs.PROVENANCE_FIELDS | provenance_refs.DERIVED_FIELDS:
        value = node.get(key)
        if not isinstance(value, str):
            continue
        parsed = access.parse(value)
        if parsed is provenance_refs.REFUSE or not all(access.reference(t) for t in parsed.tokens):
            if out is node:
                out = dict(node)
            out.pop(key, None)
    return out


def gate(gateway, principal, rule, result):
    """Scrub mirror words, caching access decisions only within this response."""
    can_read = reader(gateway, principal)

    def scrub(node):
        if isinstance(node, dict):
            fact = _is_fact(node)
            if fact:
                node = _scrub_fact(node, can_read, can_read)
                if node is _DROP:
                    return _DROP
            node = scrub_provenance(node, can_read)
            if unreadable_room(can_read, None if fact else node.get("wing"), node.get("room")):
                return _DROP
            if "connected_wing" in node and unreadable_room(
                can_read, node.get("connected_wing"), node.get("connected_room")
            ):
                return _DROP
            # Preview metadata belongs to the drawer, not to the endpoint.
            if (
                "drawer_preview" in node or (node.get("drawer_id") and "connected_wing" in node)
            ) and (
                not isinstance(node.get("drawer_wing"), str)
                or not node["drawer_wing"].strip()
                or not isinstance(node.get("drawer_room"), str)
                or not node["drawer_room"].strip()
                or unreadable_room(can_read, node["drawer_wing"], node["drawer_room"])
            ):
                return _DROP
            out = {}
            for key, value in node.items():
                cleaned = scrub(value)
                if cleaned is _DROP:
                    return _DROP
                out[key] = cleaned
            out = _clean_related(node, out)
            if len(out) == len(node) and all(out[k] is v for k, v in node.items()):
                return node
            _recount(node, out)
            return out
        if isinstance(node, list):
            out = [cleaned for item in node if (cleaned := scrub(item)) is not _DROP]
            return node if len(out) == len(node) and all(a is b for a, b in zip(out, node)) else out
        return node

    def missing(node):
        drawer_id = node.get("drawer_id") if isinstance(node, dict) else None
        return {"error": f"Drawer not found: {drawer_id}"}

    if rule in ("drawer_read", "search", "read_wing") and not isinstance(result, (dict, list)):
        return missing(result) if rule == "drawer_read" else {"error": "not found", "denied": True}
    if (
        rule == "drawer_read"
        and isinstance(result, dict)
        and isinstance(result.get("results"), list)
    ):
        items = []
        errors = 0
        for item in result["results"]:
            cleaned = scrub(item)
            if cleaned is _DROP:
                cleaned = {"drawer_id": item.get("drawer_id"), **missing(item)}
            errors += bool(isinstance(cleaned, dict) and cleaned.get("error"))
            items.append(cleaned)
        if all(a is b for a, b in zip(items, result["results"])):
            return _filter_fact_links(result)
        out = {**result, "results": items, "count": len(items), "errors": errors}
        out.pop("total", None)
        return _filter_fact_links(out)
    result = filter_room_keys(rule, result, can_read)
    cleaned = scrub(result)
    if cleaned is _DROP:
        return _dropped_result(rule, result)
    return _filter_fact_links(cleaned)


def _recount(original, cleaned):
    for key in ("drawers", "results"):
        if isinstance(cleaned.get(key), list) and cleaned[key] is not original.get(key):
            cleaned.pop("total", None)
            if "count" in cleaned:
                cleaned["count"] = len(cleaned[key])


def _is_fact(node):
    return bool({"predicate", "object", "fact", "triple_id"}.intersection(node))


def _thread_id(value):
    return isinstance(value, str) and _THREAD_ROOM.fullmatch(value) is not None


def _fact_sources(node):
    entries = []
    source = node.get("source")
    if isinstance(source, dict):
        entries.append((source.get("thread"), source))
    sources = node.get("sources")
    if isinstance(sources, list):
        entries.extend((row.get("source_thread"), row) for row in sources if isinstance(row, dict))
    if "source_thread" in node:
        entries.append((node["source_thread"], node))
    return entries


def _scrub_fact(node, can_read, access=None):
    original = node
    sources = _fact_sources(original)
    private = any(
        isinstance(original.get(k), str) and canonical_wing(original[k]) == MIRROR_WING
        for k in ("wing", "theme")
    )
    primary = original.get("source_thread")
    if primary is None and _thread_id(original.get("origin_id")):
        primary = original["origin_id"]
    if access is not None:
        node = scrub_provenance(node, access)
        if isinstance(node.get("sources"), list):
            cleaned = [
                scrub_provenance(row, access) if isinstance(row, dict) else row
                for row in node["sources"]
            ]
            if any(a is not b for a, b in zip(cleaned, node["sources"])):
                node = {**node, "sources": cleaned}
    if not private and not any(_thread_id(thread) for thread, _ in sources):
        return node
    readable = {thread for thread, _ in sources if _thread_id(thread) and can_read(thread)}
    if not readable:
        return _DROP
    out = dict(node)
    source = original.get("source")
    if "source" in original and (
        not isinstance(source, dict)
        or not _thread_id(source.get("thread"))
        or source["thread"] not in readable
    ):
        out.pop("source", None)
    if isinstance(original.get("sources"), list):
        out["sources"] = [
            clean
            for old, clean in zip(original["sources"], node["sources"])
            if isinstance(old, dict)
            and _thread_id(old.get("source_thread"))
            and old["source_thread"] in readable
        ]
    if primary is not None and (not _thread_id(primary) or primary not in readable):
        for key in (
            "source_thread",
            "source_seq_start",
            "source_seq_end",
            "source_url",
            "source_link",
            "source_drawer_id",
            "source_file",
            "source_closet",
            "origin_id",
            "origin_link",
        ):
            out.pop(key, None)
    return node if out == node else out


def _clean_related(original, out):
    related = original.get("related_facts")
    if not isinstance(related, list) or len(out.get("related_facts", [])) == len(related):
        return out
    kept = {
        row.get("id") or row.get("triple_id")
        for row in out["related_facts"]
        if isinstance(row, dict)
    }
    superseded = original.get("superseded")
    if isinstance(superseded, list):
        out["superseded"] = [key for key in superseded if key in kept]
        previous = original.get("previous")
        if isinstance(previous, list) and len(previous) == len(superseded):
            out["previous"] = [value for key, value in zip(superseded, previous) if key in kept]
        else:
            out.pop("previous", None)
    else:
        out.pop("previous", None)
    if original.get("current") not in kept:
        out.pop("current", None)
    return out


def _dicts(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from _dicts(value)


def _filter_fact_links(node):
    kept = {
        value
        for row in _dicts(node)
        if {"object", "predicate", "fact"}.intersection(row)
        and isinstance(value := row.get("id") or row.get("triple_id"), str)
    }

    def prune(value):
        if isinstance(value, dict):
            out = {
                key: prune(child)
                for key, child in value.items()
                if key != "superseded_by" or isinstance(child, str) and child in kept
            }
            return (
                value
                if len(out) == len(value) and all(out[key] is child for key, child in value.items())
                else out
            )
        if isinstance(value, list):
            out = [prune(child) for child in value]
            return value if all(a is b for a, b in zip(out, value)) else out
        return value

    return prune(node)


def _dropped_result(rule, result):
    if (
        rule == "kg_write"
        and isinstance(result, dict)
        and result.get("success") is True
        and not result.get("error")
    ):
        return {key: result[key] for key in ("success", "triple_id", "action") if key in result}
    if rule == "drawer_read":
        drawer_id = result.get("drawer_id") if isinstance(result, dict) else None
        return {"error": f"Drawer not found: {drawer_id}"}
    return {"error": "not found", "denied": True}
