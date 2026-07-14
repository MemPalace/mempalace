"""Small, failure-isolated progress reporting for background operations."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

ProgressCallback = Callable[[dict[str, Any]], None]


class ProgressReporter:
    """Merge progress fields and throttle non-forced callback delivery."""

    def __init__(
        self,
        callback: ProgressCallback | None,
        *,
        min_interval: float = 1.0,
    ) -> None:
        self.callback = callback
        self.min_interval = max(0.0, float(min_interval))
        self.state: dict[str, Any] = {}
        self._last_emitted_at: float | None = None

    def emit(self, *, force: bool = False, **updates: Any) -> None:
        self.state.update(updates)
        if self.callback is None:
            return
        now = time.monotonic()
        if (
            not force
            and self._last_emitted_at is not None
            and now - self._last_emitted_at < self.min_interval
        ):
            return
        # Throttle delivery attempts as well as successes. A broken queue or
        # callback must not turn per-file progress into an exception hot loop.
        self._last_emitted_at = now
        try:
            self.callback(dict(self.state))
        except Exception:
            # Progress is observability only; it must never fail useful work.
            return
