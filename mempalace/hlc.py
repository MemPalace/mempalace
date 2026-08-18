"""
hlc.py — Hybrid Logical Clock for RFC 004 op ordering

Total order across replicas without trusting wall clocks: physical
milliseconds + a logical counter + the replica id as final tiebreak.
Rendered as a fixed-width, lexicographically sortable string so SQLite TEXT
comparison IS the causal comparison:

    <unix_ms:13 decimal digits>-<counter:6 hex digits>-<replica_id>

e.g. ``1783038849123-000000-rep_ab12cd34ef56``.

Semantics (standard HLC):
- ``tick()`` for a local op: physical time if it advanced, else previous
  ms with counter+1 — never goes backwards, even against clock regression.
- ``observe(remote)`` on receiving a remote op: the clock absorbs the
  remote's instant so later local ops sort after everything already seen.

Cursor semantics stay LOCAL (rowid arrival order) per RFC 004 A.4 — a tail
consumer must see late-arriving remote ops even though their HLC is older.
HLC is the *display/merge* order; arrival is the *delivery* order.
"""

import re
import threading
import time

_HLC_RE = re.compile(r"^(\d{13})-([0-9a-f]{6})-(.+)$")

MAX_COUNTER = 0xFFFFFF
MAX_FUTURE_DRIFT_MS = 60_000


def parse(hlc: str):
    """Return (ms, counter, replica_id) or raise ValueError."""
    m = _HLC_RE.match(hlc)
    if not m:
        raise ValueError(f"not a valid HLC string: {hlc!r}")
    return int(m.group(1)), int(m.group(2), 16), m.group(3)


def render(ms: int, counter: int, replica_id: str) -> str:
    return f"{ms:013d}-{counter:06x}-{replica_id}"


class HybridLogicalClock:
    """Thread-safe HLC bound to one replica id.

    ``last`` may be seeded from persisted state (the newest hlc in the
    op-log) so monotonicity survives process restarts.
    """

    def __init__(self, replica_id: str, last: str = None, now_ms=None):
        self.replica_id = replica_id
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._lock = threading.Lock()
        if last:
            ms, counter, _ = parse(last)
            if self._is_within_future_drift(ms):
                self._ms, self._counter = ms, counter
            else:
                self._ms, self._counter = 0, 0
        else:
            self._ms, self._counter = 0, 0

    def _is_within_future_drift(self, ms: int) -> bool:
        return ms <= self._now_ms() + MAX_FUTURE_DRIFT_MS

    def tick(self) -> str:
        """Stamp a new local op; strictly greater than everything seen."""
        with self._lock:
            now = self._now_ms()
            if now > self._ms:
                self._ms, self._counter = now, 0
            elif self._counter < MAX_COUNTER:
                self._counter += 1
            else:
                # Counter exhaustion within one ms is pathological; step
                # the physical component rather than wrap and reorder.
                self._ms += 1
                self._counter = 0
            return render(self._ms, self._counter, self.replica_id)

    def observe(self, remote_hlc: str) -> None:
        """Absorb a remote op's instant so future ticks sort after it."""
        try:
            remote_ms, remote_counter, _ = parse(remote_hlc)
        except ValueError:
            return  # never let a malformed remote stamp wedge the clock
        if not self._is_within_future_drift(remote_ms):
            return
        with self._lock:
            if remote_ms > self._ms or (remote_ms == self._ms and remote_counter > self._counter):
                self._ms, self._counter = remote_ms, remote_counter
