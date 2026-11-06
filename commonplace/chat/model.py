"""Bounded, explicit model transport without retries or provider error text."""

import http.client
import json
import logging
import os
import socket
import threading
import time
from urllib.parse import urlsplit

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
    body = {
        "model": settings.model,
        "max_tokens": min(max_tokens, MAX_OUTPUT_TOKENS),
        "temperature": 0,
    }
    if anthropic:
        headers.update({"anthropic-version": ANTHROPIC_VERSION})
        if key is not None:
            headers["x-api-key"] = key
        body.update(system=system, messages=turns)
    else:
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        body.update(stream=False, messages=[{"role": "system", "content": system}, *turns])
    payload = json.dumps(body).encode()
    deadline = time.monotonic() + timeout

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ModelError("timeout")
        return seconds

    class DeadlineHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            http.client.HTTPConnection.connect(self)
            self.sock.settimeout(remaining())
            self.sock = self._context.wrap_socket(
                self.sock, server_hostname=self.host, do_handshake_on_connect=False
            )
            self.sock.do_handshake()

    cls = DeadlineHTTPSConnection if origin.scheme == "https" else http.client.HTTPConnection
    conn = cls(origin.hostname, origin.port, timeout=remaining())
    response = None
    expired = threading.Event()
    sockets = []

    def expire():
        expired.set()
        _shutdown_sockets([conn.sock, *sockets])

    def connect_socket(address, timeout=None, source_address=None):
        return _connect_socket(
            address, remaining, sockets, source_address, local=front_settings.is_local(settings)
        )

    conn._create_connection = connect_socket
    timer = threading.Timer(remaining(), expire)
    timer.daemon = True
    timer.start()
    try:
        conn.connect()
        sock = conn.sock
        if sock not in sockets:
            sockets.append(sock)
        sock.settimeout(remaining())
        path = origin.path.rstrip("/") + ("/v1/messages" if anthropic else "/v1/chat/completions")
        conn.request("POST", path, payload, headers)
        if on_sent is not None:
            on_sent()
        sock.settimeout(remaining())

        conn.response_class = _bounded_response(remaining)
        response = conn.getresponse()
        remaining()
        if response.status != 200:
            raise ModelError("http_error")
        chunks = bytearray()
        while not response.isclosed():
            # Keep the socket reference: HTTP/1.0 responses detach it from conn.
            sock.settimeout(remaining())
            chunk = response.read1(min(8192, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            remaining()
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ModelError("bad_response")
        return _answer(chunks, anthropic)
    except TimeoutError:
        raise ModelError("timeout") from None
    except ValueError:
        if expired.is_set():
            raise ModelError("timeout") from None
        raise
    except (OSError, http.client.HTTPException):
        raise ModelError("timeout" if expired.is_set() else "http_error") from None
    finally:
        timer.cancel()
        if response is not None:
            response.close()
        conn.close()


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
