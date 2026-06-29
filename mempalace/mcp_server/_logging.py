# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


class _MempalaceLogFilter(logging.Filter):
    """Pass only records emitted by mempalace's own loggers.

    Lets the ``MEMPALACE_LOG_FILE`` handler attach to an already-configured
    root logger (a host app embedding the server) without copying the
    host's — or a third-party library's — records into mempalace's diagnostic
    file. Every mempalace module logs through ``logging.getLogger(__name__)``,
    so its records are ``mempalace`` or ``mempalace.*``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return name == "mempalace" or name.startswith("mempalace.")


# Preserved across importlib.reload via globals(): a reload re-executes this
# module body, so a plain ``= False`` would reset the guard and let
# _init_logging() stack a duplicate file handler. globals().get keeps the prior
# True so the guard survives reload.
_logging_configured = globals().get("_logging_configured", False)


def _init_logging() -> None:
    """Configure mempalace logging: stderr by default, optional file append.

    ``MEMPALACE_LOG_FILE``, when set, attaches a ``FileHandler`` so MCP-client
    failures the client never surfaces (e.g. a ``-32000`` cold-load timeout)
    stay diagnosable from the file.

    Root-logger ownership. The server must not hijack a host
    application's logging, so the two cases are handled differently:

    * **Root unconfigured** (standalone ``mempalace-mcp``): own it — a stderr
      handler (plus the optional file handler) via ``basicConfig`` at INFO.
    * **Root already configured** (an app imported ``mempalace.mcp_server``
      after setting up its own logging): leave the host's level, format, and
      handlers untouched. Attach only the file handler, filtered to
      mempalace's own records (`_MempalaceLogFilter`), so the host's logs do
      not bleed into mempalace's file. With ``MEMPALACE_LOG_FILE`` unset the
      root logger is not touched at all.

    Never ``logging.basicConfig(..., force=True)``: it resets root's
    handlers, level and format unconditionally and clobbers any host app that
    configured logging first. The filtered additive handler keeps the
    diagnostic file without that reset.

    The file handler is mempalace-filtered in both paths, so the file is a
    clean mempalace-only stream. In the embedded path mempalace's records are
    still subject to the host's root level — a host wanting INFO diagnostics in
    the file should not raise root above INFO. The standalone path pins INFO.

    Failure modes:

    * Invalid path (missing directory, no perms, Windows NUL byte) → the file
      handler is skipped with a warning naming ``MEMPALACE_LOG_FILE``; the
      server still starts. ``ValueError`` is in the catch because Windows
      raises it for embedded-NUL paths, not ``OSError``.
    * Concurrent writers (multiple ``mempalace-mcp`` processes at one path)
      interleave at the line level; append mode means nothing is overwritten,
      but give each process its own path.

    ``delay=True`` is intentionally NOT set: deferring the open moves an
    invalid-path error to ``emit()`` time (unhandled), defeating the fail-soft
    contract. Eager open lands the same error in ``FileHandler.__init__`` and
    our ``except`` below.

    Runs at import time (module-level call below) so importing the module for
    introspection (``TOOLS`` dict, handler functions) configures logging once.
    """
    global _logging_configured
    if _logging_configured:
        # Idempotent: a second call (e.g. importlib.reload) must not add a
        # duplicate file handler in the embedded path.
        return
    _logging_configured = True

    # MEMPALACE_LOG_FILE is operator-supplied and opt-in; this is a
    # local-first server (CLAUDE.md design principle), so no path
    # sanitization — the operator's process UID is the trust boundary.
    # When unset, default to ~/.mempalace/mcp_server.log so an unhandled crash
    # leaves a persistent record on disk — the MCP client swallows stderr, so
    # stderr-only is a silent-death black hole.
    #
    # Two things bound the default:
    #
    # * Standalone only. An unconfigured root logger is the standalone
    #   ``mempalace-mcp`` process — the one whose stderr the client swallows.
    #   When a host app already owns root, "with MEMPALACE_LOG_FILE
    #   unset the root logger is not touched at all" stays literally true;
    #   defaulting there would attach a handler the host never asked for.
    # * Only if ~/.mempalace ALREADY exists. The module must never create the
    #   state home on import — a missing ~/.mempalace is part of the documented
    #   kill-switch (hooks_cli._palace_root_exists()) for disabling
    #   autosave/mining hooks, and recreating it would silently re-arm them.
    #   When the dir is absent we stay stderr-only, matching that intent.
    log_file = env_str("MEMPALACE_LOG_FILE", "")
    file_handler: logging.Handler | None = None
    if not log_file and not logging.getLogger().handlers:
        default_log_dir = paths.state_home()
        if default_log_dir.is_dir():
            log_file = str(default_log_dir / "mcp_server.log")
    file_handler_error: Exception | None = None
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            # Pin the format: the embedded path never calls basicConfig, so
            # both paths set it here instead of relying on logging's default.
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            # File is a mempalace-only diagnostic stream; keep host / library
            # records out so it stays useful when the handler rides on a
            # host-owned root logger.
            file_handler.addFilter(_MempalaceLogFilter())
        except (OSError, ValueError) as exc:
            # Fail-soft: see "Invalid path" failure mode above. Broad on
            # (OSError, ValueError) because Windows raises ValueError for
            # NUL-byte paths while POSIX uses OSError for missing-dir / EPERM.
            file_handler_error = exc

    root = logging.getLogger()
    if root.handlers:
        # A host app (or a transitive import) already owns root logging. Do
        # NOT reset it — only add our filtered file handler, if any.
        if file_handler is not None:
            root.addHandler(file_handler)
    else:
        # Standalone server: own the unconfigured root logger as before.
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
        if file_handler is not None:
            handlers.append(file_handler)
        logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)

    if file_handler_error is not None:
        logging.getLogger(__name__).warning(
            "MEMPALACE_LOG_FILE=%r could not be opened (%s); file logging disabled",
            log_file,
            file_handler_error,
        )


_init_logging()
logger = logging.getLogger(__name__)


def _log_uncaught(exc_type, exc_value, exc_tb) -> None:
    """sys.excepthook: log the full traceback before the process dies.

    The MCP client does not surface a crashed server's stderr, so an
    unhandled exception escaping ``main()`` (or raised during the import /
    startup path) otherwise leaves no record — the silent-death failure
    mode. Routing it through ``logger`` ensures the persistent logfile
    (see ``_init_logging``) captures a full stack trace. KeyboardInterrupt
    is delegated to the default hook so Ctrl-C stays clean.
    """
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.critical(
        "Uncaught exception — server is exiting", exc_info=(exc_type, exc_value, exc_tb)
    )


sys.excepthook = _log_uncaught
