"""Hallway recompute taken off the mine path.

Hallways are recomputed for a whole wing by the exact algorithm in
:func:`mempalace.hallways.compute_hallways_for_wing`. Per-drawer deltas are
not exact: entity spellings are resolved wing-wide (``_wing_file_keys``), so
one new drawer can change how earlier drawers' entities resolve. On a large
wing that whole-wing pass costs tens of seconds, which a background mine
(a hook re-mining a transcript every few minutes) should not pay each time.

A mine that defers instead:

1. marks the wing dirty in the hallway store (:func:`hallways.mark_wing_dirty`),
   durably, beside the records the mark is about; and
2. queues one coalesced ``hallways`` job for the wing in the daemon's queue,
   debounced by ``hallways.settle_seconds`` (600) and capped by
   ``hallways.max_wait_seconds`` (3600) from the wing's first mark.

A burst of mines into one wing then costs one recompute, which the daemon runs
at idle priority in its mine child. The mark is what guarantees the work: the
recompute clears it only when it read the drawers after the last mark, the
daemon re-queues any mark still present when a job ends, and a daemon that
starts queues every mark it finds (:func:`enqueue_dirty_wings`), so a mark
left by a crash, or by a mine that ran with no daemon, is still processed.

While a wing is dirty, readers see its previous hallway records: stale, never
missing or partial, because a recompute replaces the wing in one transaction.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

logger = logging.getLogger(__name__)

JOB_KIND = "hallways"


def dedupe_key(wing: str) -> str:
    return f"{JOB_KIND}:{wing}"


def schedule(wing: str, palace_path: str, *, config=None, store=None, mark=None):
    """Queue the coalesced recompute for ``wing``'s dirty mark; return the job.

    The window is measured from the mark's first time, so a mark that waited
    (a restart, a mine with no daemon) runs no later than ``max_wait`` after
    it was made. ``None`` when the wing is not dirty.
    """
    from .config import MempalaceConfig
    from .daemon_queue import QueueStore, queue_path
    from .hallways import dirty_mark

    config = config or MempalaceConfig(palace_path=palace_path)
    mark = mark or dirty_mark(wing, config)
    if mark is None:
        return None
    store = store or QueueStore(queue_path(palace_path))
    settle, max_wait = config.hallways_refresh_debounce
    remaining = max(0.0, mark["first_marked_at"] + max_wait - float(store.clock()))
    return store.enqueue(
        JOB_KIND,
        {"wing": wing},
        dedupe_key=dedupe_key(wing),
        settle_seconds=min(settle, remaining),
        max_wait_seconds=remaining,
    )


def defer_wing(wing: str, palace_path: str, *, config=None, entity_tunnels: bool = False) -> bool:
    """Mark ``wing`` dirty and queue its recompute; False when nothing was marked.

    A mark that cannot be written leaves the caller to recompute inline: the
    recompute may be delayed, never dropped. A queue that cannot be written
    only logs, since the mark is already durable and the next daemon start
    queues it.
    """
    from .hallways import mark_wing_dirty

    try:
        mark_wing_dirty(wing, config, entity_tunnels=entity_tunnels)
    except Exception as exc:  # noqa: BLE001 - the caller falls back to inline
        logger.warning("hallways: could not mark %s dirty: %s", wing, exc, exc_info=True)
        return False
    try:
        job = schedule(wing, palace_path, config=config)
    except Exception as exc:  # noqa: BLE001 - the mark stays and is queued at daemon start
        logger.warning("hallways: could not queue the recompute of %s: %s", wing, exc)
        print(f"  Hallways: recompute of {wing} deferred (queued at next daemon start)")
        return True
    when = ""
    if job is not None and job.run_after is not None:
        import time

        when = f" in ~{max(0, int(job.run_after - time.time())) // 60} min"
    print(f"  Hallways: recompute of {wing} deferred{when}")
    return True


def enqueue_dirty_wings(palace_path: str, *, store=None, config=None) -> int:
    """Queue a recompute for every dirty wing of the palace; return how many.

    The daemon calls this when it starts, so marks a crash or a daemon-less
    mine left behind are processed. Coalescing makes it harmless for a wing
    whose job is already queued.
    """
    from .config import MempalaceConfig
    from .hallways import dirty_wings

    config = config or MempalaceConfig(palace_path=palace_path)
    queued = 0
    for mark in dirty_wings(config):
        try:
            schedule(mark["wing"], palace_path, config=config, store=store, mark=mark)
            queued += 1
        except Exception:  # noqa: BLE001 - one bad wing must not stop the rest
            logger.warning("hallways: could not queue %s", mark["wing"], exc_info=True)
    return queued


def run_refresh(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one deferred recompute: the exact whole-wing algorithm, then tunnels.

    Holds the palace write lock like ``hallways --rebuild``: an inline mine
    that recomputes the same wing meanwhile would otherwise have its newer
    records overwritten by this run's older read. A refusal reports
    ``LockHeldByOtherProcess``, which the daemon defers rather than fails.
    The run fails, and the mark stays, when the drawers could not be read.
    """
    from .config import MempalaceConfig
    from .daemon_queue import LOCK_REFUSAL_ERROR_CLASS
    from .hallways import compute_hallways_for_wing, dirty_mark
    from .palace import MineAlreadyRunning, get_collection, mine_palace_lock

    palace_path = payload.get("palace_path") or MempalaceConfig().palace_path
    wing = payload.get("wing")
    if not isinstance(wing, str) or not wing:
        return {"success": False, "error": "hallways job needs a wing", "exit_code": 2}
    config = MempalaceConfig(palace_path=palace_path)
    base = {"kind": JOB_KIND, "wing": wing}
    try:
        with mine_palace_lock(palace_path):
            mark = dirty_mark(wing, config)
            if mark is None:
                return {**base, "success": True, "skipped": "clean", "hallways": 0}
            col = get_collection(palace_path, create=False)
            created = compute_hallways_for_wing(wing, col=col, config=config)
            after = dirty_mark(wing, config)
            if after is not None and after["generation"] == mark["generation"]:
                return {
                    **base,
                    "success": False,
                    "error": f"hallway recompute of {wing} could not read its drawers",
                    "exit_code": 1,
                }
            tunnels = 0
            if mark["entity_tunnels"]:
                from . import miner

                try:
                    tunnels = miner._compute_entity_tunnels_for_wing(wing, config=config)
                except Exception as exc:  # noqa: BLE001 - derived analytic, as inline
                    print(f"  WARNING: entity tunnel computation skipped — {exc}", file=sys.stderr)
    except MineAlreadyRunning as exc:
        return {
            **base,
            "success": False,
            "error": str(exc),
            "error_class": LOCK_REFUSAL_ERROR_CLASS,
            "exit_code": 1,
        }
    print(f"  Hallways: {wing} recomputed, {len(created)} within-wing link(s)")
    return {**base, "success": True, "hallways": len(created), "entity_tunnels": tunnels}


def defer_if_configured(
    wing: str,
    palace_path: str,
    *,
    config,
    mode: Optional[str],
    entity_tunnels: bool = False,
) -> bool:
    """A mine's hallway step: True when it was deferred, False to run it inline.

    ``mode`` is the mine's ``hallways`` argument; ``None`` lets config decide
    (:meth:`MempalaceConfig.hallways_mode`, interactive). Deferring falls back
    to inline when the dirty mark cannot be written.
    """
    if config.hallways_mode(mode) != "defer":
        return False
    return defer_wing(wing, palace_path, config=config, entity_tunnels=entity_tunnels)
