"""Process-wide memory of which one-shot warnings were already given.

Callers keep their own logger or stream and ask ``first_time(<topic>, ...)``
before warning; topics keep different callers' keys apart. Tests clear it
between cases with ``reset()``.
"""

import threading

_seen: set = set()
_lock = threading.Lock()


def first_time(*key) -> bool:
    """``True`` the first time ``key`` is seen in this process, ``False`` after."""
    with _lock:
        if key in _seen:
            return False
        _seen.add(key)
        return True


def reset() -> None:
    """Forget every key."""
    with _lock:
        _seen.clear()
