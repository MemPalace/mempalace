# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


def hook_stop(data: dict, harness: str):
    """Stop hook: block every N messages for auto-save."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    # Respect auto_save config toggle (clean opt-out)
    if not MempalaceConfig().hooks_auto_save:
        _output({})
        return

    # If already in a block-mode save cycle, let through (infinite-loop prevention).
    # Silent mode saves directly without returning {"decision":"block"}, so there's
    # no loop to prevent — and Claude Code's plugin dispatch sets this flag on every
    # fire after the first, which would otherwise suppress all subsequent auto-saves.
    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        # Safe default: assume silent mode on any config-read failure so saves
        # proceed rather than being silently dropped. Silent mode is the default
        # (v3.3.0+), so if we can't read config, behave as if it's still on.
        silent_guard = True
        try:
            silent_guard = MempalaceConfig().hook_silent_save
        except AttributeError as exc:
            _log(f"WARNING: could not read hook_silent_save: {exc}; defaulting to silent mode")
        if not silent_guard:
            _output({})
            return

    # Count human messages
    exchange_count = _count_human_messages(transcript_path)

    # Track last save point
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    since_last = exchange_count - last_save

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save")

    if since_last >= SAVE_INTERVAL and exchange_count > 0:
        with _hook_write_routing_context() as routing:
            if routing.blocked:
                _log_hook_write_blocked(routing, "stop-hook checkpoint")
                _output(_blocked_hook_output(routing))
                return

            _log(f"TRIGGERING SAVE at exchange {exchange_count}")

            # Read hook settings from config
            try:
                config = MempalaceConfig()
                silent = config.hook_silent_save
                toast = config.hook_desktop_toast
            except Exception:
                silent = True
                toast = False

            project_wing = _wing_from_transcript_path(transcript_path)

            if silent:
                # Save directly via Python API — systemMessage renders in terminal
                result = {"count": 0}
                if transcript_path:
                    result = _save_diary_direct(
                        transcript_path,
                        session_id,
                        wing=project_wing,
                        toast=toast,
                        agent_name=_diary_agent_for_harness(harness),
                    )
                    _ingest_transcript(transcript_path)
                _maybe_auto_ingest()
                # Only advance save marker after successful save
                count = result.get("count", 0)
                if count > 0:
                    try:
                        last_save_file.write_text(str(exchange_count), encoding="utf-8")
                    except OSError:
                        pass
                    themes = result.get("themes", [])
                    if themes:
                        tag = " \u2014 " + ", ".join(themes)
                    else:
                        tag = ""
                    _output(
                        {
                            "systemMessage": f"\u2726 {count} memories woven into the palace{tag}",
                        }
                    )
                else:
                    _output({})
            else:
                # Legacy: block and ask Claude to save via MCP tools.
                # Marker advances before confirmed save — best-effort; if Claude
                # fails to save, the checkpoint is lost but won't retry endlessly.
                try:
                    last_save_file.write_text(str(exchange_count), encoding="utf-8")
                except OSError:
                    pass
                if transcript_path:
                    _ingest_transcript(transcript_path)
                _maybe_auto_ingest()
                reason = STOP_BLOCK_REASON + f" Write diary entry to wing={project_wing}."
                _output({"decision": "block", "reason": reason})
    else:
        _output({})


def hook_session_start(data: dict, harness: str):
    """Session start hook: initialize session tracking state."""
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]

    _log(f"SESSION START for session {session_id}")

    # Initialize session state directory
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Surface a required-daemon problem at session start instead of waiting
    # until the first save is due. Hooks still never cold-start the daemon.
    with _hook_write_routing_context() as routing:
        if routing.blocked:
            _log_hook_write_blocked(routing, "session-start readiness check")
            _output(_blocked_hook_output(routing))
            return

    # Pass through — no blocking on session start
    _output({})


def _clear_session_last_save(session_id: str) -> None:
    """Drop the per-session save marker once a session has ended.

    ``hook_stop`` writes ``{session_id}_last_save`` but never had a clean-exit
    cleanup path, so the marker lingered. The session is over by the time
    ``hook_session_end`` runs, so removing it here keeps ``hook_state/`` from
    accumulating dead markers. OS errors (including a missing marker, since
    ``FileNotFoundError`` is an ``OSError``) are swallowed — this is best-effort
    cleanup, never a reason to fail the hook.
    """
    try:
        (STATE_DIR / f"{session_id}_last_save").unlink()
    except OSError:
        pass


def hook_session_end(data: dict, harness: str):
    """Session end hook: one final flush when a session exits cleanly.

    Closes the gap (#1341) where a session that never crosses ``SAVE_INTERVAL``
    on ``Stop`` and never triggers ``PreCompact`` exits with nothing saved —
    the common case for short, useful sessions.

    Why background instead of mine inline: Claude Code's hooks reference
    documents a default SessionEnd timeout of 1.5 seconds, and "timeouts set on
    plugin-provided hooks do not raise the budget"
    (https://code.claude.com/docs/en/hooks). A cold ``mempalace`` start alone
    exceeds 1.5s, so this handler must never mine in the hook foreground. The
    shell wrapper backgrounds it and returns immediately; the heavy capture is
    spawned *detached* via ``_ingest_transcript`` / ``_maybe_auto_ingest`` (both
    route through ``_spawn_mine`` / ``_detached_popen_kwargs``). On POSIX that
    detached child reliably outlives the session (verified). On Windows only the
    mine grandchild (spawned with detached-process flags) is designed to break
    away from the session; the backgrounded hook process and the in-process
    diary write are best-effort there (no Windows CI coverage yet). This
    honors the "background everything / hooks under 500ms" budget. SessionEnd
    has no decision control, so this only ever saves; it never emits a block
    payload.
    """
    if not _palace_root_exists():
        _output({})
        return

    # Parse inside the try so a malformed payload (e.g. non-dict stdin that
    # makes _parse_harness_input raise) still runs the finally cleanup below.
    session_id = "unknown"
    try:
        parsed = _parse_harness_input(data, harness)
        session_id = parsed["session_id"]
        transcript_path = parsed["transcript_path"]

        # Read config defensively (mirror hook_stop): a corrupt or unreadable
        # config must not lose the final save, so default to auto-save on and
        # toasts off rather than crashing the hook.
        try:
            config = MempalaceConfig()
            auto_save = config.hooks_auto_save
            toast = config.hook_desktop_toast
        except Exception:
            auto_save = True
            toast = False

        # Respect auto_save config toggle (clean opt-out)
        if not auto_save:
            _output({})
            return

        _log(f"SESSION END for session {session_id}")

        # Validate the harness-provided transcript path before touching it
        # (extension + ".." traversal check), mirroring the read path that
        # already runs through _validate_transcript_path. A rejected path skips
        # the transcript captures but still lets the independent MEMPAL_DIR mine
        # run.
        valid_transcript = ""
        if transcript_path:
            try:
                validated = _validate_transcript_path(transcript_path)
            except OSError:
                validated = None
            if validated is None:
                _log(f"WARNING: transcript_path rejected by validator: {transcript_path!r}")
            else:
                valid_transcript = str(validated)

        # Flush. The diary checkpoint (in-process ChromaDB write) runs FIRST,
        # before any detached mine is spawned, so it never contends for the
        # palace lock; this handler is already backgrounded by the wrapper, so it
        # is not under the SessionEnd budget and has time to finish. The detached
        # transcript ingest follows; re-mining a transcript ``Stop`` already
        # captured is a near no-op (deterministic convo IDs + ``file_already_mined``
        # short-circuit + upsert). ``reason`` is intentionally not branched on:
        # every clean-exit reason (incl. ``/clear`` / ``resume``) warrants the
        # flush. Order matches ``hook_stop``.
        with _hook_write_routing_context() as routing:
            if routing.blocked:
                _log_hook_write_blocked(routing, "session-end flush")
                _output(_blocked_hook_output(routing))
                return

            if valid_transcript:
                _save_diary_direct(
                    valid_transcript,
                    session_id,
                    wing=_wing_from_transcript_path(valid_transcript),
                    toast=toast,
                    agent_name=_diary_agent_for_harness(harness),
                )
                _ingest_transcript(valid_transcript)
            _maybe_auto_ingest()

        _output({})
    finally:
        _clear_session_last_save(session_id)


def hook_precompact(data: dict, harness: str):
    """Precompact hook: mine transcript synchronously, then allow compaction.

    Respects the ``hooks.auto_save`` config toggle — when disabled, returns
    immediately without mining.
    """
    if not _palace_root_exists():
        _output({})
        return
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]

    # Respect auto_save config toggle (clean opt-out)
    if not MempalaceConfig().hooks_auto_save:
        _output({})
        return

    _log(f"PRE-COMPACT triggered for session {session_id}")

    with _hook_write_routing_context() as routing:
        if routing.blocked:
            _log_hook_write_blocked(routing, "precompact flush")
            _output(_blocked_hook_output(routing))
            return

        # Capture tool output via our normalize path before compaction loses it
        if transcript_path:
            _ingest_transcript(transcript_path)

        # Mine MEMPAL_DIR synchronously so project data lands before
        # compaction proceeds. Transcript convos were already kicked off
        # above via _ingest_transcript.
        _mine_sync()

    _output({})


def run_hook(hook_name: str, harness: str):
    """Main entry point: read stdin JSON, dispatch to hook handler."""
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "session-end": hook_session_end,
        "precompact": hook_precompact,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness)
