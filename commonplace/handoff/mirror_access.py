"""Apply thread read boundaries to every outgoing mirror drawer."""

import math
import unicodedata
from pathlib import Path

from commonplace.policy import canonical_wing
from commonplace.rules import checks

from . import provenance_refs
from .hallway_fields import NUMERIC_KEYS, public_row
from .models import MIRROR_WING
from .recall import Recall
from .thread_store import legacy_match_keys, room_match_value

_DROP = object()


class _ResponseAccess:
    """Live authority cached for one outgoing response, including startup clones."""

    def __init__(self, gateway, principal):
        self.gateway, self.principal = gateway, principal
        self.rooms, self.records, self.parsed, self.private = {}, {}, {}, {}

    def recall(self):
        ctx = self.gateway.handoff
        recall = ctx.recall
        if checks._deadline.get() is not None:
            checks.remaining(0)
            store = ctx._stores.get(self.principal.workspace_id)
            if store is None:
                return None
            recall = Recall(
                lambda _: store, private=recall._private, import_eligible=recall._import_eligible
            )
        return recall

    def can_read(self, room):
        if room not in self.rooms:
            self.rooms[room] = False
            try:
                recall = self.recall()
                self.rooms[room] = recall is not None and recall.can_read(self.principal, room)
            except Exception:
                pass
        return self.rooms[room]

    def ordinary_record(self, node):
        try:
            recall = self.recall()
            return recall is not None and recall.ordinary_record(self.principal, node.get("room"))
        except Exception:
            return False

    def preview_allowed(self, node):
        """A readable endpoint never proves the preview drawer's source range."""
        if "drawer_preview" not in node and not (
            node.get("drawer_id") and "connected_wing" in node
        ):
            return True
        wing, room = node.get("drawer_wing"), node.get("drawer_room")
        sensitive = (
            isinstance(wing, str) and canonical_wing(wing) in (MIRROR_WING, "private_chats")
        ) or _thread_id(room)
        return not sensitive or self.ordinary_record({"room": room})

    def import_allowed(self, node):
        key = id(node)
        if key not in self.records:
            self.records[key] = False
            try:
                recall = self.recall()
                self.records[key] = recall is not None and recall.import_record_allowed(
                    self.principal, node
                )
            except Exception:
                pass
        return self.records[key]

    def __call__(self, room):
        return self.can_read(room)

    def parse(self, value):
        if not isinstance(value, str):
            return provenance_refs.refs(value)
        if value not in self.parsed:
            self.parsed[value] = provenance_refs.refs(value)
        return self.parsed[value]

    def reference(self, token):
        if token.lower().startswith("thr_"):
            # Stored IDs are lower case; an upper-case spelling names the same thread.
            return self(token.lower())
        rows = self._source_rows(token)
        return bool(rows) and any(self(row.id) for row in rows)

    def exact_source_readable(self, value, names):
        """A value equal to a stored source ID (after NFKC), or naming an ``mcp:``
        remainder that a legacy (non-canonical) stored source resolves to, is
        shown only to readers of every such thread.

        Both are indexed store lookups, cached for this response. If a lookup
        fails, every value that is not a bare thread ID is hidden.
        """
        rows = self._lookup(
            ("value", unicodedata.normalize("NFKC", value)),
            lambda store, workspace: store.by_source_value(workspace, value),
        )
        legacy = self._lookup(
            ("legacy",),
            lambda store, workspace: [
                (key, thread)
                for thread, source in store.legacy_sources(workspace)
                for key in legacy_match_keys(source)
            ],
        )
        if rows is None or legacy is None:
            return provenance_refs.is_thread_id(value)
        threads = [row.id for row in rows]
        for name in names:
            if name.startswith("mcp:"):
                target = room_match_value(name)
                threads.extend(thread for key, thread in legacy if target.startswith(key))
        return all(self(thread) for thread in threads)

    def _lookup(self, key, fetch):
        """One cached store lookup; no handoff (or a workspace without threads)
        means no stored sources, and a failed or unreadable lookup is None."""
        if key not in self.private:
            self.private[key] = None
            try:
                ctx = getattr(self.gateway, "handoff", None)
                workspace = self.principal.workspace_id
                has_threads = getattr(ctx, "has_threads", None)
                if ctx is None or (
                    # A workspace that never stored a thread has no sources;
                    # never create its thread store just to look.
                    ctx._stores.get(workspace) is None
                    and has_threads is not None
                    and not has_threads(workspace)
                ):
                    self.private[key] = []
                else:
                    checks.remaining(0)
                    store = (
                        ctx._stores.get(self.principal.workspace_id)
                        if checks._deadline.get() is not None
                        else ctx.store_for(self.principal.workspace_id)
                    )
                    # Under a deadline an unloaded store of a workspace that has
                    # threads cannot be read in time: fail closed, like _source_rows.
                    if store is not None:
                        self.private[key] = fetch(store, self.principal.workspace_id)
            except Exception:
                pass
        return self.private[key]

    def source_readable(self, token):
        """A room naming source IDs is shown only to readers of every thread filed
        under any of them; naming no stored thread reveals none; faults hide it."""
        rows = self._source_rows(token, "prefix")
        return rows is not None and all(self(row.id) for row in rows)

    def _source_rows(self, token, mode=None):
        """Stored threads filed under a source ID in this workspace; None on fault."""
        mode = mode or ("exact" if "#sha256:" in token.lower() else "family")
        key = (self.principal.workspace_id, mode, token)
        if key not in self.private:
            self.private[key] = None
            try:
                ctx = self.gateway.handoff
                checks.remaining(0)
                store = (
                    ctx._stores.get(self.principal.workspace_id)
                    if checks._deadline.get() is not None
                    else ctx.store_for(self.principal.workspace_id)
                )
                if store is None:
                    return None
                if mode == "exact":
                    found = store.by_source(self.principal.workspace_id, token)
                    rows = [found] if found is not None else []
                elif mode == "prefix":
                    rows = store.by_source_prefix(self.principal.workspace_id, token)
                else:
                    rows = store.by_source_family(self.principal.workspace_id, token)
                self.private[key] = rows
            except Exception:
                pass
        return self.private[key]

    def association(self, kind, identifier):
        """Authorize current metadata in this workspace, then revalidate it."""
        try:
            checks.remaining(0)
            workers = self.gateway.mediator.workers
            args = {"kind": kind, "identifier": identifier}
            result = workers.call_tool(self.principal, "read_associations", args)
            rows = result.get("associations") if isinstance(result, dict) else None
            if not isinstance(rows, list) or not rows:
                return False
            if not self.association_rows_allowed(rows):
                return False
            checked = workers.call_tool(
                self.principal, "read_associations", {**args, "expect": rows}
            )
            return (
                isinstance(checked, dict)
                and checked.get("associations") == rows
                and self.association_rows_allowed(rows)
            )

        except Exception:
            return False

    def association_rows_allowed(self, rows):
        # Refresh authority even when the same room occurred earlier in this reply.
        self.rooms.clear()
        for row in rows:
            if not isinstance(row, dict):
                return False
            wing, room = row.get("wing"), row.get("room")
            if (
                not isinstance(wing, str)
                or not wing.strip()
                or not isinstance(room, str)
                or not room.strip()
                or not self.principal.policy.can_read_wing(wing)
                or unreadable_room(self, wing, room)
            ):
                return False
        return True


def reader(gateway, principal):
    """Access and provenance caches live for one request/response only."""
    return _ResponseAccess(gateway, principal)


def private_thread_reference(access, value):
    """A value naming any thread this reader cannot read is not shown to them."""
    names = provenance_refs.room_references(value)
    if names is None:
        return True
    exact = getattr(access, "exact_source_readable", None)
    if isinstance(value, str) and exact is not None and not exact(value, names):
        return True
    source = getattr(access, "source_readable", None)
    return any(
        not access(name) if name[:4].lower() == "thr_" else source is None or not source(name)
        for name in names
    )


def unreadable_room(can_read, wing, room):
    if isinstance(wing, str) and canonical_wing(wing) == MIRROR_WING:
        return not isinstance(room, str) or not can_read(room)
    return private_thread_reference(can_read, room)


def _hidden_place(can_read, node, fact, import_row):
    """Rooms and diary topics name threads alike; a proved import row keeps its room."""
    wing = None if fact else node.get("wing")
    if not import_row and unreadable_room(can_read, wing, node.get("room")):
        return True
    # Diary entries and their drawer metadata carry a topic beside the room.
    return "topic" in node and private_thread_reference(can_read, node["topic"])


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
            if not private_thread_reference(can_read, r)
        }
        return result if rooms == result["rooms"] else {**result, "rooms": rooms}
    return result


def scrub_provenance(node, access):
    out = node
    fields = provenance_refs.PROVENANCE_FIELDS | provenance_refs.DERIVED_FIELDS
    if node.get("result_type") == "asset":
        fields |= {"path", "title"}  # Media search also displays source_path under these aliases.
    for key in fields:
        value = node.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            if out is node:
                out = dict(node)
            out.pop(key, None)
            continue
        parsed = access.parse(value)
        allowed = parsed is not provenance_refs.REFUSE and all(
            access.reference(t) for t in parsed.tokens
        )
        if allowed and key in {"source_drawer_id", "source_closet"}:
            allowed = access.association(
                "drawer" if key == "source_drawer_id" else "closet", parsed.text
            )
        if not allowed:
            if out is node:
                out = dict(node)
            out.pop(key, None)
    # Flat ranges follow the existing provenance pass, preserving token-free
    # legacy parents. Nested aliases require a complete protected reference.
    if out.get("source_thread") is None:
        keys = {k for k in ("source_seq_start", "source_seq_end") if out.get(k) is not None}
        if keys:
            out = {key: value for key, value in out.items() if key not in keys}
    for key in ("source", "source_kept"):
        bundle = out.get(key)
        if (
            isinstance(bundle, dict)
            and bundle.keys()
            & {
                "thread",
                "seq_start",
                "seq_end",
                "source_thread",
                "source_seq_start",
                "source_seq_end",
            }
            and not _proved_thread(bundle.get("thread", bundle.get("source_thread")), access)
        ):
            out = {name: value for name, value in out.items() if name != key}
    for key in ("source", "target"):
        endpoint = out.get(key)
        if isinstance(endpoint, dict) and "drawer_id" in endpoint:
            identifier = endpoint["drawer_id"]
            if (
                not isinstance(identifier, str)
                or not identifier
                or not access.association("drawer", identifier)
            ):
                out = {name: value for name, value in out.items() if name != key}
    return out


def hallway_visible(node, access, policy):
    """A label is display text: scan protected tokens without citation refusal."""
    wing, rooms = node.get("wing"), node.get("rooms", [])
    if not isinstance(wing, str) or not wing.strip() or not policy.can_read_wing(wing):
        return False
    if not isinstance(rooms, list) or any(not isinstance(r, str) or not r.strip() for r in rooms):
        return False
    if not rooms and canonical_wing(wing) == MIRROR_WING:
        return False
    if any(unreadable_room(access, wing, room) for room in rooms):
        return False
    for key, value in public_row(node).items():
        if key in NUMERIC_KEYS:
            if (
                type(value) not in (int, float)
                or isinstance(value, float)
                and not math.isfinite(value)
            ):
                return False
        elif key == "rooms":
            if not all(_hallway_text_visible(room, access) for room in value):
                return False
        elif not _hallway_text_visible(value, access):
            return False
    return True


def _hallway_text_visible(value, access):
    """All hallway strings share the raw token scan and its character bound."""
    if not isinstance(value, str) or len(value) > provenance_refs.MAX_REFERENCE_CHARS:
        return False
    try:
        checks.remaining(0)
        tokens = provenance_refs.label_tokens(value)
        for token in tokens:
            checks.remaining(0)
            if not access.reference(token):
                return False
        return True
    except TimeoutError:
        return False


def _proved_thread(value, access):
    parsed = access.parse(value)
    return (
        parsed is not provenance_refs.REFUSE
        and bool(parsed.text)
        and (
            not parsed.tokens
            or parsed.text in parsed.tokens
            and all(access.reference(token) for token in parsed.tokens)
        )
    )


_ACQUISITION_COUNTS = frozenset(
    {
        "total_before_filter",
        "filtered_by_max_distance",
        "date_filter_pool_truncated",
        "candidates_truncated",
        "distinct_results_truncated",
    }
)


def _without_search_acquisition_counts(principal, rule, result):
    # Candidate acquisition precedes drawer and thread authorization, for every
    # principal: even a see-all reader may lack a thread a room names. Neither
    # its counts nor its truncation flags describe the visible set.
    if rule == "search" and isinstance(result, dict) and _ACQUISITION_COUNTS & set(result):
        result = {key: value for key, value in result.items() if key not in _ACQUISITION_COUNTS}
    return result


def gate(gateway, principal, rule, result, *, access=None):
    """Scrub mirror words, caching access decisions only within this response."""
    result = _without_search_acquisition_counts(principal, rule, result)
    access = access or reader(gateway, principal)
    can_read = access
    import_allowed = access.import_allowed
    reader_recall = access.recall
    reader_scope = principal.policy.has_scope("memory:import:read")

    def scrub(node):
        if isinstance(node, dict):
            fact = _is_fact(node)
            if reader_scope and not access.preview_allowed(node):
                return _DROP
            import_row = False
            if (
                reader_scope
                and not fact
                and not access.ordinary_record(node)
                and any(key in node for key in ("drawer_id", "text", "content"))
                and (
                    node.get("wing") in ("private_threads", "private_chats")
                    or _thread_id(node.get("room"))
                )
            ):
                import_row = import_allowed(node)
                if not import_row:
                    return _DROP
                # Return only proved words and structural provenance. Arbitrary
                # labels, previews and links cannot piggyback on a safe range.
                node = _proved_record(node)
            if fact:
                proved = _reader_fact(node, reader_recall, principal) if reader_scope else set()
                if proved is None:
                    return _DROP
                node = _scrub_fact(
                    node, lambda source: source in proved or can_read(source), access
                )
                if node is _DROP:
                    return _DROP
            node = scrub_provenance(node, access)
            if _hidden_place(can_read, node, fact, import_row):
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
                if (
                    key == "metadata"
                    and isinstance(cleaned, dict)
                    and isinstance(cleaned.get("source_file"), str)
                ):
                    cleaned = {**cleaned, "source_file": Path(cleaned["source_file"]).name}
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
    if isinstance(result, dict) and isinstance(result.get("hallways"), list):
        return _gate_hallways(result, access, principal.policy)
    result = filter_room_keys(rule, result, can_read)
    cleaned = scrub(result)
    if cleaned is _DROP:
        return _dropped_result(rule, result)
    return _filter_fact_links(cleaned)


def _gate_hallways(result, access, policy):
    rows = result["hallways"]
    if not all(isinstance(row, dict) and hallway_visible(row, access, policy) for row in rows):
        # A late revocation discards the page and its counts together.
        return {**result, "hallways": [], "count": 0, "total": 0}
    # Hallways prove plural rooms above; generic drawer scrub expects room.
    return _filter_fact_links({**result, "hallways": [public_row(row) for row in rows]})


def _proved_record(node):
    out = {k: node[k] for k in ("drawer_id", "wing", "room", "text", "content") if k in node}
    for key in ("score", "similarity", "distance"):
        if type(node.get(key)) in (int, float):
            out[key] = node[key]
    metadata = node.get("metadata") or {}
    if str(node.get("drawer_id", "")).startswith("imported:"):
        out["metadata"] = {
            k: metadata[k] for k in ("source_thread", "import_view_sha256") if k in metadata
        }
    else:
        source = node.get("source_path") or node.get("source_file") or metadata.get("source_file")
        out["source_path"] = source
        out["metadata"] = {"source_file": source, "source_thread": node["room"]}
    return out


def _reader_fact(node, reader_recall, principal):
    """Only view-bearing sources require every-source proof; ordinary facts retain their gate."""
    try:
        recall = reader_recall()
        if recall is None:
            return None
        proved = set()
        for source, row in _fact_sources(node):
            if recall.ordinary_record(principal, source):
                continue
            if not recall.proves_import(
                principal,
                source,
                row.get("source_seq_start", row.get("seq_start")),
                row.get("source_seq_end", row.get("seq_end")),
            ):
                return None
            proved.add(source)
        return proved
    except Exception:
        return None


def _recount(original, cleaned):
    """Counts and flags in a gated reply describe only what the reader receives."""
    for key in ("drawers", "results"):
        if isinstance(cleaned.get(key), list) and cleaned[key] is not original.get(key):
            cleaned.pop("total", None)
            if "count" in cleaned:
                cleaned["count"] = len(cleaned[key])
    matches = cleaned.get("matches")
    if isinstance(matches, list) and matches is not original.get("matches"):
        if "is_duplicate" in cleaned:
            cleaned["is_duplicate"] = bool(matches)
    entries, before = cleaned.get("entries"), original.get("entries")
    if (
        "showing" in original
        and isinstance(entries, list)
        and isinstance(before, list)
        and entries is not before
    ):
        # A diary page. The worker already excluded hidden topics outside this
        # window before counting, so only the entries dropped here remain.
        total = original.get("total")
        cleaned["total"] = max(
            0, (total if type(total) is int else 0) - (len(before) - len(entries))
        )
        cleaned["showing"] = len(entries)
        if not entries and cleaned["total"] == 0:
            for key in ("total", "showing"):
                cleaned.pop(key)
            cleaned["message"] = "No diary entries yet."


def _is_fact(node):
    return bool({"predicate", "object", "fact", "triple_id"}.intersection(node))


_thread_id = provenance_refs.is_thread_id


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
