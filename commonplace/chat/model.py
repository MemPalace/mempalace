"""Bounded, explicit model transport without retries or provider error text."""

import http.client
import json
import logging
import os
import socket
import threading
import time
from typing import NamedTuple
from urllib.parse import SplitResult, urlsplit

from commonplace.chat import settings as front_settings
from commonplace.chat.types import (
    ANTHROPIC_VERSION,
    KEY_HOSTS_ENV,
    MAX_INPUT_CHARS,
    MAX_OUTPUT_TOKENS,
    MAX_RESPONSE_BYTES,
    MODEL_TIMEOUT_S,
    ChatDenied,
    FrontSettings,
    ModelError,
)

ANTHROPIC_ORIGIN = "https://api.anthropic.com"


def _bounded_response(remaining):
    class BoundedResponse(http.client.HTTPResponse):
        def begin(self):
            original = self.fp

            class Headers:
                size = 0

                def readline(self, limit=-1):
                    remaining()
                    line = original.readline(min(limit if limit >= 0 else 65537, 65537))
                    self.size += len(line)
                    remaining()
                    if self.size > 65536:
                        raise ModelError("bad_response")
                    return line

                def __getattr__(self, name):
                    return getattr(original, name)

            self.fp = Headers()
            super().begin()
            self.fp = original

    return BoundedResponse


def _shutdown_sockets(sockets):
    for sock in sockets:
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except (OSError, ValueError):
                pass


def _connect_socket(address, remaining, sockets, source_address=None, *, local=False):
    last_error = None
    for family, kind, proto, _, target in socket.getaddrinfo(
        address[0], address[1], 0, socket.SOCK_STREAM
    ):
        remaining()
        if local and not front_settings.loopback_host(target[0]):
            logging.getLogger(__name__).warning("Local model address refused")
            raise OSError("Local model address refused")
        candidate = socket.socket(family, kind, proto)
        try:
            candidate.settimeout(remaining())
            if source_address:
                candidate.bind(source_address)
            candidate.connect(target)
            remaining()
        except OSError as exc:
            candidate.close()
            last_error = exc
            remaining()
            continue
        except BaseException:
            candidate.close()
            raise
        sockets.append(candidate)
        return candidate
    remaining()
    raise last_error or OSError("No model addresses")


class Endpoint(NamedTuple):
    anthropic: bool
    origin: SplitResult
    headers: dict
    path: str


def resolve_endpoint(settings: FrontSettings, key: str | None) -> Endpoint:
    """Validate the origin and key host for one call; no I/O."""
    anthropic = settings.provider == "anthropic"
    if not anthropic and settings.provider != "openai_compatible":
        raise ValueError("model provider unavailable")
    origin = urlsplit(ANTHROPIC_ORIGIN if anthropic else settings.base_url or "")
    if origin.scheme not in ("http", "https") or not origin.hostname:
        raise ValueError("invalid model origin")
    if key is not None and not anthropic:
        hosts = {host.strip().lower() for host in os.environ.get(KEY_HOSTS_ENV, "").split(",")}
        if origin.hostname.lower() not in hosts:
            raise ChatDenied("model key host not allowed")
    headers = {"Content-Type": "application/json"}
    if anthropic:
        headers.update({"anthropic-version": ANTHROPIC_VERSION})
        if key is not None:
            headers["x-api-key"] = key
    elif key is not None:
        headers["Authorization"] = f"Bearer {key}"
    path = origin.path.rstrip("/") + ("/v1/messages" if anthropic else "/v1/chat/completions")
    return Endpoint(anthropic, origin, headers, path)


class Call:
    """One deadline-bound request: checked sockets, bounded headers, error codes.

    Use as a context manager. The deadline shuts the sockets so a blocked read ends at
    once; `stop()` does the same on request. Socket-level failures leave the block as
    `ModelError` with only the codes `timeout`, `http_error` and `stopped`.
    """

    def __init__(self, settings: FrontSettings, endpoint: Endpoint, timeout: float):
        self.endpoint = endpoint
        self.deadline = time.monotonic() + timeout
        self.expired = threading.Event()
        self.stopped = threading.Event()
        self.closed = threading.Event()
        self.sockets = []
        self.response = None
        self.sock = None
        local = front_settings.is_local(settings)

        class DeadlineHTTPSConnection(http.client.HTTPSConnection):
            def connect(conn):
                http.client.HTTPConnection.connect(conn)
                conn.sock.settimeout(self.remaining())
                conn.sock = conn._context.wrap_socket(
                    conn.sock, server_hostname=conn.host, do_handshake_on_connect=False
                )
                conn.sock.do_handshake()

        origin = endpoint.origin
        cls = DeadlineHTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
        self.conn = cls(origin.hostname, origin.port, timeout=self.remaining())
        self.conn._create_connection = lambda address, timeout=None, source_address=None: (
            _connect_socket(address, self.remaining, self.sockets, source_address, local=local)
        )
        self.timer = threading.Timer(self.remaining(), self._expire)
        self.timer.daemon = True
        self.timer.start()

    def remaining(self):
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise ModelError("timeout")
        return seconds

    def _shutdown(self):
        _shutdown_sockets([self.conn.sock, *self.sockets])

    def _expire(self):
        self.expired.set()
        self._shutdown()

    def stop(self):
        self.stopped.set()
        self._shutdown()

    def watch(self, cancel: threading.Event):
        """Stop the call as soon as `cancel` is set, until the call is closed."""

        def run():
            while not self.closed.is_set():
                if cancel.wait(0.05):
                    self.stop()
                    return

        threading.Thread(target=run, daemon=True).start()

    def open(self, payload: bytes, on_sent=None):
        """Connect, send the request and return the 200 response."""
        conn = self.conn
        conn.connect()
        sock = conn.sock
        if sock not in self.sockets:
            self.sockets.append(sock)
        sock.settimeout(self.remaining())
        conn.request("POST", self.endpoint.path, payload, self.endpoint.headers)
        if on_sent is not None:
            on_sent()
        sock.settimeout(self.remaining())
        conn.response_class = _bounded_response(self.remaining)
        self.response = conn.getresponse()
        self.remaining()
        if self.response.status != 200:
            raise ModelError("http_error")
        # Keep the socket reference: HTTP/1.0 responses detach it from conn.
        self.sock = sock
        return self.response

    def read(self, size: int) -> bytes:
        self.sock.settimeout(self.remaining())
        chunk = self.response.read1(size)
        self.remaining()
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed.set()
        self.timer.cancel()
        if self.response is not None:
            self.response.close()
        self.conn.close()
        if exc_type is None:
            return False
        if self.stopped.is_set() and issubclass(
            exc_type, (OSError, ValueError, http.client.HTTPException)
        ):
            raise ModelError("stopped") from None
        if issubclass(exc_type, TimeoutError):
            raise ModelError("timeout") from None
        if issubclass(exc_type, ValueError):
            if self.expired.is_set():
                raise ModelError("timeout") from None
            return False
        if issubclass(exc_type, (OSError, http.client.HTTPException)):
            raise ModelError("timeout" if self.expired.is_set() else "http_error") from None
        return False


def complete(
    settings: FrontSettings,
    key: str | None,
    system: str,
    turns: list[dict],
    *,
    timeout: float = MODEL_TIMEOUT_S,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    on_sent=None,
) -> str:
    if len(system) + sum(len(turn["content"]) for turn in turns) > MAX_INPUT_CHARS:
        raise ValueError("model input too long")
    endpoint = resolve_endpoint(settings, key)
    anthropic = endpoint.anthropic
    body = {
        "model": settings.model,
        "max_tokens": min(max_tokens, MAX_OUTPUT_TOKENS),
        "temperature": 0,
    }
    if anthropic:
        body.update(system=system, messages=turns)
    else:
        body.update(stream=False, messages=[{"role": "system", "content": system}, *turns])
    payload = json.dumps(body).encode()
    with Call(settings, endpoint, timeout) as call:
        response = call.open(payload, on_sent)
        chunks = bytearray()
        while not response.isclosed():
            chunk = call.read(min(8192, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ModelError("bad_response")
        return _answer(chunks, anthropic)


def _answer(chunks, anthropic):
    try:
        data = json.loads(chunks)
        if anthropic:
            first = data["content"][0]
            if first["type"] != "text":
                raise ValueError
            answer = first["text"]
        else:
            answer = data["choices"][0]["message"]["content"]
        if not isinstance(answer, str):
            raise ValueError
    except (ValueError, KeyError, IndexError, TypeError):
        raise ModelError("bad_response") from None
    return answer


def probe_models(config):
    """Local discovery uses the same checked socket path as completions."""
    if not front_settings.is_local(config):
        return [], False
    origin = urlsplit(config.base_url)
    sockets = []
    deadline = time.monotonic() + 2

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError()
        return value

    conn = (
        http.client.HTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    )(origin.hostname, origin.port, timeout=2)
    conn._create_connection = lambda address, timeout=None, source_address=None: _connect_socket(
        address, remaining, sockets, source_address, local=front_settings.is_local(config)
    )
    timer = threading.Timer(2, lambda: _shutdown_sockets([conn.sock, *sockets]))
    timer.daemon = True
    timer.start()
    try:
        conn.request("GET", origin.path.rstrip("/") + "/v1/models")
        response = conn.getresponse()
        if response.status != 200:
            return [], False
        data = response.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            return [], False
        rows = json.loads(data)["data"]
        if not isinstance(rows, list):
            return [], False
        return [
            row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
        ][:200], True
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException):
        return [], False
    finally:
        timer.cancel()
        conn.close()
