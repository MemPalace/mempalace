"""Mirror stored threads into the workspace palace. Owner: part T03.

Class-pure units become logical drawers in wing ``MIRROR_WING``
(``private_threads``), room ``<stored thread id>``. The authenticated internal
worker operation preserves unit identities and index provenance. Legacy chunks
retain their public drawer-write path. Both paths stamp authorship and preserve
verbatim text; retries add no duplicate drawers.

Best effort: ``thread_store`` remains the lossless copy. A mirror failure
does not fail the handoff.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, replace
from typing import Callable

from mempalace.lexical import ranking_stop_words, rerank_merged_hits
from mempalace.stored_text import INDEX_SPANS, IndexSource

from .models import CHUNK_MAX_CHARS, MIRROR_WING, RecallHit, StoredThread
from .thread_store import Chunk, chunk_messages

CallTool = Callable  # (principal, tool_name, arguments) -> parsed result dict


class MirrorError(RuntimeError):
    """Memory could not mirror or search a stored thread."""


@dataclass(frozen=True)
class MirrorUnit(Chunk):
    message_class: str
    index_spans: str | None = None


def message_class(message) -> str:
    if message.kind == "tool_call":
        return "tool_call"
    if message.kind == "tool_result":
        return "tool_output"
    if message.kind == "system" or message.role == "system":
        return "system"
    return "conversation"


def class_units(messages: list, max_chars: int = CHUNK_MAX_CHARS) -> list[MirrorUnit]:
    """Keep whole messages together where possible, without mixing classes.

    Long messages retain one header, then verbatim continuation slices. Index
    provenance is computed before slicing, so a short continuation cannot
    independently accept or reject a base64 fragment.
    """
    if max_chars <= 0:
        raise ValueError("Chunk size must be positive")
    units = []
    pending = []
    length = 0
    kind = ""

    def append(text, start, end, kind, source=None, offset=0):
        source = source or IndexSource(text)
        part = (
            units[-1].index + 1
            if units and (units[-1].seq_start, units[-1].seq_end) == (start, end)
            else 0
        )
        units.append(
            MirrorUnit(
                part,
                start,
                end,
                text,
                kind,
                source.metadata(offset, offset + len(text)).get(INDEX_SPANS),
            )
        )

    def flush():
        nonlocal length
        if pending:
            append("\n".join(text for _, text in pending), pending[0][0], pending[-1][0], kind)
            pending.clear()
            length = 0

    for message in messages:
        next_kind = message_class(message)
        prefix = f"[{message.seq}] {message.role} {message.kind}"
        if message.name:
            prefix += f" {message.name}"
        text = prefix + ": " + message.text
        if pending and (next_kind != kind or length + 1 + len(text) > max_chars):
            flush()
        kind = next_kind
        if len(text) > max_chars:
            source = IndexSource(text)
            for offset in range(0, len(text), max_chars):
                append(
                    text[offset : offset + max_chars],
                    message.seq,
                    message.seq,
                    kind,
                    source,
                    offset,
                )
        else:
            length += len(text) + bool(pending)
            pending.append((message.seq, text))
    flush()
    return units


def _call(call_tool: CallTool, principal, name: str, arguments: dict, *, missing_ok=False) -> dict:
    try:
        result = call_tool(principal, name, arguments)
    except Exception as exc:
        raise MirrorError("Memory worker call failed") from exc
    if missing_ok and isinstance(result, dict):
        absent = result.get("error") == "No palace found" or (
            result.get("backend") == "chroma" and result.get("error") == "Chroma database missing"
        )
        if absent:
            return {"drawers": [], "total": 0}
    if (
        not isinstance(result, dict)
        or result.get("error")
        or result.get("success") is False
        or result.get("isError")
    ):
        raise MirrorError("Memory worker returned an error")
    return result


def _legacy_mirrored_seq(call_tool, principal, stored):
    """Read existing origin metadata once; never retrofit mirrored drawers."""
    offset = 0
    completed = -1
    while True:
        page = _call(
            call_tool,
            principal,
            "mempalace_list_drawers",
            {"wing": MIRROR_WING, "room": stored.id, "limit": 100, "offset": offset},
            missing_ok=True,
        )
        drawers = page.get("drawers")
        if not isinstance(drawers, list):
            raise MirrorError("Memory worker returned invalid drawer metadata")
        for drawer in drawers:
            if drawer.get("wing") != MIRROR_WING or drawer.get("room") != stored.id:
                continue
            metadata = drawer.get("metadata") or {}
            source = metadata.get("source_file", "")
            if not isinstance(source, str) or not source.startswith(f"handoff:{stored.id}#"):
                continue
            label = _range_label(
                {"claimed_by": metadata.get("claimed_by") or metadata.get("added_by")}
            )
            if label is not None:
                completed = max(completed, label[1])
        offset += len(drawers)
        if not drawers or offset >= page.get("total", offset):
            # Legacy writes were ordered. The caller decides whether the final
            # message is whole or needs replaying after a partial split write.
            return min(completed, stored.message_count - 1)


def _bootstrap_seq(store, stored, end: int) -> int:
    """Last message the legacy drawers fully cover; -1 when none."""
    if end < 0:
        return -1  # Fresh thread: no legacy drawers, no store read.
    found = store.messages(stored.id, end, end)
    # Use the legacy writer's default chunk limit, not the physical drawer size.
    # Changing that default affects threads not yet bootstrapped. One legacy
    # add_drawer atomically upserts every physical row, so an unsplit message
    # in a surviving logical drawer is complete even across multiple rows.
    if len(found) == 1 and found[0].seq == end and len(chunk_messages(found)) == 1:
        return end
    return end - 1  # Split, missing or unexpected: replay the final message in full.


def mirror_pending(
    call_tool,
    principal,
    stored,
    store,
    *,
    skip_empty=False,
    should_continue=None,
    max_messages=None,
    max_units=None,
    before_range=None,
):
    """Resume a durable batch; interrupted writes leave its checkpoint pending.

    ``skip_empty`` omits archive-only rows without renumbering messages.
    ``should_continue`` may stop writes by returning False before a worker call.
    ``max_messages`` pins and finishes at most one bounded range per call;
    without it, handoff keeps its existing whole-thread behavior. ``max_units``
    reduces new ranges to a whole-message prefix. ``before_range`` can defer
    a range before its first write; its durable pin survives the deferral.
    """
    if max_messages is not None and (type(max_messages) is not int or max_messages < 1):
        raise ValueError("max_messages must be a positive integer")
    progress = store.mirror_progress(stored.id)
    end = _legacy_mirrored_seq(call_tool, principal, stored) if progress is None else -1
    bootstrap = _bootstrap_seq(store, stored, end)
    drawer_ids = []
    while True:
        progress = store.mirror_progress(stored.id)
        completed = progress[0] if progress is not None else bootstrap
        through = stored.message_count - 1
        if max_messages is not None:
            through = min(through, completed + max_messages)
            if max_units is not None and (progress is None or progress[0] == progress[1]):
                through = _bounded_end(store, stored, completed, through, max_units, skip_empty)
        completed, through = store.begin_mirror(stored.id, through, bootstrap=bootstrap)
        if completed >= stored.message_count - 1:
            return drawer_ids
        messages = store.messages(stored.id, completed + 1, through)
        if not messages or messages[0].seq != completed + 1 or messages[-1].seq != through:
            raise MirrorError("Stored thread has an incomplete mirror range")
        if skip_empty:
            messages = [message for message in messages if message.text]
        units = class_units(messages)
        if before_range is not None:
            before_range(units)
        if should_continue is None:
            drawer_ids.extend(mirror(call_tool, principal, stored, units))
        else:
            drawer_ids.extend(
                mirror(call_tool, principal, stored, units, should_continue=should_continue)
            )
        store.finish_mirror(stored.id, through)
        if max_messages is not None:
            return drawer_ids


def _bounded_end(store, stored, completed, through, max_units, skip_empty):
    messages = store.messages(stored.id, completed + 1, through)
    low, high = 1, len(messages)
    # Whole messages are the durable unit. At least one message is allowed,
    # even when that single message exceeds the soft per-job unit budget.
    while low < high:
        middle = (low + high + 1) // 2
        candidate = messages[:middle]
        if skip_empty:
            candidate = [row for row in candidate if row.text]
        if len(class_units(candidate)) <= max_units:
            low = middle
        else:
            high = middle - 1
    return completed + low if messages else through


def mirror(
    call_tool: CallTool, principal, stored: StoredThread, chunks: list, *, should_continue=None
) -> list[str]:
    """Return stable unit IDs; legacy chunks retain content-based deduplication."""
    if chunks and isinstance(chunks[0], MirrorUnit):
        # Do not send thousands of identical continuations to the worker.
        # The same content in another range/class remains a different unit.
        unique = {}
        keys = []
        for unit in chunks:
            key = (unit.seq_start, unit.seq_end, unit.message_class, unit.text)
            unique.setdefault(key, unit)
            keys.append(key)
        units = list(unique.values())
        drawer_ids = []
        for offset in range(0, len(units), 64):
            if should_continue is not None and not should_continue():
                raise MirrorError("Memory mirror paused before worker write")
            batch = units[offset : offset + 64]
            result = _call(
                call_tool,
                principal,
                "write_mirror_units",
                {
                    "thread_id": stored.id,
                    "units": [
                        {
                            "seq_start": unit.seq_start,
                            "seq_end": unit.seq_end,
                            "message_class": unit.message_class,
                            "part": unit.index,
                            "text": unit.text,
                            "index_spans": unit.index_spans,
                        }
                        for unit in batch
                    ],
                },
            )
            ids = result.get("drawer_ids")
            if (
                not isinstance(ids, list)
                or len(ids) != len(batch)
                or not all(isinstance(i, str) and i for i in ids)
            ):
                raise MirrorError("Memory worker returned invalid mirror ids")
            drawer_ids.extend(ids)
        mapped = dict(zip(unique, drawer_ids))
        return [mapped[key] for key in keys]
    drawer_ids = []
    mirrored = {}
    for chunk in chunks:
        if chunk.text in mirrored:
            # Preserve the input-to-drawer mapping without another write.
            # The lossless store retains each repeated piece's full origin.
            drawer_ids.append(mirrored[chunk.text])
            continue
        try:
            chunk.text.encode("utf-8")
        except UnicodeEncodeError as exc:
            # add_drawer strips lone surrogates; fail instead of mirroring
            # different words from the lossless thread store.
            raise MirrorError("Memory cannot mirror this text verbatim") from exc
        arguments = {
            "wing": MIRROR_WING,
            "room": stored.id,
            "content": chunk.text,
            "source_file": f"handoff:{stored.id}#{chunk.index}",
            "added_by": f"handoff:{chunk.seq_start}-{chunk.seq_end}:{len(chunk.text)}",
        }
        if should_continue is not None and not should_continue():
            raise MirrorError("Memory mirror paused before worker write")
        result = _call(
            call_tool,
            principal,
            "mempalace_add_drawer",
            arguments,
        )
        drawer_id = result.get("drawer_id")
        if not isinstance(drawer_id, str) or not drawer_id:
            raise MirrorError("Memory worker returned no entry id")
        mirrored[chunk.text] = drawer_id
        drawer_ids.append(drawer_id)
    return drawer_ids


_RANGE = re.compile(r"handoff:([0-9]+)-([0-9]+):([0-9]+)\Z")
_SEQUENCE = re.compile(r"^\[([0-9]+)\] ", re.MULTILINE)


def _range_label(hit: dict):
    label = hit.get("claimed_by", "")
    match = _RANGE.fullmatch(label) if isinstance(label, str) else None
    if match is None:
        return None
    start, end, length = map(int, match.groups())
    if start > end:
        raise MirrorError("Memory entry has an invalid message range")
    return start, end, length


def _score(hit: dict) -> float:
    for key, sign in (("score", 1), ("similarity", 1), ("distance", -1)):
        value = hit.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return sign * float(value)
    return 0.0


def _chunk_text(call_tool: CallTool, principal, hit: dict, label, hydrated: dict) -> str:
    text = hit.get("text")
    if not isinstance(text, str):
        raise MirrorError("Memory entry has no text")
    if label is None or len(text) == label[2]:
        return text

    # Search reports the logical drawer id even when the worker split the
    # mirrored chunk. Fetch it once to reassemble the complete original text.
    drawer_id = hit.get("drawer_id")
    if not isinstance(drawer_id, str) or not drawer_id:
        raise MirrorError("Memory entry has no id")
    if drawer_id not in hydrated:
        hydrated[drawer_id] = _call(
            call_tool, principal, "mempalace_get_drawer", {"drawer_id": drawer_id}
        )
    result = hydrated[drawer_id]
    metadata = result.get("metadata")
    if not isinstance(metadata, dict):
        raise MirrorError("Memory entry has no origin")
    if (
        result.get("wing") != MIRROR_WING
        or result.get("room") != hit["room"]
        or metadata.get("source_file") != (hit.get("source_path") or hit.get("source_file"))
    ):
        raise MirrorError("Memory entry not found")
    text = result.get("content")
    if not isinstance(text, str) or len(text) != label[2]:
        raise MirrorError("Memory entry is incomplete")
    return text


def palace_search(
    call_tool: CallTool, principal, stored_ids: list[str], query: str, limit: int = 8
) -> list[RecallHit]:
    """Default hybrid search, returning one excerpt per overlapping range.

    An oversized message may have further matching continuation pieces.
    Fetch its returned sequence range to read the complete verbatim message.
    """
    if limit <= 0:
        return []
    fetch_limit = min(limit * 4, 50)
    hits = {}
    components = {}
    hydrated = {}
    first = None
    for stored_id in dict.fromkeys(stored_ids):
        result = _call(
            call_tool,
            principal,
            "mempalace_search",
            {
                "query": query,
                "wing": MIRROR_WING,
                "room": stored_id,
                "limit": fetch_limit,
                "include_facts": False,
            },
        )
        if first is None:
            first = result
        results = result.get("results")
        if not isinstance(results, list):
            raise MirrorError("Memory worker returned invalid search results")
        origin = re.compile(
            r"handoff:"
            + re.escape(stored_id)
            + r"#(?:v2:[0-9]+-[0-9]+:(?:conversation|tool_call|tool_output|system):)?([0-9]+)\Z"
        )
        for hit in results:
            if not isinstance(hit, dict):
                raise MirrorError("Memory worker returned an invalid search entry")
            # WorkerClient bypasses token policy: never return another room's
            # content, even if a backend accidentally ignores a search filter.
            if hit.get("wing") != MIRROR_WING or hit.get("room") != stored_id:
                continue
            source = hit.get("source_path") or hit.get("source_file")
            match = origin.fullmatch(source) if isinstance(source, str) else None
            if match is None:
                continue
            index = int(match.group(1))
            label = _range_label(hit)
            text = _chunk_text(call_tool, principal, hit, label, hydrated)
            if label is not None:
                start, end = label[:2]
            else:
                sequences = [int(seq) for seq in _SEQUENCE.findall(text)]
                if not sequences:
                    raise MirrorError("Memory entry has no message range")
                start, end = min(sequences), max(sequences)
            recalled = RecallHit(stored_id, index, start, end, _score(hit), text)
            key = (stored_id, source)
            if key not in hits or recalled.score > hits[key].score:
                hits[key] = recalled
                components[key] = {**hit, "text": text}
    rerank_merged_hits(
        (first or {}).get("query", query),
        list(components.values()),
        stop_words=ranking_stop_words(first or {}),
    )
    for key, hit in components.items():
        hits[key] = replace(hits[key], score=_score(hit))
    unique = []
    for hit in sorted(hits.values(), key=lambda hit: hit.score, reverse=True):
        if not any(
            hit.stored_thread_id == previous.stored_thread_id
            and hit.seq_start <= previous.seq_end
            and previous.seq_start <= hit.seq_end
            for previous in unique
        ):
            unique.append(hit)
    return unique[:limit]
