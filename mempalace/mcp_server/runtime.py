# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


def _startup_preflight() -> None:
    """Startup SQLite integrity + HNSW capacity probes, off the protocol thread.

    Failures must never take down the server: the
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
# own Chroma handles: it could only ever be a read-only peer (writer lease),
# and racing writers corrupt FTS5 indexes. Instead the stdio process forwards every JSON-RPC request to the hub
# verbatim, adding the local bearer token. This makes stdio-only harnesses
# (plugins, desktop apps) share the hub with zero client-side reconfiguration.
# Shares the CLI forwarder's kill switch (MEMPALACE_HUB_FORWARD=0).
_hub_proxy_announced = False


def _hub_proxy_target():
    """The live hub serving our palace, or None."""
    global _hub_proxy_announced
    from ..hub_client import HubClient

    hub = HubClient.discover(_config.palace_path)
    if hub is not None and not _hub_proxy_announced:
        _hub_proxy_announced = True
        logger.info("Live palace hub detected at %s; proxying stdio requests to it", hub.base_url)
    return hub


def _request_is_mutating(request: dict) -> bool:
    if request.get("method") != "tools/call":
        return False
    # This decides whether a mid-flight failure may be replayed locally, so it
    # must return a verdict rather than raise: the same `or {}` trap made a
    # non-mapping `params` throw AttributeError instead of answering "not
    # mutating", and an unhashable name broke the membership test. A call that
    # changes state outside the palace must not be rerun either, so this reads
    # read-only mode's set rather than the palace-write one.
    _, params = _normalize_envelope(request)
    name = params.get("name")
    return isinstance(name, str) and name in _READ_ONLY_REFUSED_TOOLS


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

    from ..hub_client import FORWARD_ERRORS, proxy_error_response

    hub = _hub_proxy_target()
    if hub is None:
        return handle_request(request)
    try:
        from ..mcp_proxy import _annotate_forwarded_update_status

        return _annotate_forwarded_update_status(request, hub.post(request))
    except FORWARD_ERRORS as exc:
        reached_hub = isinstance(exc, urllib.error.HTTPError)
        if not reached_hub and not _request_is_mutating(request):
            logger.warning(
                "Hub at %s unreachable (%s); handling request locally", hub.base_url, exc
            )
            return handle_request(request)
        return proxy_error_response(request, hub.base_url, exc)


def _stdio_reply(line: str, dispatch) -> Optional[str]:
    """The JSON-RPC text owed for one stdio line, or None when none is owed.

    Shared by this server and the light server's stdio loop.
    """
    try:
        request = json.loads(line)
    except Exception as exc:
        # ValueError (digit limit) and RecursionError (nesting) are still
        # parse failures, so answer -32700 with a null id the way the HTTP
        # transport does. The type is in the log because MemoryError has
        # no message of its own.
        logger.error("Server error: %s: %s", type(exc).__name__, exc)
        return dumps_client(_json_rpc_parse_error())
    try:
        response = dispatch(request)
    except Exception:
        # Log with the traceback: the client only gets a generic -32603, so
        # the stack is the only record of what failed.
        logger.exception("Server error")
        req_id = request.get("id") if isinstance(request, dict) else None
        if req_id is None:
            # A notification is owed no response, failure included.
            return None
        return dumps_client(_json_rpc_internal_error(req_id))
    return None if response is None else dumps_client(response)


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
    # handshake itself never touches the database. Warnings still show at
    # startup rather than on first use: the probe starts now and logs as soon
    # as it finishes; tool calls that need the
    # verdict serialize on _sqlite_integrity_refresh_lock via
    # _ensure_sqlite_integrity_status instead of re-running the probe.
    threading.Thread(
        target=_startup_preflight,
        name="mcp-startup-preflight",
        daemon=True,
    ).start()

    # Opt-in: pre-load the embedder so the first chromadb-write tool call
    # does not pay the ONNX/CoreML cold-load tax under the MCP client
    # timeout. Default off — preserves current startup latency.
    _maybe_eager_warmup_embedder()

    # Idle auto-exit: release ChromaDB file handles from stale servers
    # that outlived their Claude Code session.
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

        try:
            payload = _stdio_reply(line, _dispatch_stdio_request)
        except KeyboardInterrupt:
            break
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


_MCP_WRITER_WAIT_SECONDS_ENV = "MEMPALACE_MCP_WRITER_WAIT_SECONDS"
_MCP_WRITER_WAIT_SECONDS_DEFAULT = 120.0
_MCP_WRITER_WAIT_MAX_DELAY = 5.0


def _writer_wait_seconds() -> float:
    """How long a writable HTTP server waits for a peer to release the writer lease.

    ``MEMPALACE_MCP_WRITER_WAIT_SECONDS`` (default 120). ``0`` restores the old
    behaviour of refusing immediately. A value that is not a finite,
    non-negative number falls back to the default with a warning.
    """
    seconds = env_float(_MCP_WRITER_WAIT_SECONDS_ENV, _MCP_WRITER_WAIT_SECONDS_DEFAULT)
    return seconds if seconds >= 0 else _MCP_WRITER_WAIT_SECONDS_DEFAULT


def _acquire_writer_lease_for_http_startup() -> tuple[bool, str]:
    """Acquire the writer lease for writable HTTP startup, waiting if a peer holds it.

    Refusing at once would turn a transient holder into an outage. A peer
    session's MCP server, a hook-driven mine or a CLI write that holds the
    lease releases it when it finishes, but ``SystemExit(2)`` before binding
    means nothing retries, and under ``Restart=always`` the unit would restart
    forever without ever starting. So the server retries for a bounded time
    with backoff, logging once when the wait starts, and still exits 2 when
    the wait runs out so a supervisor sees a terminal failure.

    Only contention waits. ``_acquire_mcp_writer_lock`` sets
    ``_MCP_WRITER_READ_ONLY`` when another writer holds the lock; a setup
    failure (backend or lock directory) leaves it unset, and waiting would not
    fix that, so it returns at once as before.
    """
    ok, reason = _acquire_mcp_writer_lock()
    if ok or not _MCP_WRITER_READ_ONLY:
        return ok, reason

    budget = _writer_wait_seconds()
    if budget <= 0:
        return ok, reason

    logger.warning(
        "Writable MCP HTTP startup is waiting up to %g s for the writer lease: %s",
        budget,
        reason,
    )
    started = time.monotonic()
    deadline = started + budget
    delay = 0.5
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, reason
        time.sleep(min(delay, remaining))
        delay = min(delay * 2, _MCP_WRITER_WAIT_MAX_DELAY)
        ok, reason = _acquire_mcp_writer_lock()
        if ok:
            logger.info(
                "Writable MCP HTTP startup acquired the writer lease after %.1f s",
                time.monotonic() - started,
            )
            return True, ""
        if not _MCP_WRITER_READ_ONLY:
            return False, reason


def _run_http_loop() -> None:
    # In HTTP mode there is no JSON-RPC stdio channel. Keeping the import-time
    # stdout->stderr guard in place means any accidental print from a dependency
    # still cannot masquerade as an HTTP response.
    logger.info("MemPalace MCP HTTP server starting...")

    # A writable HTTP server is a long-lived storage client, so it must own the
    # local palace before it binds. Refusing at startup avoids advertising a
    # writable service that will only fail (or race) on its first mutation.
    # A peer holding the lease is waited out for a bounded time first.
    # Explicit read-only HTTP remains safe to run beside the one writer owner.
    owns_writer_lease = False
    if not _READ_ONLY:
        writer_ok, writer_reason = _acquire_writer_lease_for_http_startup()
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

        if env_bool("MEMPALACE_EAGER_WARMUP", False):

            def _warmup_with_lock():
                with _HTTP_REQUEST_LOCK:
                    _maybe_eager_warmup_embedder()

            threading.Thread(
                target=_warmup_with_lock,
                name="mcp-http-eager-warmup",
                daemon=True,
            ).start()

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
    check reclaims it. Calling ``sys.exit(0)`` from the handler
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

    Parses ``sys.argv`` and applies ``--palace`` / ``--backend`` / ``--read-only``
    to the process, including ``MEMPALACE_PALACE_PATH`` and ``MEMPALACE_BACKEND``
    in ``os.environ``; importing the package parses no argv.

    Side effect: pops ``PYTHONPATH`` from ``os.environ`` so any
    subprocess this server spawns inherits a clean env. Host applications that
    call ``main()`` programmatically should be aware that the parent process
    loses ``PYTHONPATH`` as well. Library imports do NOT trigger this side
    effect; only the CLI/MCP entry point does.

    Transports:
    - ``stdio`` remains the default for existing Claude/MCP deployments.
    - ``http`` is opt-in and serves JSON-RPC POSTs at ``/mcp`` in the same
      process, avoiding the long-lived stdio framing failure surface.
    """
    global _args

    _args = _parse_args()
    _apply_server_flags(palace=_args.palace, backend=_args.backend, read_only=_args.read_only)

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
