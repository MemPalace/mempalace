# Loaded into mempalace.mcp_server via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.mcp_server":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.mcp_server")


class _MempalaceLogFilter(logging.Filter):
    """Pass only records emitted by mempalace's own loggers.

    Lets the ``MEMPALACE_LOG_FILE`` handler attach to an already-configured
    root logger (a host app embedding the server, #1860) without copying the
    host's — or a third-party library's — records into mempalace's diagnostic
    file. mempalace loggers are ``mempalace`` / ``mempalace.*`` (the dotted
    ``__name__`` family) plus the flat ``mempalace_mcp`` /
    ``mempalace_format_miner`` / ``mempalace_hallways`` / ``mempalace_graph``
    loggers — every one is prefixed ``mempalace``.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name
        return name == "mempalace" or name.startswith(("mempalace.", "mempalace_"))


# Preserved across importlib.reload via globals(): a reload re-executes this
# module body, so a plain ``= False`` would reset the guard and let
# _init_logging() stack a duplicate file handler. globals().get keeps the prior
# True so the guard survives reload (#1885 review).
_logging_configured = globals().get("_logging_configured", False)


def _init_logging() -> None:
    """Configure mempalace logging: stderr by default, optional file append.

    ``MEMPALACE_LOG_FILE``, when set, attaches a ``FileHandler`` so MCP-client
    failures the client never surfaces (e.g. the ``-32000`` cold-load timeout
    in #1495) stay diagnosable from the file.

    Root-logger ownership (#1860). The server must not hijack a host
    application's logging, so the two cases are handled differently:

    * **Root unconfigured** (standalone ``mempalace-mcp``): own it — a stderr
      handler (plus the optional file handler) via ``basicConfig`` at INFO.
      The historical behaviour.
    * **Root already configured** (an app imported ``mempalace.mcp_server``
      after setting up its own logging): leave the host's level, format, and
      handlers untouched. Attach only the file handler, filtered to
      mempalace's own records (`_MempalaceLogFilter`), so the host's logs do
      not bleed into mempalace's file. With ``MEMPALACE_LOG_FILE`` unset the
      root logger is not touched at all.

    Previously this called ``logging.basicConfig(..., force=True)``, which
    reset root's handlers/level/format unconditionally and silently clobbered
    any host app that had configured logging first (#1860). ``force`` existed
    (#1495) only to stop ``basicConfig`` no-op'ing when handlers already
    existed; the filtered additive handler preserves that diagnostic contract
    without the collateral reset.

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
    log_file = os.environ.get("MEMPALACE_LOG_FILE", "").strip()
    file_handler: logging.Handler | None = None
    file_handler_error: Exception | None = None
    if log_file:
        try:
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            # Pin the format: the embedded path never calls basicConfig, so set
            # it here instead of relying on logging's default formatter. The
            # default already renders "%(message)s", but the explicit set makes
            # both paths identical and independent of that default (#1885 review).
            file_handler.setFormatter(logging.Formatter("%(message)s"))
            # File is a mempalace-only diagnostic stream; keep host / library
            # records out so it stays useful when the handler rides on a
            # host-owned root logger (#1860).
            file_handler.addFilter(_MempalaceLogFilter())
        except (OSError, ValueError) as exc:
            # Fail-soft: see "Invalid path" failure mode above. Broad on
            # (OSError, ValueError) because Windows raises ValueError for
            # NUL-byte paths while POSIX uses OSError for missing-dir / EPERM.
            file_handler_error = exc

    root = logging.getLogger()
    if root.handlers:
        # A host app (or a transitive import) already owns root logging. Do
        # NOT reset it (#1860) — only add our filtered file handler, if any.
        if file_handler is not None:
            root.addHandler(file_handler)
    else:
        # Standalone server: own the unconfigured root logger as before.
        handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
        if file_handler is not None:
            handlers.append(file_handler)
        logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=handlers)

    if file_handler_error is not None:
        logging.getLogger("mempalace_mcp").warning(
            "MEMPALACE_LOG_FILE=%r could not be opened (%s); file logging disabled",
            log_file,
            file_handler_error,
        )


_init_logging()
logger = logging.getLogger("mempalace_mcp")
