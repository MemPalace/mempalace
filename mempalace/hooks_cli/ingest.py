# Loaded into mempalace.hooks_cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.hooks_cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.hooks_cli")


def _maybe_auto_ingest():
    """Background-mine MEMPAL_DIR (project files) if set.

    Transcript convos are ingested separately via ``_ingest_transcript``
    in the hook handlers — this function does not handle them, to avoid
    asymmetric interpreter handling and PID-file overwrite when both
    targets fire from a single hook call (#1231 review).

    Per-target dedup is done by ``_spawn_mine`` itself: each (dir, mode)
    target gets its own PID slot, so distinct targets never block each
    other but a re-fire of the same target while the previous one is
    still running is silently skipped.
    """
    targets = _get_mine_targets()
    if not targets:
        return

    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "project auto-ingest")
        return

    for mine_dir, mode in targets:
        try:
            if routing.use_daemon:
                try:
                    _submit_daemon_job(
                        "mine",
                        {"source": mine_dir, "mode": mode, "agent": "mempalace"},
                        dedupe_key=_daemon_mine_dedupe_key(mine_dir, mode),
                        wait=False,
                    )
                except Exception as exc:
                    # Daemon accepted context — don't fall back (would double-mine).
                    _log(f"Daemon mine submission failed: {exc}")
                continue
            _spawn_mine([_mempalace_python(), "-m", "mempalace", "mine", mine_dir, "--mode", mode])
        except OSError:
            pass
        except Exception as exc:
            # Non-daemon spawn path failed. Hooks must never crash the user's
            # shell — log and continue. Do not label this a daemon failure: the
            # daemon block above handles its own errors with its own message.
            _log(f"mine hook failed: {exc}")


def _mine_sync():
    """Synchronously mine MEMPAL_DIR (precompact path).

    Transcript convos are ingested separately via ``_ingest_transcript``
    in ``hook_precompact`` — keeping them out of this function avoids
    timeout stacking against the harness 30s ceiling (#1231 review).
    """
    targets = _get_mine_targets()
    if not targets:
        return

    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "synchronous project mine")
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_path = STATE_DIR / "hook.log"
    for mine_dir, mode in targets:
        try:
            if routing.use_daemon:
                try:
                    job = _submit_daemon_job(
                        "mine",
                        {"source": mine_dir, "mode": mode, "agent": "mempalace"},
                        dedupe_key=_daemon_mine_dedupe_key(mine_dir, mode),
                        wait=True,
                        timeout=60,
                    )
                    result = job.get("result") or {}
                    if _job_deferred_by_lock(job):
                        # Parked behind the palace lock, not failed: the daemon
                        # runs it once the holder exits. Saying "failed" here
                        # would be the false report #2014 is about.
                        _log(f"Daemon sync mine deferred: {_lock_deferral_reason(job)}")
                    elif job.get("state") != "succeeded" or not result.get("success", True):
                        _log(f"Daemon sync mine failed: {result.get('error', job.get('error'))}")
                except Exception as exc:
                    # Daemon accepted context — don't fall back (would double-mine).
                    _log(f"Daemon sync mine submission failed: {exc}")
                continue
            with open(log_path, "a") as log_f:
                subprocess.run(
                    [
                        _mempalace_python(),
                        "-m",
                        "mempalace",
                        "mine",
                        mine_dir,
                        "--mode",
                        mode,
                    ],
                    stdout=log_f,
                    stderr=log_f,
                    timeout=60,
                    # Windows: hide the conhost window this sync mine would
                    # otherwise flash on every fire. Mirrors the async paths
                    # (_spawn_mine / _desktop_toast) via _detached_popen_kwargs().
                    # 0 is a no-op off-Windows.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
        except Exception as exc:
            # Non-daemon sync spawn path failed. Hooks must never crash the
            # user's shell — log and continue (not a daemon failure; the daemon
            # block above handles its own errors).
            _log(f"mine hook failed: {exc}")


def _desktop_toast(body: str, title: str = "MemPalace"):
    """Send a desktop notification via notify-send. Fails silently."""
    try:
        subprocess.Popen(
            ["notify-send", "--app-name=MemPalace", "--icon=brain", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detached_popen_kwargs(),
        )
    except OSError:
        pass


#: Markers of harness-injected text that lands in the transcript with
#: ``role: "user"`` but was never typed by the user. A message opening with
#: any of them is skipped when composing the checkpoint's ``recent:`` line,
#: which otherwise fills with the same boilerplate in every session instead of
#: what the session was about. Literal substrings, deliberately: the wrappers
#: are fixed strings and a regex would cost more for no gain in the hook budget.
_HARNESS_BOILERPLATE_MARKERS = (
    "<command-message>",  # slash-command expansion
    "<command-name>",  # slash-command name, when it leads
    "<command-args>",  # slash-command arguments
    "<system-reminder>",  # injected reminders
    "<local-command-caveat>",  # local command output caveat block
    "<local-command-stdout>",  # local command output body
    "<task-notification>",  # background task completion notices
    "[SYSTEM NOTIFICATION",  # unbracketed notification banner
    "[Request interrupted by user",  # interruption record, carries no topic
    "[Image:",  # pasted-image placeholder, no words to summarize
    "Base directory for this skill:",  # skill preamble
)


def _is_harness_boilerplate(text: str) -> bool:
    """True when a ``role: user`` message is harness injection, not user words.

    Anchored at the opening of the message, after leading whitespace. Position
    is the whole discriminator: the harness emits a wrapper *as* the message,
    so an injection always opens one, while a wrapper appearing later is a
    human quoting the tooling. Matching anywhere in the body discarded real
    messages over text further in than the checkpoint ever keeps: someone
    writing "its events arrive as ``<task-notification>`` messages and wake the
    loop" thousands of characters into a design note lost the whole note, even
    though the leading 200 characters :func:`_extract_recent_messages` stores
    were pure prose.

    A bounded leading *window* was tried before the anchor and is not enough. A
    quote inside the first 200 characters is still a quote, and a window turns
    the rule into a tunable with a false-positive rate attached to its size.
    ``startswith`` has no such knob. Measured over 4,778 real ``role: "user"``
    text messages, the two agree on every message, so the anchor gives up no
    recall for the knob it removes.
    """
    return text.lstrip().startswith(_HARNESS_BOILERPLATE_MARKERS)


def _extract_recent_messages(transcript_path: str, count: int = _RECENT_MSG_COUNT) -> list[str]:
    """Extract the last N user messages from a JSONL transcript.

    Harness-injected messages are skipped (see
    :data:`_HARNESS_BOILERPLATE_MARKERS`) so the checkpoint summarizes the
    conversation rather than the tooling around it.
    """
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return []
    messages = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    # Claude Code format
                    msg = entry.get("message") or entry.get("event_message") or {}
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            content = " ".join(
                                b.get("text", "") for b in content if isinstance(b, dict)
                            )
                        if not isinstance(content, str) or not content.strip():
                            continue
                        if _is_harness_boilerplate(content):
                            continue
                        messages.append(content.strip()[:200])
                    # Codex CLI format
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            text = payload.get("message", "")
                            if isinstance(text, str) and text.strip():
                                if not _is_harness_boilerplate(text):
                                    messages.append(text.strip()[:200])
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return []
    return messages[-count:]


_THEME_STOPWORDS = frozenset(
    "the a an and or but in on at to for of is it i me my you your we our "
    "this that with from by was were be been are not no yes can do did dont "
    "will would should could have has had lets let just also like so if then "
    "ok okay sure yeah hey hi here there what when where how why which some "
    "all any each every about into out up down over after before between "
    "get got make made need want use used using check look see run try "
    "know think right now still already really very much more most too "
    "file files code one two new first last next thing things way well".split()
)


def _extract_themes(messages: list[str], max_themes: int = 3) -> list[str]:
    """Pull 2-3 distinctive topic words from recent messages.

    Note: stopword list is English-only; non-English corpora will produce noisy themes.
    """
    from collections import Counter

    words: Counter[str] = Counter()
    for msg in messages:
        for word in msg.lower().split():
            # Strip punctuation, keep words 4+ chars
            clean = word.strip(".,;:!?\"'`()[]{}#<>/\\-_=+@$%^&*~")
            if len(clean) >= 4 and clean not in _THEME_STOPWORDS and clean.isalpha():
                words[clean] += 1
    return [w for w, _ in words.most_common(max_themes)]


def _save_diary_direct(
    transcript_path: str,
    session_id: str,
    wing: str = "",
    toast: bool = False,
    *,
    agent_name: str,
) -> dict:
    """Write a diary checkpoint by calling the tool function directly (no MCP roundtrip).

    The entry is filed under `agent_name` so the agent that later calls
    `mempalace_diary_read(agent_name=...)` discovers it (#1693). If `wing` is
    set, the entry lands in that wing (typically the project wing derived from
    the transcript path); a `diary_read` with an empty wing spans every wing
    the agent wrote to, so project-derived wings stay discoverable.

    Returns {"count": N, "themes": [...]} on success, {"count": 0} on failure.
    A daemon lock deferral also returns {"count": 0}: nothing is filed yet, but
    the entry is queued and the daemon files it once the holder exits, so the
    checkpoint marker is deliberately not advanced.
    """
    messages = _extract_recent_messages(transcript_path)
    if not messages:
        _log("No recent messages to save")
        return {"count": 0}

    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "diary checkpoint")
        return {
            "count": 0,
            "routing_blocked": True,
            "routing_message": routing.notice,
        }

    themes = _extract_themes(messages)

    # Build a compressed diary entry from recent conversation
    now = datetime.now()
    topics = "|".join(m[:80] for m in messages[-10:])
    entry = (
        f"CHECKPOINT:{now.strftime('%Y-%m-%d')}|session:{session_id}"
        f"|msgs:{len(messages)}|recent:{topics}"
    )

    try:
        if routing.use_daemon:
            try:
                job = _submit_daemon_job(
                    "diary_write",
                    {
                        "agent_name": agent_name,
                        "entry": entry,
                        "topic": "checkpoint",
                        "wing": wing,
                    },
                    priority=10,
                    wait=True,
                    timeout=30,
                )
            except Exception as exc:
                # Daemon accepted context — don't fall back (would double-write).
                _log(f"Daemon diary checkpoint failed: {exc}")
                return {"count": 0}
            result = job.get("result") or {}
            if job.get("state") == "succeeded" and result.get("success"):
                _log(f"Diary checkpoint saved: {result.get('entry_id', '?')}")
                try:
                    ack_file = STATE_DIR / "last_checkpoint"
                    ack_file.write_text(
                        json.dumps({"msgs": len(messages), "ts": now.isoformat()}),
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                if toast:
                    _desktop_toast(f"Checkpoint saved - {len(messages)} messages archived")
                return {"count": len(messages), "themes": themes}
            if _job_deferred_by_lock(job):
                # Queued behind the palace lock: the entry is held and the daemon
                # files it once the holder exits. Not a failure, and not a reason
                # to re-file it here -- that would duplicate verbatim content.
                _log(f"Daemon diary checkpoint deferred: {_lock_deferral_reason(job)}")
                return {"count": 0}
            _log(f"Daemon diary checkpoint failed: {result.get('error', job.get('error'))}")
            return {"count": 0}

        from ..mcp_server import tool_diary_write

        result = tool_diary_write(
            agent_name=agent_name,
            entry=entry,
            topic="checkpoint",
            wing=wing,
        )
        if result.get("success"):
            _log(f"Diary checkpoint saved: {result.get('entry_id', '?')}")
            # Write state for ack tool to read
            try:
                ack_file = STATE_DIR / "last_checkpoint"
                ack_file.write_text(
                    json.dumps({"msgs": len(messages), "ts": now.isoformat()}),
                    encoding="utf-8",
                )
            except OSError:
                pass
            if toast:
                _desktop_toast(f"Checkpoint saved \u2014 {len(messages)} messages archived")
            return {"count": len(messages), "themes": themes}
        else:
            _log(f"Diary checkpoint failed: {result.get('error', 'unknown')}")
    except Exception as e:
        _log(f"Diary checkpoint error: {e}")
    return {"count": 0}


def _ingest_transcript(transcript_path: str):
    """Mine a Claude Code session transcript into the palace as a conversation."""
    path = _validate_transcript_path(transcript_path)
    if path is None:
        return
    try:
        if not path.is_file() or path.stat().st_size < 100:
            return
    except OSError:
        return

    try:
        MempalaceConfig()  # validate config loads
    except Exception:
        return

    routing = _current_hook_write_routing()
    if routing.blocked:
        _log_hook_write_blocked(routing, "transcript ingest")
        return

    try:
        if routing.use_daemon:
            try:
                _submit_daemon_job(
                    "mine",
                    {
                        "source": str(path),
                        "mode": "convos",
                        "wing": "sessions",
                        "agent": "mempalace",
                    },
                    dedupe_key=_daemon_mine_dedupe_key(str(path), "convos"),
                    wait=False,
                )
                _log(f"Transcript ingest submitted to daemon: {path.name}")
            except Exception as exc:
                # Daemon accepted context — don't fall back (would double-mine).
                _log(f"Daemon transcript ingest failed: {exc}")
            return

        # Route through ``_spawn_mine`` so the per-target PID guard kicks
        # in here too — repeated Stop/PreCompact fires for the same
        # transcript should not stack up parallel ingest mines.
        _spawn_mine(
            [
                _mempalace_python(),
                "-m",
                "mempalace",
                "mine",
                str(path),
                "--mode",
                "convos",
                "--wing",
                "sessions",
            ]
        )
        _log(f"Transcript ingest started: {path.name}")
    except OSError:
        pass
    except Exception as exc:
        # Non-daemon ingest spawn path failed. Hooks must never crash the
        # user's shell — log and continue (not a daemon failure; the daemon
        # block above handles its own errors).
        _log(f"transcript ingest hook failed: {exc}")
