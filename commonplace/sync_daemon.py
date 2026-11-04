"""Durable, explicitly configured transcript and memory synchronization."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlencode

from .capture_client import endpoint
from .sync_protocol import (
    BACKFILL_WRITES,
    LIVE_TICK_SECONDS,
    MAX_WAIT_SECONDS,
    RETRY_RE,
    SYNC_FEED,
    TOKEN_ENV,
    WRITE_BUDGET,
)


class ConfigError(ValueError):
    """A configuration problem, distinct from a retryable source failure."""


def request(target, token, method, route, body=None):
    """Return status and JSON without printing source bytes or following redirects."""
    connection_type = (
        http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(target.hostname, target.port, timeout=35)
    try:
        connection.request(
            method,
            route,
            None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
            {"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Sync response too large")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("Invalid sync response")
        return response.status, payload
    finally:
        connection.close()


def _epoch(value):
    return value.timestamp() if isinstance(value, datetime) else float(value)


def _feed_key(hub, env_name):
    return hashlib.sha256((hub.rstrip("/") + "\n" + env_name).encode()).hexdigest()


class _Session:
    def __init__(self, config, state, clock, transport):
        from . import sync_sources

        self.sources = sync_sources
        self.config, self.state, self.clock, self.transport = config, state, clock, transport
        self.target = endpoint(config["hub"])
        self.tokens = config["tokens"]
        self.feed_env = next(
            (
                TOKEN_ENV[s]
                for s in ("claude-code", "codex", "native-memory")
                if self.tokens.get(TOKEN_ENV[s])
            ),
            None,
        )
        if self.feed_env is None:
            raise ConfigError("Set a sync token environment variable")
        self.key = _feed_key(config["hub"], self.feed_env)
        self.interval, self.enabled = 900, True
        self.signals = True
        self.refresh = False
        self.next_poll = 0.0
        self.bkey = _feed_key(config["hub"], "backfill")
        self.backfill = False
        self.unavailable = set()
        self.initialize_transient = False
        self._apply_cached()

    def now(self):
        return _epoch(self.clock())

    def _apply_cached(self):
        row = self.state.feed_cursor(self.key)
        if row:
            value = row.get("state_json", {})
            value = json.loads(value) if isinstance(value, str) else value
            self.interval = int(value.get("interval_seconds", 900))
            self.enabled = value.get("capture_enabled", True)

    def feed_args(self, wait=0):
        row = self.state.feed_cursor(self.key)
        route = "/api/events?" + urlencode(
            {"feed": SYNC_FEED, "after": row["seq"] if row else 0, "wait": wait}
        )
        return self.target, self.tokens[self.feed_env], "GET", route

    def feed_request(self, wait=0):
        return self.transport(*self.feed_args(wait))

    def feed_result(self, result):
        status, payload = result
        if status == 403:
            # A pre-sync hub has no sync feed. Keep its capture path usable.
            self.signals = False
            status, settings = self.transport(
                self.target, self.tokens[self.feed_env], "GET", "/api/settings/capture"
            )
            if status != 200:
                return 1, False
            self.enabled = bool(settings["enabled"])
            self.interval = int(settings.get("interval_seconds", 900))
            self.next_poll = self.now() + self.interval
            return 0, False
        if status != 200:
            self.next_poll = self.now() + 60
            return (0 if status == 429 else 1), False
        self.state.store_feed(self.key, payload, fetched_at=self.now())
        self._apply_cached()
        window = payload["state"].get("next_window_at")
        window = datetime.fromisoformat(window.replace("Z", "+00:00")).timestamp() if window else 0
        self.next_poll = window if window > self.now() else self.now() + self.interval
        return 0, bool(payload.get("more"))

    def feed(self):
        while True:
            code, more = self.feed_result(self.feed_request())
            if code or not more:
                return code

    def _status(self, op, status):
        if status in ("conflict", "revoked"):
            self.state.outbox_mark(op.op_id, status)
        self.state.set_status(op.source, op.path, op.native_id, status)

    def _defer(self, payload, key):
        match = re.search(RETRY_RE, str(payload.get("error", "")), re.IGNORECASE)
        self.state.defer(key, self.now() + (max(1, int(match[1])) if match else 60))

    def initialize(self, sources=None, *, once=False):
        """Adopt each server cursor before scanning a stream for the first time."""
        failed = False
        self.unavailable = set()
        self.initialize_transient = False
        for source in self.sources.discover(self.config) if sources is None else sources:
            if self.state.cursor(source.source, source.path, source.native_id) is not None:
                continue
            token = self.tokens.get(TOKEN_ENV[source.source])
            if not token:
                raise ConfigError("Missing capture token environment variable")
            try:
                status, payload = self.transport(
                    self.target,
                    token,
                    "GET",
                    "/api/capture?"
                    + urlencode({"source": source.source, "thread_id": source.native_id}),
                )
            except (OSError, http.client.HTTPException, ValueError):
                failed = True
                self.initialize_transient = True
                continue
            if status == 429:
                self._defer(payload, self.key)
                if once:
                    continue  # --once still reports every other stream's state
                return int(failed)  # the feed is deferred; stop asking this pass
            if status != 200:
                if status == 409:
                    self.state.set_status(source.source, source.path, source.native_id, "paused")
                if 400 <= status < 500 and status not in (408, 409, 425, 429):
                    self.state.set_status(
                        source.source,
                        source.path,
                        source.native_id,
                        "revoked",
                    )
                    self.unavailable.add((source.source, source.path, source.native_id))
                self.initialize_transient |= status != 409 and not (
                    400 <= status < 500 and status not in (408, 425, 429)
                )
                failed |= status != 409
                continue
            offset, prefix = payload.get("offset"), payload.get("prefix_sha256")
            if (
                type(offset) is not int
                or offset < 0
                or not isinstance(prefix, str)
                or re.fullmatch(r"[0-9a-f]{64}", prefix) is None
            ):
                failed = True
                self.initialize_transient = True
                continue
            self.state.store_cursor(source.source, source.path, source.native_id, offset, prefix)
        return int(failed)

    def _backfill(self, discovered, ops):
        """Faster first catch-up, once per feed: ends when caught up or capped, never re-enters."""
        # Cadence rows also hold durable counters; no separate schema is needed.
        done = self.bkey + "-done"
        if self.state.next_due(done):
            return False
        if self.state.next_due(self.bkey) >= BACKFILL_WRITES or (
            not ops
            and any(
                self.state.cursor(s.source, s.path, s.native_id) is not None for s in discovered
            )
            and all(
                self.state.cursor(s.source, s.path, s.native_id) is not None
                or (s.source, s.path, s.native_id) in self.unavailable
                for s in discovered
            )
        ):
            self.state.defer(done, self.now())
            return False
        return True

    def _budget(self, env_name, retry=False):
        """Durable client budget: bounded first-sync backfill, then sliding-hour slots."""
        key = self.key + ":writes:" + env_name
        now = self.now()
        slots = [(self.state.next_due(f"{key}:{i}"), i) for i in range(WRITE_BUDGET[0])]
        due, slot = min(slots)
        if due <= now:
            self.state.defer(f"{key}:{slot}", now + WRITE_BUDGET[1])
            return True
        if self.backfill:
            used = self.state.next_due(self.bkey)
            if used < BACKFILL_WRITES:
                extra_due = self.state.next_due(self.bkey + "-next")
                if extra_due > now and not retry:
                    self.state.defer(self.key, min(due, extra_due))
                    return False
                self.state.defer(self.bkey, used + 1)
                if not retry:
                    self.state.defer(self.bkey + "-next", now + LIVE_TICK_SECONDS)
                return True
            self.backfill = False
        self.state.defer(self.key, due)
        return False

    def cycle(self, *, once=False):
        # Freeze targets before cursor requests, which may take time on reconnect.
        config = dict(self.config, _state=self.state)
        config["_discovered"] = self.sources.discover(config)
        config["targets"] = self.sources.snapshot_targets(config)
        failed = self.initialize(config["_discovered"], once=once)
        initializing = True
        # scan commits immutable ops even if the feed or network is unavailable.
        while True:
            ops = self.sources.scan(config, self.state, now=self.now())
            self.backfill = not once and self._backfill(config["_discovered"], ops)

            # Refused streams do not slow backfill while others can progress;
            # with nothing to make progress on, or a deferred feed, keep run()'s backoff.
            if (
                initializing
                and self.backfill
                and not self.initialize_transient
                and self.state.next_due(self.key) <= self.now()
                and any(
                    self.state.cursor(s.source, s.path, s.native_id) is not None
                    for s in config["_discovered"]
                )
            ):
                failed = 0
            initializing = False
            if not ops or not self.enabled:
                return failed
            if not once and self.state.next_due(self.key) > self.now():
                return failed
            op = ops[0]
            try:
                body = self.sources.body(op)
            except self.sources.SliceConflict:
                self._status(op, "conflict")
                failed = 1
                continue
            env_name = TOKEN_ENV[op.source]
            token = self.tokens.get(env_name)
            if not token:
                raise ConfigError("Missing sync token environment variable")
            if not self._budget(env_name):
                return failed
            self.state.outbox_attempt(op.op_id)
            route = "/api/capture" if op.kind == "capture" else "/api/import"
            try:
                status, payload = self.transport(self.target, token, "POST", route, body)
                if status == 400 and op.kind == "capture" and "client_op_id" in body:
                    if not self._budget(env_name, retry=True):
                        return failed
                    legacy = {k: v for k, v in body.items() if k != "client_op_id"}
                    status, payload = self.transport(self.target, token, "POST", route, legacy)
            except (OSError, http.client.HTTPException, ValueError):
                return 1
            if status == 429:
                self._defer(payload, self.key)
                # A refused stream keeps run()'s backoff while the feed waits.
                return max(failed, int(bool(self.unavailable)))
            if status == 409:
                self.refresh = True
                off = "off" in str(payload.get("error", "")).lower() and payload.get(
                    "code"
                ) not in {"prefix_conflict", "op_conflict"}
                self._status(op, "paused" if off else "conflict")
                if off:
                    self.enabled = False
                if off:
                    return 0
                failed = 1
                continue
            if status in (408, 425):
                return 1
            if 400 <= status < 500:
                self.refresh |= status == 401
                self._status(op, "revoked")
                failed = 1
                continue
            if status != 200:
                return 1
            if op.kind == "capture" and payload.get("offset") != op.offset + op.length:
                return 1
            if op.kind == "import" and (payload.get("refused") or payload.get("accepted") != 1):
                return 1
            self.state.outbox_ack(op.op_id, payload)
            # Re-scan up to the original cycle targets, including post-replay growth.


def run(config, state, *, clock, sleep, transport, once=False, stop=None) -> int:
    """Run one bounded cycle or watch until stop(), using injected I/O and time."""
    state.retry_rejected()
    session = _Session(config, state, clock, transport)
    stop = stop or (lambda: False)
    backoff = 1
    executor = None
    future = None
    try:
        while not stop():
            feed_error = 0
            try:
                if future is None and (
                    once or session.interval == 0 or session.now() >= session.next_poll
                ):
                    feed_error = session.feed()
                elif future is not None and future.done():
                    feed_error, more = session.feed_result(future.result())
                    future = None
                    if more:
                        feed_error = max(feed_error, session.feed())
            except (OSError, http.client.HTTPException, ValueError):
                feed_error = 1
                if future is not None and future.done():
                    future = None
            try:
                result = max(feed_error, session.cycle(once=once))
            except ConfigError:
                raise
            except (OSError, http.client.HTTPException, ValueError):
                result = 1
            if session.refresh:
                session.refresh = False
                try:
                    session.feed()
                except (OSError, http.client.HTTPException, ValueError):
                    pass
            if once:
                return result
            if result:
                sleep(backoff)
                backoff = min(60, backoff * 2)
                continue
            backoff = 1
            if session.interval == 0 and session.signals and future is None:
                if executor is None:
                    executor = ThreadPoolExecutor(max_workers=1)
                # Only transport runs in the worker; all state commits stay here.
                future = executor.submit(session.transport, *session.feed_args(MAX_WAIT_SECONDS))
            sleep(
                LIVE_TICK_SECONDS
                if session.interval == 0
                else max(
                    LIVE_TICK_SECONDS,
                    min(
                        (
                            due
                            for due in (session.next_poll, state.next_due(session.key))
                            if due > session.now()
                        ),
                        default=session.now() + LIVE_TICK_SECONDS,
                    )
                    - session.now(),
                )
            )
        return 0
    finally:
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
