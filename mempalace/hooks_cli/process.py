# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


def _detached_popen_kwargs() -> dict:
    """Kwargs that give a Popen child a hidden console so the hook can exit.

    Without these, Windows holds the parent open until the child closes the
    inherited stdout/stderr handles — manifesting as "Stop hook hangs" at
    session end (#1268). On POSIX the parent can already exit (orphan
    reparents to init), but ``start_new_session`` makes the boundary
    explicit so signals to the hook don't propagate to the background mine.
    """
    kwargs: dict = {"stdin": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        flags = 0
        for name in ("CREATE_NO_WINDOW", "CREATE_NEW_PROCESS_GROUP", "CREATE_BREAKAWAY_FROM_JOB"):
            flags |= getattr(subprocess, name, 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        kwargs["start_new_session"] = True
    return kwargs


def _palace_root_exists() -> bool:
    """User-removable kill-switch.

    If ~/.mempalace/ does not exist, the user has explicitly cleared it.
    All hook side effects (logging, state dir creation, mining, ingestion)
    must respect this and short-circuit BEFORE touching disk — including
    before logging the short-circuit itself.

    Uses ``is_dir()`` rather than ``exists()`` so a stray regular file at
    ``~/.mempalace`` (or a broken symlink) is treated as absent — otherwise
    the kill-switch would be bypassed and ``STATE_DIR.mkdir()`` would later
    crash on ``NotADirectoryError``.
    """
    return PALACE_ROOT.is_dir()


def _mempalace_python() -> str:
    """Return the python interpreter that has mempalace installed.

    When hooks are invoked by Claude Code, sys.executable may be the system
    python which lacks chromadb and other deps.  Resolution order:
    1. MEMPALACE_PYTHON env var (explicit override)
    2. Venv python from package install path
    3. Editable install: venv/ sibling to mempalace/
    4. sys.executable fallback
    """
    # Honor explicit override (used by shell hook wrappers)
    env_python = os.environ.get("MEMPALACE_PYTHON", "")
    if env_python and os.path.isfile(env_python) and os.access(env_python, os.X_OK):
        return env_python
    # Historical module file was mempalace/hooks_cli.py. Package __init__.py
    # lives one directory deeper; anchor at that parent so parents[N] matches
    # the old layout (venv install or editable mempalace/ sibling).
    #
    # ``parents[3]`` / ``parents[1]`` would raise IndexError when the package
    # lives at a shallow filesystem path — Docker containers mounting at
    # ``/work``, ``/opt/app``, or other minimal-prefix installs don't have 4
    # (or sometimes even 2) parent directories. Use ``len(parents)`` to
    # check the depth before indexing; LBYL is the standard Python idiom
    # for bounded-integer lookups. Per PR #1580 review (gemini-code-assist,
    # medium priority).
    anchor = Path(__file__).resolve()
    if anchor.name == "__init__.py":
        anchor = anchor.parent
    parents = anchor.parents
    if len(parents) > 3:
        venv_bin = parents[3] / "bin" / "python"
        if venv_bin.is_file():
            return str(venv_bin)
    # Editable install: assumes project root has a venv/ sibling to mempalace/
    if len(parents) > 1:
        project_venv = parents[1] / "venv" / "bin" / "python"
        if project_venv.is_file():
            return str(project_venv)
    return sys.executable


_RECENT_MSG_COUNT = 30  # how many recent user messages to summarize

STOP_BLOCK_REASON = (
    "MemPalace auto-save checkpoint. "
    "Use mempalace_diary_write (session summary) and mempalace_add_drawer "
    "(quotes, decisions, code) to save session content. "
    "Do NOT use native auto-memory files."
)

PRECOMPACT_BLOCK_REASON = (
    "MemPalace emergency save — compaction imminent. "
    "Use mempalace_diary_write (thorough summary) and mempalace_add_drawer "
    "(ALL quotes, decisions, code, context) to save ALL content before context is lost. "
    "Do NOT use native auto-memory files."
)


def _sanitize_session_id(session_id: str) -> str:
    """Only allow alnum, dash, underscore to prevent path traversal."""
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _validate_transcript_path(transcript_path: str) -> Path:
    """Validate and resolve a transcript path, rejecting paths outside expected roots.

    Returns a resolved Path if valid, or None if the path should be rejected.
    Accepted paths must:
    - Have a .jsonl or .json extension
    - Not contain '..' after resolution (path traversal prevention)
    """
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve()
    if path.suffix not in (".jsonl", ".json"):
        return None
    # Reject if the original input contained '..' traversal components
    if ".." in Path(transcript_path).parts:
        return None
    return path


def _count_human_messages(transcript_path: str) -> int:
    """Count human messages in a JSONL transcript, skipping command-messages."""
    path = _validate_transcript_path(transcript_path)
    if path is None:
        if transcript_path:
            _log(f"WARNING: transcript_path rejected by validator: {transcript_path!r}")
        return 0
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if "<command-message>" in content:
                                continue
                        elif isinstance(content, list):
                            text = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                            if "<command-message>" in text:
                                continue
                        count += 1
                    # Also handle Codex CLI transcript format
                    # {"type": "event_msg", "payload": {"type": "user_message", "message": "..."}}
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


_state_dir_initialized = False


def _log(message: str):
    """Append to hook state log file."""
    if not _palace_root_exists():
        return  # User removed the palace; do not recreate by logging
    global _state_dir_initialized
    try:
        if not _state_dir_initialized:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                STATE_DIR.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
            _state_dir_initialized = True
        log_path = STATE_DIR / "hook.log"
        is_new = not log_path.exists()
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        if is_new:
            try:
                log_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
    except OSError:
        pass


def _output(data: dict):
    """Print JSON to stdout without importing modules that may redirect streams.

    If mempalace.mcp_server is already loaded, reuse its saved real stdout fd.
    Otherwise, write directly to fd 1 so hook responses still go to stdout even
    if sys.stdout has been redirected elsewhere.
    """
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    real_stdout_fd: int | None = None
    mcp_mod = sys.modules.get("mempalace.mcp_server")
    if mcp_mod is None:
        parent = (__package__ or "").rsplit(".", 1)[0]
        mcp_mod = sys.modules.get(f"{parent}.mcp_server" if parent else "mcp_server")
    if mcp_mod is not None:
        real_stdout_fd = getattr(mcp_mod, "_REAL_STDOUT_FD", None)

    fd = real_stdout_fd if real_stdout_fd is not None else 1
    offset = 0
    try:
        while offset < len(payload):
            try:
                offset += os.write(fd, payload[offset:])
            except InterruptedError:
                continue
        return
    except OSError:
        pass

    sys.stdout.buffer.write(payload)
    sys.stdout.buffer.flush()
