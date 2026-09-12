# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _startup_preflight() -> None:
    """Startup SQLite integrity + HNSW capacity probes, off the protocol thread.

    Runs the same checks the stdio loop used to run synchronously before
    reading the first request. Failures must never take down the server: the
    lazy consumers (_ensure_sqlite_integrity_status, _get_client) re-run or
    re-check on demand, so an exception here only loses the early warning.
    """
    try:
        _ensure_sqlite_integrity_status()
        _refresh_vector_disabled_flag()
    except Exception:
        logger.exception("startup preflight failed")


def _drop_broken_stdout() -> None:
    """Point fd 1 at devnull after a stdout write failed with a pipe error.

    The response line that failed mid-write can leave bytes buffered in
    ``sys.stdout``; the interpreter's shutdown flush would then re-raise
    ``BrokenPipeError`` and turn a clean exit into status 120. With fd 1
    on devnull that final flush drains harmlessly, so the process exits 0
    and any held flocks (e.g. ``mine_palace``) release via normal
    teardown.
    """
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


# Stdio→hub proxying. When a long-lived HTTP hub (`mempalace serve`) owns
# this palace, a stdio server spawned by an agent harness must not open its
# own Chroma handles: it could only ever be a read-only peer (writer lease,
# #1818), and racing writers are exactly what corrupted FTS5 indexes in the
# wild. Instead the stdio process forwards every JSON-RPC request to the hub
# verbatim, adding the local bearer token. This makes stdio-only harnesses
# (plugins, desktop apps) share the hub with zero client-side reconfiguration.
# Shares the CLI forwarder's kill switch (MEMPALACE_HUB_FORWARD=0).
_HUB_FORWARD_ENV = "MEMPALACE_HUB_FORWARD"
_HUB_PROXY_TIMEOUT_S = 600.0
_hub_proxy_announced = False


def _hub_proxy_target():
    """Return (base_url, headers) for a live hub serving our palace, or None."""
    global _hub_proxy_announced
    if _truthy_env_off(_HUB_FORWARD_ENV):
        return None
    try:
        from .. import server_registry

        info = server_registry.read_live_serverinfo(_config.palace_path)
        if not info or info.get("pid") == os.getpid():
            return None
        base_url = server_registry.client_base_url(info)
        headers = {"Content-Type": "application/json"}
    except Exception:
        logger.debug("hub discovery failed; serving locally", exc_info=True)
        return None
    if not _hub_proxy_announced:
        _hub_proxy_announced = True
        logger.info("Live palace hub detected at %s; proxying stdio requests to it", base_url)
    return base_url, headers


def _truthy_env_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def _forward_request_to_hub(base_url: str, headers: dict, request: dict, palace_path: str):
    """POST one JSON-RPC request to the hub; None for notifications (202)."""
    from .. import server_registry

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


def _request_is_mutating(request: dict) -> bool:
    if request.get("method") != "tools/call":
        return False
    name = ((request.get("params") or {}).get("name")) or ""
    return name in _MUTATING_TOOLS


def _dispatch_stdio_request(request: dict):
    """Route one stdio request: live hub first, local handling otherwise.

    The hub check runs per request (a tiny local JSON read), so a hub that
    starts, restarts on a new port, or dies mid-session is picked up without
    restarting this process. Fallback to local handling is allowed only when
    the failure provably happened before the hub accepted the request — a
    mutating call that failed mid-flight must NOT be replayed locally (the
    hub may still be executing it), so it surfaces as a JSON-RPC error.
    """
    import urllib.error

    target = _hub_proxy_target()
    if target is None:
        return handle_request(request)
    base_url, headers = target
    try:
        from ..mcp_proxy import _annotate_forwarded_update_status

        return _annotate_forwarded_update_status(
            request,
            _forward_request_to_hub(base_url, headers, request, _config.palace_path),
        )
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        reached_hub = isinstance(exc, urllib.error.HTTPError)
        if not reached_hub and not _request_is_mutating(request):
            logger.warning("Hub at %s unreachable (%s); handling request locally", base_url, exc)
            return handle_request(request)
        if request.get("id") is None:
            return None
        return {
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


def _run_stdio_loop() -> None:
    _restore_stdout()

    # Force UTF-8 on stdio. MCP JSON-RPC is UTF-8, but Python on Windows
    # defaults stdin/stdout to the system codepage (e.g. cp1251), which
    # corrupts non-ASCII payloads and surfaces as generic -32000 errors on
    # Cyrillic/CJK content. See PEP 540.
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    logger.info("MemPalace MCP Server starting...")

    # Pre-flight in a background thread: PRAGMA quick_check reads every page
    # of chroma.sqlite3 (20s+ on multi-GB palaces) and running it before the
    # protocol loop starves the client's initialize timeout, even though the
    # handshake itself never touches the database. The #1222 intent (warnings
    # visible at startup rather than on first use) is preserved — the probe
    # starts now and logs as soon as it finishes; tool calls that need the
    # verdict serialize on _sqlite_integrity_refresh_lock via
    # _ensure_sqlite_integrity_status instead of re-running the probe.
    threading.Thread(
        target=_startup_preflight,
        name="mcp-startup-preflight",
        daemon=True,
    ).start()

    # Opt-in: pre-load the embedder so the first chromadb-write tool call
    # does not pay the ONNX/CoreML cold-load tax under the MCP client
    # timeout (#1495). Default off — preserves current startup latency.
    _maybe_eager_warmup_embedder()

    # Idle auto-exit: release ChromaDB file handles from stale servers
    # that outlived their Claude Code session (#1552).
    _start_idle_exit_watchdog()

    # Say so when a chromadb write stops coming back, from a thread the stuck
    # call is not blocking.
    _start_write_stall_watchdog()

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            break
        except OSError as exc:
            # An orphaned pty/pipe surfaces as EIO/EBADF here instead of a
            # clean EOF — same meaning: the client is gone. Never loop on
            # it: an orphaned stdio server holding the mine_palace flock
            # blocked all palace writes for hours (2026-07-10 outage).
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
            response = _dispatch_stdio_request(request)
            if response is not None:
                payload = json.dumps(response, ensure_ascii=False)
        except KeyboardInterrupt:
            break
        except Exception as e:
            logger.error(f"Server error: {e}")
            continue

        if payload is None:
            continue
        try:
            sys.stdout.write(payload + "\n")
            sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except (BrokenPipeError, OSError) as exc:
            # The client's read end is gone; every future response write
            # would fail the same way, so treat it like stdin EOF and
            # shut down instead of swallowing it in the generic handler.
            logger.info("stdout write failed (%s) -- client disconnected, shutting down", exc)
            _drop_broken_stdout()
            break


def _run_http_loop() -> None:
    # In HTTP mode there is no JSON-RPC stdio channel. Keeping the import-time
    # stdout->stderr guard in place means any accidental print from a dependency
    # still cannot masquerade as an HTTP response.
    logger.info("MemPalace MCP HTTP server starting...")

    # A writable HTTP server is a long-lived storage client, so it must own the
    # local palace before it binds. Refusing at startup avoids advertising a
    # writable service that will only fail (or race) on its first mutation.
    # Explicit read-only HTTP remains safe to run beside the one writer owner.
    owns_writer_lease = False
    if not _READ_ONLY:
        writer_ok, writer_reason = _acquire_mcp_writer_lock()
        if not writer_ok:
            logger.error("Writable MCP HTTP startup refused: %s", writer_reason)
            raise SystemExit(2)
        owns_writer_lease = True

    try:
        # The HTTP transport exists for long-lived deployments. Do the cheap
        # filesystem-only probe before binding, but never make the listener wait on
        # optional embedder/HNSW warmup. Operators and tests should see /healthz as
        # soon as the process is alive.
        _refresh_vector_disabled_flag()
        _start_idle_exit_watchdog()
        _start_write_stall_watchdog()

        raw_warmup = os.environ.get("MEMPALACE_EAGER_WARMUP", "").strip().lower()
        if raw_warmup in _WARMUP_TRUTHY:

            def _warmup_with_lock():
                with _HTTP_REQUEST_LOCK:
                    _maybe_eager_warmup_embedder()

            threading.Thread(
                target=_warmup_with_lock,
                name="mcp-http-eager-warmup",
                daemon=True,
            ).start()
        elif raw_warmup and raw_warmup not in _WARMUP_FALSY:
            # Keep the same warning behavior as stdio mode for typo values.
            _maybe_eager_warmup_embedder()

        _serve_http(_args.host, _args.port)
    finally:
        if owns_writer_lease:
            # _serve_http uses daemon request threads, so synchronize with the
            # dispatch lock before closing storage and exposing the palace to
            # another process. Response serialization happens after this lock
            # and no longer touches the backend.
            with _HTTP_REQUEST_LOCK:
                _release_mcp_writer_lock()


def _install_shutdown_signal_handlers() -> None:
    """Route terminal signals through ``sys.exit`` so ``atexit`` runs.

    The palace writer lease is released by an ``atexit`` callback registered
    when the lock is acquired. CPython's default disposition for SIGTERM and
    SIGHUP is immediate termination, which skips ``atexit`` and leaves
    ``mine_palace_*.lock`` naming a dead PID until a contender's liveness
    check reclaims it (#2205). Calling ``sys.exit(0)`` from the handler
    unwinds the synchronous stdio/http loop and runs the existing release
    path. SIGHUP is Unix-only (SSH session disconnect); Windows only gets
    SIGTERM. Handlers are best-effort — signal registration only works from
    the main thread and is a no-op when the platform omits the signal.
    """
    import signal

    def _shutdown_handler(signum, frame):  # noqa: ARG001
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGHUP"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _shutdown_handler)
        except (ValueError, OSError):
            # Not in the main thread, or the platform rejects the install.
            pass


def main():
    """MCP server entry point for the ``mempalace-mcp`` console script.

    Side effect: pops ``PYTHONPATH`` from ``os.environ`` (see #1423) so any
    subprocess this server spawns inherits a clean env. Host applications that
    call ``main()`` programmatically should be aware that the parent process
    loses ``PYTHONPATH`` as well. Library imports do NOT trigger this side
    effect; only the CLI/MCP entry point does.

    Transports:
    - ``stdio`` remains the default for existing Claude/MCP deployments.
    - ``http`` is opt-in and serves JSON-RPC POSTs at ``/mcp`` in the same
      process, avoiding the long-lived stdio framing failure surface from
      #1801.
    """

    # Drop leaked PYTHONPATH so any subprocess this server spawns starts
    # with a clean env. The sys.path filter in mempalace/__init__.py
    # already protects this process from the same ABI mismatch; here we
    # extend the protection to children.
    os.environ.pop("PYTHONPATH", None)

    _install_shutdown_signal_handlers()

    # Consent is persisted locally and defaults off. When enabled, refresh the
    # release cache in the background so the first agent status call never
    # waits on PyPI and can naturally surface a newly available version.
    from ..update_awareness import schedule_update_check

    schedule_update_check()

    if _args.transport == "http":
        _run_http_loop()
    else:
        _run_stdio_loop()
