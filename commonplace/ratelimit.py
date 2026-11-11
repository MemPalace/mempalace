"""Process-local, bounded token buckets for the gateway's public boundary."""

from __future__ import annotations

import ipaddress
import math
import os
import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Limit:
    name: str
    capacity: int
    seconds: int


ANONYMOUS = Limit("anonymous", 30, 60)
CREDENTIALS = Limit("credentials", 10, 60)
AUTHENTICATED = Limit("authenticated", 600, 60)
UNTRUSTED = Limit("untrusted", 60, 60)
WRITES = Limit("untrusted-writes", 120, 3600)
SHORT_LINKS = Limit("short-links", 120, 60)


@dataclass
class Backfill:
    """Bounded extra write allowance for a connection's first backfill.

    It tops up the steady untrusted write limit and is counted durably, so a
    restart or a client that "starts over" cannot re-arm it. ``per_connection``
    is spent once, within ``window_seconds`` of first use. ``per_space`` is a
    cap across all connections of a workspace that renews every
    ``space_period_seconds``. Zero disables the allowance.
    """

    per_connection: int = 5000
    per_space: int = 20000
    window_seconds: int = 24 * 3600
    space_period_seconds: int = 30 * 24 * 3600


def _integer(env, name, default, minimum):
    raw = env.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = minimum - 1
    if value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def limits_from_env(env=None) -> tuple[Limit, Backfill]:
    """Steady untrusted write limit and first-backfill allowance, from configuration."""
    env = os.environ if env is None else env
    write = Limit(
        WRITES.name,
        _integer(env, "COMMONPLACE_WRITE_LIMIT", WRITES.capacity, 1),
        _integer(env, "COMMONPLACE_WRITE_LIMIT_SECONDS", WRITES.seconds, 1),
    )
    default = Backfill()
    backfill = Backfill(
        _integer(env, "COMMONPLACE_BACKFILL_PER_CONNECTION", default.per_connection, 0),
        _integer(env, "COMMONPLACE_BACKFILL_PER_SPACE", default.per_space, 0),
        _integer(env, "COMMONPLACE_BACKFILL_WINDOW_SECONDS", default.window_seconds, 1),
        _integer(env, "COMMONPLACE_BACKFILL_SPACE_PERIOD_SECONDS", default.space_period_seconds, 1),
    )
    return write, backfill


def client_ip(peer: str, forwarded: str = "") -> str:
    """Only our loopback Caddy peer may supply the rightmost forwarded IP."""
    candidate = forwarded.rsplit(",", 1)[-1].strip() if peer == "127.0.0.1" else ""
    try:
        if candidate and "%" not in candidate:
            return _canonical(candidate)
    except ValueError:
        pass
    try:
        return _canonical(peer)
    except ValueError:
        return "unknown"


def _canonical(value: str) -> str:
    """One spelling per address on every Python version: an IPv4-mapped
    IPv6 address (``::ffff:1.2.3.4``) is that IPv4 address."""
    address = ipaddress.ip_address(value)
    mapped = getattr(address, "ipv4_mapped", None)
    return str(mapped or address)


class TokenBuckets:
    """Charge all applicable policies together, or none. Never store credentials.

    Idle buckets expire after one full refill period. At capacity, new identities
    fail closed until space expires; evicting live identities would reset limits.
    """

    def __init__(self, *, clock=time.monotonic, max_entries=10000):
        self.clock = clock
        self.max_entries = max_entries
        self._buckets = {}
        self._lock = threading.Lock()
        self._next_cleanup = 0.0

    def check(self, policies: list[tuple[Limit, str]]) -> int:
        """Return zero on admission, otherwise Retry-After seconds (rounded up)."""
        with self._lock:
            now = self.clock()
            if now >= self._next_cleanup or len(self._buckets) >= self.max_entries:
                self._buckets = {
                    key: bucket for key, bucket in self._buckets.items() if bucket[2] > now
                }
                self._next_cleanup = now + 60
            pending = {}
            retry = 0.0
            for limit, identity in policies:
                key = (limit.name, identity)
                tokens, updated, _expires = self._buckets.get(
                    key, (limit.capacity, now, now + limit.seconds)
                )
                rate = limit.capacity / limit.seconds
                tokens = min(limit.capacity, tokens + max(0.0, now - updated) * rate)
                if tokens < 1:
                    retry = max(retry, (1 - tokens) / rate)
                pending[key] = (tokens - 1, now, now + limit.seconds)
            new_keys = pending.keys() - self._buckets.keys()
            if len(self._buckets) + len(new_keys) > self.max_entries:
                retry = max(retry, min((b[2] - now for b in self._buckets.values()), default=60))
            if retry > 0:
                return max(1, math.ceil(retry))
            self._buckets.update(pending)
            return 0

    def refund(self, policies: list[tuple[Limit, str]]) -> None:
        """Give back one token for each policy a rejected request was charged."""
        with self._lock:
            for limit, identity in policies:
                key = (limit.name, identity)
                if key in self._buckets:
                    tokens, updated, expires = self._buckets[key]
                    self._buckets[key] = (min(limit.capacity, tokens + 1), updated, expires)
