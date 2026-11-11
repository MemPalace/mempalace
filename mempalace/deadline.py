"""Read deadline shared by the rules checks and the fact readers.

Leaf module: stdlib only. ``commonplace.rules.checks`` owns when a deadline is
set; ``knowledge_graph``, ``fact_index`` and ``searcher.facts`` only read it.
Importing it from there keeps those modules from reaching ``commonplace.rules``
(and through it events, boards and the gateway), which would widen every
import-following consumer of the fact store.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

_deadline = ContextVar("commonplace_rules_deadline", default=None)


def remaining(timeout, clock=monotonic):
    end = _deadline.get()
    if end is None:
        return timeout
    left = end - clock()
    if left <= 0:
        raise TimeoutError("rules deadline expired")
    return min(timeout, left)


@contextmanager
def deadline(timeout, clock=monotonic):
    """Nested readers may shorten a caller's deadline, never extend it."""
    end = clock() + timeout
    prior = _deadline.get()
    token = _deadline.set(min(prior, end) if prior is not None else end)
    try:
        remaining(timeout, clock)
        yield
        remaining(timeout, clock)
    finally:
        _deadline.reset(token)
