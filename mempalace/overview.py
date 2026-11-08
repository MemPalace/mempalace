"""Allocation bounds for advisory overviews, independent of drawer payloads.

Backend overview iterators yield byte-bounded projected batches. Generic page
adapters must guarantee acquisition bytes, rather than trim an acquired full row.
"""

import json
import sys

KEYS = ("wing", "room", "is_sentinel", "ingest_mode")
PAGE_ROWS = 500
PAGE_BYTES = 1024 * 1024
CELL_BYTES = 64 * 1024
AGGREGATE_BYTES = 8 * 1024 * 1024
MAX_PAIRS = 16384


class OverviewError(ValueError):
    pass


def retained_size(value, seen=None):
    """Deep size of owned containers, keys and values; shared objects once."""
    if seen is None:
        seen = set()
    if id(value) in seen:
        return 0
    seen.add(id(value))
    n = sys.getsizeof(value)
    if isinstance(value, dict):
        n += sum(retained_size(k, seen) + retained_size(v, seen) for k, v in value.items())
    elif isinstance(value, (tuple, list, set, frozenset)):
        n += sum(retained_size(v, seen) for v in value)
    return n


def projected_batches(rows):
    """Split complete projected rows before the canonical JSON batch exceeds 1MiB."""
    batch, size = [], 2
    try:
        for raw in rows:
            meta = {k: v for k, v in (raw or {}).items() if k in KEYS}
            for value in meta.values():
                if isinstance(value, str) and len(value.encode("utf-8")) > CELL_BYTES:
                    raise OverviewError("overview_too_large")
            encoded = len(
                json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            if encoded + 2 > PAGE_BYTES:
                raise OverviewError("overview_too_large")
            if batch and (len(batch) >= PAGE_ROWS or size + 1 + encoded > PAGE_BYTES):
                yield batch
                batch, size = [], 2
            size += encoded + bool(batch)
            batch.append(meta)
        if batch:
            yield batch
    finally:
        close = getattr(rows, "close", None)
        if close:
            close()


class Tally:
    """Bounded aggregate. One bounded dictionary insertion may precede size refusal."""

    def __init__(self):
        self.total = 0
        self.taxonomy = {}
        self.pairs = 0
        self.size = retained_size(self.taxonomy)

    def add(self, wing, room, n=1):
        rooms = self.taxonomy.get(wing)
        if rooms is None or room not in rooms:
            if self.pairs >= MAX_PAIRS:
                raise OverviewError("overview_too_large")
            # Conservative preflight includes containers and unshared keys.
            if self.size + retained_size({wing: {room: n}}) > AGGREGATE_BYTES:
                raise OverviewError("overview_too_large")
            before = sys.getsizeof(self.taxonomy) + (
                sys.getsizeof(rooms) if rooms is not None else 0
            )
            if rooms is None:
                rooms = self.taxonomy[wing] = {}
                self.size += retained_size(wing) + sys.getsizeof(rooms)
            rooms[room] = n
            after = sys.getsizeof(self.taxonomy) + sys.getsizeof(rooms)
            self.size += after - before + retained_size(room) + sys.getsizeof(n)
            self.pairs += 1
            if self.size > AGGREGATE_BYTES:
                raise OverviewError("overview_too_large")
        else:
            rooms[room] += n
        self.total += n

    def result(self):
        if retained_size(self.taxonomy) > AGGREGATE_BYTES:
            raise OverviewError("overview_too_large")
        return self.total, self.taxonomy
