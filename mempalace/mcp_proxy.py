"""Thin stdio front end for the MemPalace MCP server.

``mempalace-mcp`` is spawned once per agent session, and when a hub is
running every one of those processes is a pure proxy: ``_dispatch_stdio_request``
forwards each JSON-RPC request over HTTP and the local storage stack is never
touched. Importing :mod:`mempalace.mcp_server` to do that costs ~77 MB anyway,
because chromadb (+61 MB on its own), numpy, pydantic, grpc and opentelemetry
are all pulled in at module scope. A fleet of 50 agents therefore paid ~3.9 GB
to hold proxies that do no work.

This module is the entry point instead. It imports only the standard library
plus :mod:`mempalace.config` and :mod:`mempalace.server_registry` (~5 MB each),
so a proxied session runs at roughly 22 MB. The full server is imported lazily,
and only when this process actually has to serve a request itself.

The fallback is deliberately preserved: a session whose hub dies keeps working.
It just stops being free at that point, so it says so — once to the log, and on
the tool result itself, because the agent driving the session is the one who
needs to know its memory backend changed shape underneath it.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import urllib.error

from .hub_client import HUB_FORWARD_ENV, HUB_PROXY_TIMEOUT_S, discover_hub

from .update_awareness import cached_update_status, schedule_update_check

logger = logging.getLogger(__name__)

# Shared with the in-server forwarder and the CLI forwarder.
_HUB_FORWARD_ENV = HUB_FORWARD_ENV
_HUB_PROXY_TIMEOUT_S = HUB_PROXY_TIMEOUT_S

_DEGRADED_NOTICE = (
    "MemPalace is running WITHOUT its shared hub. This session is now serving "
    "the palace directly, which loads the whole index into this process "
    "(hundreds of MB) instead of reusing the hub's. Memory tools still work. "
    "If several agents are running, expect memory pressure until the hub is "
    "back — check that the MemPalace hub process is alive."
)


def _is_plain_stdio_invocation(argv: list) -> bool:
    """True when this is an ordinary stdio session that a hub could serve.

    Anything that asks for a different transport, or that configures the
    serving process itself, goes straight to the full server. Being wrong in
    this direction only costs the old startup weight; being wrong the other
    way would silently drop a flag, so unknown arguments count as "not plain".
    """
    allowed_flags = {"--palace", "--collection", "--backend"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--transport":
            if i + 1 >= len(argv) or argv[i + 1] != "stdio":
                return False
            i += 2
            continue
        if arg.startswith("--transport="):
            if arg.split("=", 1)[1] != "stdio":
                return False
            i += 1
            continue
        if arg in allowed_flags:
            # The server's parser exits on --palace or --backend without a value
            # (--collection it does not define, and skips). The local fallback
            # would only parse this once the hub is gone, ending the session; the
            # full server refuses it at startup instead.
            valueless = i + 1 >= len(argv) or argv[i + 1].startswith("-")
            if valueless and arg != "--collection":
                return False
            i += 1 if valueless else 2
            continue
        if any(arg.startswith(flag + "=") for flag in allowed_flags):
            i += 1
            continue
        return False
    return True


def _palace_path(argv: list):
    """Resolve the palace path without importing the server."""
    for i, arg in enumerate(argv):
        if arg == "--palace" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--palace="):
            return arg.split("=", 1)[1]
    try:
        from .config import MempalaceConfig

        return MempalaceConfig().palace_path
    except Exception:
        logger.debug("palace path unresolved; serving locally", exc_info=True)
        return None


def _hub_target(palace_path):
    """Return ``(base_url, headers)`` for a live hub serving our palace, else None."""
    return discover_hub(palace_path)


def _forward(base_url: str, headers: dict, request: dict, palace_path: str):
    """POST one JSON-RPC request to the hub; None for notifications (202)."""
    from . import server_registry

    body = json.dumps(request, ensure_ascii=False).encode("utf-8")
    with server_registry.urlopen_with_server_tokens(
        palace_path,
        f"{base_url}/mcp",
        data=body,
        headers=headers,
        timeout=_HUB_PROXY_TIMEOUT_S,
    ) as resp:
        raw = resp.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def _annotate_degraded(response):
    """Prepend the hub-is-gone notice to a tools/call result.

    The driving agent only ever sees ``result.content``; a log line it cannot
    read is not a warning. Prepended rather than appended so it survives a
    client that renders only the first block, and only on tools/call, so
    tools/list and the handshake keep their exact shapes.
    """
    if not isinstance(response, dict):
        return response
    result = response.get("result")
    if not isinstance(result, dict):
        return response
    content = result.get("content")
    if not isinstance(content, list):
        return response
    result["content"] = [{"type": "text", "text": f"[mempalace] {_DEGRADED_NOTICE}"}, *content]
    return response


def _annotate_forwarded_update_status(request: dict, response):
    """Attach this proxy runtime's cached state beside the hub's state."""
    params = request.get("params")
    if not isinstance(params, dict):
        params = {}
    if request.get("method") != "tools/call" or params.get("name") != "mempalace_status":
        return response

    local_status = cached_update_status()
    schedule_update_check()
    try:
        content = response["result"]["content"]
    except (KeyError, TypeError):
        return response
    for block in content if isinstance(content, list) else ():
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        try:
            payload = json.loads(block.get("text", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        remote_updates = payload.get("updates")
        if not isinstance(remote_updates, dict):
            remote_updates = {}
        elif "server" not in remote_updates and "client" not in remote_updates:
            remote_updates = {"server": remote_updates}
        payload["updates"] = {**remote_updates, "client": local_status}
        block["text"] = json.dumps(payload, indent=2, ensure_ascii=False)
        break
    return response


def _import_server():
    """Import the full server, and hand stdout back if the import fails.

    The import moves fd 1 onto stderr before anything in it that can fail, and
    only the server's own _restore_stdout undoes that. A failed import takes
    that function with it, so every answer this process wrote afterwards would
    reach stderr instead of the client.
    """
    try:
        saved_fd = os.dup(1)
    except OSError:
        saved_fd = None
    saved_stdout = sys.stdout
    try:
        from . import mcp_server
    except BaseException:
        if saved_fd is not None:
            os.dup2(saved_fd, 1)
        sys.stdout = saved_stdout
        raise
    finally:
        if saved_fd is not None:
            os.close(saved_fd)
    return mcp_server


class _LocalServer:
    """Lazily-imported full server, plus the background services it expects.

    Import is deferred to the first request this process has to answer itself,
    which is the whole point of this module: a proxied session never pays it.
    """

    def __init__(self):
        self._module = None
        self._load_error: BaseException | None = None

    @property
    def loaded(self) -> bool:
        return self._module is not None

    def load(self):
        if self._load_error is not None:
            raise self._load_error
        if self._module is None:
            logger.warning(
                "MemPalace hub unavailable; serving this session locally. "
                "Loading the local storage stack (this process will grow)."
            )
            try:
                mcp_server = _import_server()

                # Importing the server installs its stdio protection: os.dup2(2, 1)
                # plus sys.stdout = sys.stderr, so stray library prints cannot
                # corrupt JSON-RPC. That also redirects *our* responses to stderr —
                # fd 1 itself is moved, so holding a reference to the old object is
                # not enough. _restore_stdout undoes both levels, exactly as the
                # server's own stdio loop does before it starts answering.
                mcp_server._restore_stdout()
                if hasattr(sys.stdout, "reconfigure"):
                    try:
                        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                    except (AttributeError, OSError):
                        pass

                # This path never runs the server's main(), so apply the flags main()
                # would have applied. After the stdout restore: a refused flag raises,
                # and the proxy keeps answering over stdout.
                args = mcp_server._parse_args(sys.argv[1:])
                mcp_server._apply_server_flags(
                    palace=args.palace, backend=args.backend, read_only=args.read_only
                )

                for start in (
                    mcp_server._start_idle_exit_watchdog,
                    mcp_server._start_write_stall_watchdog,
                ):
                    try:
                        start()
                    except Exception:
                        logger.debug("local service %s failed to start", start, exc_info=True)
                self._module = mcp_server
            except BaseException as exc:
                # The server's own import dups stdout before it can fail, and a
                # failed import drops the module without closing that dup.
                # Remember the failure so the next request does not dup again.
                self._load_error = exc
                raise
        return self._module


def _proxy_error(request: dict, base_url: str, exc: Exception, local_error=None):
    """Mirror the in-server proxy failure shape for a request we must not replay.

    ``local_error`` is why this process could not serve the request either.
    """
    if request.get("id") is None:
        return None
    response = {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": -32000,
            "message": f"palace hub proxy failed: {exc}",
            "data": {
                "hub": base_url,
                "hint": (
                    "The palace hub did not complete this request. Mutating tools "
                    "are not replayed locally — the hub may still be executing the "
                    "call. Check the hub process, then retry."
                ),
            },
        },
    }
    if local_error is not None:
        response["error"]["data"]["local_server"] = f"could not start: {local_error}"
    return response


def _json_rpc_error(req_id, code: int, message: str) -> dict:
    """Mirrors the server's helper of that name; importing it would load the storage stack."""
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _local_server_error(request: dict, exc: Exception):
    """Answer a request only this process could serve, once its local server could not start."""
    if request.get("id") is None:
        return None
    return _json_rpc_error(
        request.get("id"),
        -32000,
        f"MemPalace hub unavailable and the local server could not start: {exc}",
    )


def _handle(request: dict, palace_path, local: _LocalServer):
    """Route one request: live hub first, this process otherwise."""
    if not isinstance(request, dict):
        # What the full server answers it with.
        return _json_rpc_error(None, -32600, "Invalid Request")
    target = _hub_target(palace_path)
    if target is not None:
        base_url, headers = target
        try:
            return _annotate_forwarded_update_status(
                request, _forward(base_url, headers, request, palace_path)
            )
        except (
            urllib.error.URLError,
            OSError,
            TimeoutError,
            ValueError,
            http.client.HTTPException,
        ) as exc:
            # Reaching the hub and getting an HTTP error means it may have run
            # the call; so does any mid-flight failure on a mutating tool, an
            # answer that broke off (IncompleteRead, BadStatusLine) included.
            # Neither may be replayed here, and nothing can be when the local
            # server cannot start.
            try:
                module = local.load()
            except Exception as load_exc:
                logger.error(
                    "Hub at %s failed (%s), and the local server could not start: %s",
                    base_url,
                    exc,
                    load_exc,
                )
                return _proxy_error(request, base_url, exc, local_error=load_exc)
            if isinstance(exc, urllib.error.HTTPError) or module._request_is_mutating(request):
                return _proxy_error(request, base_url, exc)
            logger.warning("Hub at %s unreachable (%s); handling request locally", base_url, exc)
            return _annotate_degraded(module.handle_request(request))
    try:
        module = local.load()
    except Exception as exc:
        logger.error("Local server could not start: %s", exc)
        return _local_server_error(request, exc)
    return _annotate_degraded(module.handle_request(request))


def _run_proxy_loop(palace_path) -> None:
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    local = _LocalServer()
    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            break
        except OSError as exc:
            logger.info("stdin read failed (%s) -- client disconnected, shutting down", exc)
            break
        if not line:
            logger.info("stdin EOF -- client disconnected, shutting down")
            break
        line = line.strip()
        if not line:
            continue

        payload = None
        try:
            request = json.loads(line)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            # Whatever json.loads rejects, nesting too deep to parse included,
            # leaves the id unknowable: the answer carries a null one, as the
            # hub's HTTP transport answers it.
            logger.error("Server error: %s", exc)
            payload = json.dumps(_json_rpc_error(None, -32700, "Parse error"), ensure_ascii=False)
        else:
            try:
                response = _handle(request, palace_path, local)
                if response is not None:
                    payload = json.dumps(response, ensure_ascii=False)
            except KeyboardInterrupt:
                break
            except Exception:
                # The client gets the full server's generic -32603, so the
                # traceback is the only record of what failed.
                logger.exception("Server error")
                req_id = request.get("id") if isinstance(request, dict) else None
                if req_id is None:
                    # A notification is owed no response, failure included.
                    continue
                payload = json.dumps(
                    _json_rpc_error(req_id, -32603, "Internal error"), ensure_ascii=False
                )

        if payload is None:
            continue
        try:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except (BrokenPipeError, OSError) as exc:
            logger.info("stdout write failed (%s) -- client disconnected, shutting down", exc)
            break


def main() -> None:
    """Entry point for ``mempalace-mcp``.

    Delegates to the full server for anything but a plain stdio session, and
    for a plain stdio session with no hub to proxy to — in both cases the
    heavy import was going to happen regardless.
    """
    argv = sys.argv[1:]
    if not _is_plain_stdio_invocation(argv):
        from . import mcp_server

        return mcp_server.main()

    palace_path = _palace_path(argv)
    if _hub_target(palace_path) is None:
        from . import mcp_server

        return mcp_server.main()

    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    _run_proxy_loop(palace_path)
