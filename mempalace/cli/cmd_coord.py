# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


# ── Logstream (RFC 003 agent coordination) ────────────────────────────────


def _open_logstream(args):
    """Open the palace logstream database for a CLI command.

    Direct SQLite access is safe alongside a running hub: logstream.sqlite3
    is WAL-mode and independent of Chroma, so CLI writes are immediately
    visible to hub readers without the mine-style forwarding Chroma needs.
    """
    from ..logstream import LOGSTREAM_DB_FILENAME, Logstream

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    return Logstream(db_path=os.path.join(palace_path, LOGSTREAM_DB_FILENAME))


def _logstream_fail(message: str, as_json: bool):
    import json

    if as_json:
        print(json.dumps({"error": message}))
    else:
        print(f"Error: {message}", file=sys.stderr)
    sys.exit(1)


def _read_stdin_exact() -> str:
    """Read stdin as bytes and decode — never through the text layer.

    ``sys.stdin.read()`` applies universal-newline translation, turning
    CRLF into LF before we ever see it. For a store addressed by sha256
    over the exact bytes, that is silent corruption: a patch piped in from
    a Windows agent would be stored as different content with a different
    digest than the one that produced it. ``.buffer`` is absent when stdin
    has been replaced by a plain StringIO, so fall back to the text read
    rather than crashing.
    """
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    return buffer.read().decode("utf-8")


def _write_stdout_exact(content: str) -> None:
    """Write content to stdout as bytes, bypassing newline translation.

    The counterpart to :func:`_read_stdin_exact` — ``mempalace artifact get
    ID | git apply`` must deliver the stored bytes, not a re-translated
    copy of them.
    """
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        sys.stdout.write(content)
        return
    sys.stdout.flush()
    buffer.write(content.encode("utf-8"))
    buffer.flush()


def _read_text_arg(inline, file_arg, default=""):
    """Resolve inline text vs --*-file (with '-' meaning stdin).

    File and stdin reads are byte-exact (see :func:`_read_stdin_exact`):
    the logstream's contract is verbatim content, so line endings must
    reach the store exactly as the author wrote them.
    """
    if inline is not None and file_arg is not None:
        raise ValueError("pass inline text or a file, not both")
    if file_arg is not None:
        if file_arg == "-":
            return _read_stdin_exact()
        return Path(os.path.expanduser(file_arg)).read_bytes().decode("utf-8")
    if inline is not None:
        return inline
    return default


def _parse_metadata_arg(raw):
    import json

    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"--metadata is not valid JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError("--metadata must be a JSON object")
    return value


def _print_event_line(event):
    target = event["to_agent"] or "*"
    corr = f" corr={event['correlation_id']}" if event["correlation_id"] else ""
    topic = f" topic={event['topic']}" if event.get("topic") else ""
    status = f" [{event['status']}]" if event["status"] else ""
    arts = f" artifacts={len(event['artifact_ids'])}" if event["artifact_ids"] else ""
    body = event["body"].replace("\n", " ")
    if len(body) > 80:
        body = body[:77] + "..."
    body = f" :: {body}" if body else ""
    print(
        f"  {event['id']}  {event['created_at']}  {event['type']}  "
        f"{event['stream']}/{event['room']}  {event['from_agent']}->{target}"
        f"{status}{topic}{corr}{arts}{body}"
    )


def _watch_json(payload, *, follow: bool) -> str:
    """Serialize one ``logstream watch`` record.

    A single-shot watch prints exactly one document, so it is pretty-printed
    and ``json.load``-able as-is. Under ``--follow`` there are many records
    on one stream: indented documents concatenated back-to-back are *not*
    valid JSON, and ``json.load`` / ``jq`` reject them with trailing data —
    which defeats the point of a machine-readable flag on the mode meant for
    daemons. Follow mode therefore emits NDJSON, one compact record per line.
    """
    import json

    return (
        json.dumps(payload, ensure_ascii=False)
        if follow
        else json.dumps(payload, indent=2, ensure_ascii=False)
    )


def _watch_spec(args, as_json) -> dict:
    """Validate ``logstream watch`` arguments and build its filter spec.

    Kept apart from the watch loop so every bad input is rejected before any
    polling, any cursor resolution, and any checkpoint write — several of the
    bugs on this path were arguments that only failed once the loop had
    already started and persisted state.
    """
    from ..logstream import normalize_watch_values, sanitize_watch_spec

    if args.poll_timeout_ms is not None and args.poll_timeout_ms <= 0:
        # A configured zero would make watch_events' expired-deadline branch
        # yield forever without ever polling, burning a core.
        _logstream_fail("--poll-timeout-ms must be a positive number of milliseconds", as_json)
    if args.limit is not None and args.limit < 1:
        # argparse accepts it; list_events would raise mid-loop, where the
        # only handler is for KeyboardInterrupt — a traceback, and under
        # --json no error document at all.
        _logstream_fail("--limit must be a positive integer", as_json)
    if args.idle_exit_ms is not None and args.idle_exit_ms < 0:
        # Only 0 means "wait forever". A negative value arriving from config
        # or from timeout arithmetic would otherwise take that same branch
        # silently, leaving a harness waiting on a watcher it believes will
        # time out.
        _logstream_fail(
            "--idle-exit-ms must be zero (wait forever) or a positive number of milliseconds",
            as_json,
        )

    to_agents = list(args.to_agent or [])
    exclude = list(args.exclude_from_agent or [])
    if args.agent:
        # The whole point of --agent: to_agent=<me> also matches '*'
        # broadcasts, and your own broadcasts are broadcasts — so a watcher
        # without this exclusion wakes itself every time it posts a status.
        to_agents.append(args.agent)
        exclude.append(args.agent)
    spec = {
        "streams": normalize_watch_values(args.stream),
        "rooms": normalize_watch_values(args.room),
        "topics": normalize_watch_values(getattr(args, "topic", None)),
        "types": normalize_watch_values(args.type),
        "statuses": normalize_watch_values(args.status),
        "to_agents": normalize_watch_values(to_agents),
        "from_agents": normalize_watch_values(args.from_agent),
        "exclude_from_agents": normalize_watch_values(exclude),
        "correlation_ids": normalize_watch_values(args.correlation_id),
    }
    try:
        spec = sanitize_watch_spec(spec)
    except ValueError as exc:
        _logstream_fail(str(exc), as_json)

    return spec


def _logstream_watch(ls, args, as_json):
    """Run ``logstream watch`` — block until interesting events arrive.

    Split out of ``cmd_logstream`` so that dispatcher stays under the
    complexity gate: this branch carries cursor persistence, an idle timeout,
    and follow-vs-exit semantics that no other subcommand needs.

    Exit contract, chosen so a harness can background this process and treat
    its exit as a wake-up: return (0) when a match was printed, 2 when the
    idle timeout expired having seen nothing — the same convention
    ``logstream wait`` uses for a timeout.
    """
    import time

    from ..logstream import (
        WATCH_STATE_ABSENT,
        WATCH_STATE_CORRUPT,
        read_watch_state,
        write_watch_cursor,
    )

    spec = _watch_spec(args, as_json)

    stored_cursor, state_condition = read_watch_state(args.state_file)
    # A missing cursor is four different facts, and only one of them may
    # start at the tip. Treating a corrupt file or an empty-log restart as a
    # first run skips everything that arrived since, then checkpoints past
    # it — the loss is silent and permanent.
    recovery_failed = state_condition == WATCH_STATE_CORRUPT
    cursor = args.since_event_id or stored_cursor
    skipped_from = None
    if cursor is None and not args.from_start and state_condition == WATCH_STATE_ABSENT:
        # Start at the tip, like the SSE live-tail does at connect time.
        # Starting from the beginning of a long fleet log means a fresh
        # watcher wakes holding weeks of history and cannot tell it is
        # stale — measured 41 events, the oldest 49 days old, on a real
        # shared brain. Backlog is the inbox sweep's job; a watcher is for
        # what arrives from now on. Never silent: say what was skipped.
        cursor = ls.latest_event_id()
        skipped_from = cursor

    # One place decides the starting position; one place records it. That
    # position can arrive three ways — an explicit --since-event-id, the tip
    # above, or None (an empty log, or --from-start) — and every one of them
    # needs the same immediate checkpoint when no state file exists yet.
    # Deferring to the first watch_events yield leaves a window of up to a
    # full poll timeout in which an interrupt leaves no file behind, and the
    # next launch calls itself a first run and jumps to the tip, skipping
    # whatever arrived in between. Unlike later checkpoints this one cannot
    # fail quietly: losing it costs a skipped event, not a replay.
    if cursor is not None:
        # Verify the anchor before it is written anywhere. list_events raises
        # on an unknown since_event_id, and the required startup write below
        # happens first — so a typo'd or stale --since-event-id would be
        # persisted, then crash the first poll, and every later run without
        # the flag would reload it and crash again until someone deleted the
        # file by hand. Fail cleanly instead, leaving no state behind.
        try:
            ls.list_events(since_event_id=cursor, limit=1)
        except ValueError as exc:
            if args.since_event_id:
                # Explicitly supplied and wrong: user error, so refuse
                # without leaving anything behind to reload next time.
                _logstream_fail(f"{exc}. Nothing was written to the state file.", as_json)
            # A *stored* cursor whose event has gone (log rebuilt, replica
            # reset) is corrupt state rather than user error, and refusing to
            # start would strand the watcher exactly as an unreadable file
            # would. Replay instead: a duplicate, never a missed delegation.
            print(
                f"Stored cursor {cursor} no longer exists in this log; replaying from the "
                "start rather than refusing to run.",
                file=sys.stderr,
            )
            cursor = None

    if args.state_file and state_condition == WATCH_STATE_ABSENT:
        try:
            write_watch_cursor(args.state_file, cursor, agent=args.agent, required=True)
        except OSError as exc:
            _logstream_fail(
                f"could not write the initial checkpoint to {args.state_file}: {exc}. "
                "Refusing to start: without it a restart would skip every event "
                "that arrives before then.",
                as_json,
            )
    if not as_json:
        where = args.agent or ", ".join(sorted(spec["to_agents"] or [])) or "everything"
        print(f"Watching {where} from {cursor or 'now'}; Ctrl-C to stop.", file=sys.stderr)
    if skipped_from:
        print(
            f"Starting at the tip ({skipped_from}); earlier events are not replayed. "
            "Use --from-start to replay them, or sweep with `mempalace logstream list`.",
            file=sys.stderr,
        )
    if recovery_failed:
        print(
            f"State file {args.state_file} is unreadable; replaying from the start "
            "rather than skipping to the tip, so nothing since the last good "
            "checkpoint is lost.",
            file=sys.stderr,
        )

    idle_s = args.idle_exit_ms / 1000.0 if args.idle_exit_ms and args.idle_exit_ms > 0 else None
    deadline = time.monotonic() + idle_s if idle_s else None
    matched_any = False

    def _poll_timeout_ms():
        if deadline is None:
            return args.poll_timeout_ms
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        return min(args.poll_timeout_ms, remaining_ms)

    try:
        for matched, cursor in ls.watch_events(
            cursor=cursor,
            poll_timeout_ms=_poll_timeout_ms,
            limit=args.limit,
            **spec,
        ):
            if matched:
                matched_any = True
                if idle_s:
                    deadline = time.monotonic() + idle_s
                if as_json:
                    print(
                        _watch_json(
                            {
                                "events": matched,
                                "count": len(matched),
                                "cursor": cursor,
                                "timed_out": False,
                            },
                            follow=args.follow,
                        ),
                        flush=True,
                    )
                else:
                    print(f"{len(matched)} event(s):")
                    for event in matched:
                        _print_event_line(event)
                    sys.stdout.flush()
                # Matched batches checkpoint *after* stdout so a kill or
                # broken pipe between the two replays the event instead of
                # skipping it. Unmatched advances (below) are safe immediately:
                # those events were examined and rejected.
                write_watch_cursor(args.state_file, cursor, agent=args.agent)
                if not args.follow:
                    return
                continue
            write_watch_cursor(args.state_file, cursor, agent=args.agent)
            if deadline is not None and time.monotonic() >= deadline:
                if as_json:
                    print(
                        _watch_json(
                            {"events": [], "count": 0, "cursor": cursor, "timed_out": True},
                            follow=args.follow,
                        )
                    )
                else:
                    print("Idle timeout; no matching events.")
                sys.exit(0 if matched_any else 2)
    except KeyboardInterrupt:
        # Exit 0 is the documented "a match was printed" signal, so an
        # interrupted watcher must not use it — a supervisor would report
        # mail that never arrived. 128 + SIGINT, the shell convention.
        if not as_json:
            print("Stopped.", file=sys.stderr)
        sys.exit(130)
    except ValueError as exc:
        # Backstop. Every known bad input is rejected before the loop
        # starts, but a validation error escaping mid-poll would otherwise
        # surface as a traceback — and under --json as no error document at
        # all, which a machine consumer cannot distinguish from a crash.
        _logstream_fail(str(exc), as_json)


def cmd_logstream(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.logstream_action == "append":
            try:
                body = _read_text_arg(args.body, args.body_file)
                event = ls.append_event(
                    type=args.type,
                    stream=args.stream,
                    room=args.room,
                    topic=getattr(args, "topic", None),
                    from_agent=args.from_agent,
                    to_agent=args.to_agent,
                    correlation_id=args.correlation_id,
                    branch=args.branch,
                    base_commit=args.base_commit,
                    status=args.status,
                    body=body,
                    metadata=_parse_metadata_arg(args.metadata),
                    artifact_ids=args.artifact_id or None,
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Appended:")
                _print_event_line(event)
        elif args.logstream_action in ("list", "wait"):
            filters = {
                "stream": args.stream,
                "room": args.room,
                "topic": getattr(args, "topic", None),
                "type": args.type,
                "to_agent": args.to_agent,
                "from_agent": args.from_agent,
                "correlation_id": args.correlation_id,
                "status": args.status,
                "since_event_id": args.since_event_id,
                "since_created_at": args.since_created_at,
            }
            try:
                if args.logstream_action == "list":
                    events = ls.list_events(
                        limit=args.limit,
                        order=getattr(args, "order", "asc"),
                        before_event_id=getattr(args, "before_event_id", None),
                        **filters,
                    )
                    result = {"events": events, "count": len(events)}
                else:
                    result = ls.wait_events(timeout_ms=args.timeout_ms, limit=args.limit, **filters)
                    result["count"] = len(result["events"])
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                if result.get("timed_out"):
                    print("Timed out; no matching events.")
                elif not result["events"]:
                    print("No matching events.")
                else:
                    print(f"{result['count']} event(s):")
                    for event in result["events"]:
                        _print_event_line(event)
            if result.get("timed_out"):
                sys.exit(2)
        elif args.logstream_action == "watch":
            _logstream_watch(ls, args, as_json)
        elif args.logstream_action == "sync":
            from ..logsync import load_peers, sync_all, sync_with_peer

            palace_path = (
                os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
            )
            try:
                if args.peer:
                    results = [sync_with_peer(ls, args.peer, args.token or "")]
                else:
                    if not load_peers(palace_path):
                        _logstream_fail(
                            f"no peers configured ({palace_path}/peers.json) and no --peer given",
                            as_json,
                        )
                    results = sync_all(ls, palace_path)
            except Exception as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                for stats in results:
                    if stats.get("error"):
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])}: ERROR {stats['error']}"
                        )
                    else:
                        print(
                            f"  {stats.get('peer_name', stats['peer_url'])} "
                            f"({stats['peer_replica']}): +{stats['pulled_events']} events, "
                            f"+{stats['pulled_artifacts']} artifacts"
                        )
            if any(s.get("error") for s in results):
                sys.exit(1)
        elif args.logstream_action == "ack":
            try:
                event = ls.ack_event(
                    args.event_id,
                    from_agent=args.from_agent,
                    status=args.status,
                    body=args.body or "",
                    topic=getattr(args, "topic", None),
                )
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(event, indent=2, ensure_ascii=False))
            else:
                print("Acknowledged:")
                _print_event_line(event)
    finally:
        ls.close()


def _codex_task_runner(workspace: Path, prompt: str) -> tuple[list[str], dict]:
    return ["codex", "exec", "--cd", str(workspace), prompt], {}


def _claude_task_runner(workspace: Path, prompt: str) -> tuple[list[str], dict]:
    return ["claude", "--print", prompt], {"cwd": str(workspace)}


_TASK_RUNNER_ADAPTERS = {
    "codex": ("-codex", _codex_task_runner),
    "claude": ("-claude", _claude_task_runner),
}


def _validate_task_workspace(workspace: Path, task: dict) -> None:
    """Prove a controlled launch starts from the task's exact clean Git state."""
    import subprocess

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(workspace), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            detail = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise ValueError(f"workspace Git validation failed: {detail}")
        return completed.stdout.strip()

    branch = git("branch", "--show-current")
    if branch != task["branch"]:
        raise ValueError(
            f"workspace branch is {branch or '(detached)'!r}; task requires {task['branch']!r}"
        )
    try:
        expected_commit = git("rev-parse", "--verify", f"{task['base_commit']}^{{commit}}")
    except ValueError as exc:
        raise ValueError(
            f"task base commit {task['base_commit']!r} does not resolve in the workspace"
        ) from exc
    head_commit = git("rev-parse", "HEAD")
    if head_commit != expected_commit:
        raise ValueError(f"workspace base commit is {head_commit}; task requires {expected_commit}")
    if git("status", "--porcelain"):
        raise ValueError(
            "workspace has uncommitted changes; controlled launch requires a clean checkout"
        )


def cmd_task(args):
    """Create and run complete logstream tasks through a small public interface."""
    import json

    from ..tasks import create_task, task_handoff, validate_task_request

    as_json = getattr(args, "json", False)
    task_file = getattr(args, "task_file", None)
    ls = None
    if args.task_action == "create" or not task_file:
        try:
            ls = _open_logstream(args)
        except Exception as exc:
            _logstream_fail(str(exc), as_json)
    try:
        if args.task_action == "create":
            try:
                goal = _read_text_arg(args.goal, args.goal_file)
                done = _read_text_arg(args.done, args.done_file)
                result = create_task(
                    ls,
                    project=args.project,
                    from_agent=args.from_agent,
                    to_agent=args.to_agent,
                    goal=goal,
                    branch=args.branch,
                    base_commit=args.base_commit,
                    done=done,
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            event = result["task"]
            handoff = result["handoff"]
            correlation_id = event["correlation_id"]
            if as_json:
                print(json.dumps({"task": event, "handoff": handoff}, indent=2, ensure_ascii=False))
            else:
                print(f"Task created: {correlation_id}")
                print(f"Event: {event['id']}")
                print(f"To: {args.to_agent}")
                print("\nReady to paste:")
                print(handoff)
        elif args.task_action == "launch":
            import subprocess

            try:
                if task_file:
                    task = json.loads(Path(task_file).read_text(encoding="utf-8"))
                    validate_task_request(task, source="--task-file")
                    correlation_id = task["correlation_id"]
                else:
                    correlation_id = args.correlation_id
                    events = ls.list_events(
                        type="task.request", correlation_id=correlation_id, limit=2
                    )
                    if not events:
                        raise ValueError(f"task {correlation_id!r} not found")
                    if len(events) > 1:
                        raise ValueError(
                            f"task {correlation_id!r} has multiple requests; refusing to guess"
                        )
                    task = validate_task_request(events[0], source=f"task {correlation_id!r}")
                addressed_agent = task["to_agent"]
                if args.agent and addressed_agent not in (None, "*", args.agent):
                    raise ValueError(
                        f"task is addressed to {addressed_agent!r}, not {args.agent!r}"
                    )
                agent = args.agent or addressed_agent
                if not agent or agent == "*":
                    raise ValueError(
                        "broadcast tasks require --agent for a concrete worker identity"
                    )
                expected_suffix, runner_adapter = _TASK_RUNNER_ADAPTERS[args.runner]
                actual_suffix = next(
                    (
                        suffix
                        for suffix, _adapter in _TASK_RUNNER_ADAPTERS.values()
                        if agent.endswith(suffix)
                    ),
                    None,
                )
                if actual_suffix is not None and actual_suffix != expected_suffix:
                    raise ValueError(
                        f"runner {args.runner} does not match addressed identity {agent!r}"
                    )
                workspace = Path(os.path.expanduser(args.workspace)).resolve()
                if not workspace.is_dir():
                    raise ValueError(f"workspace is not a directory: {workspace}")
                _validate_task_workspace(workspace, task)
            except (ValueError, OSError, json.JSONDecodeError) as exc:
                _logstream_fail(str(exc), as_json)

            prompt = task_handoff(correlation_id, agent)
            command, runner_kwargs = runner_adapter(workspace, prompt)
            print(f"Launching {correlation_id} with {args.runner} as {agent}")
            # Release the SQLite handle before the child connects back to the
            # same logstream. The child owns its own process lifetime and MCP
            # connection; no shell is involved in constructing this command.
            if ls is not None:
                ls.close()
                ls = None
            try:
                completed = subprocess.run(
                    command,
                    check=False,
                    **runner_kwargs,
                )
            except OSError as exc:
                _logstream_fail(f"could not start {args.runner}: {exc}", as_json)
            if completed.returncode:
                sys.exit(completed.returncode)
    finally:
        if ls is not None:
            ls.close()


def cmd_artifact(args):
    import json

    as_json = getattr(args, "json", False)
    try:
        ls = _open_logstream(args)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    try:
        if args.artifact_action == "put":
            try:
                content = _read_text_arg(args.content, args.file, default=None)
                if content is None:
                    content = _read_stdin_exact()
                artifact = ls.put_artifact(
                    kind=args.kind,
                    content=content,
                    created_by=args.created_by,
                    metadata=_parse_metadata_arg(args.metadata),
                )
            except (ValueError, OSError) as exc:
                _logstream_fail(str(exc), as_json)
            if as_json:
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            else:
                print(f"Stored {artifact['id']}  kind={artifact['kind']}")
                print(f"  sha256={artifact['sha256']}")
                print(f"  size={artifact['size_bytes']} bytes")
            # Warnings go to stderr in both modes so `--json | jq` stays
            # clean while interactive callers still can't miss them.
            for warning in artifact.get("warnings", []):
                print(f"Warning: {warning}", file=sys.stderr)
        elif args.artifact_action == "get":
            try:
                artifact = ls.get_artifact(args.artifact_id)
            except ValueError as exc:
                _logstream_fail(str(exc), as_json)
            if artifact is None:
                _logstream_fail(f"artifact {args.artifact_id!r} not found", as_json)
            if args.out:
                # write_bytes, not write_text: on Windows the text layer
                # expands LF back to CRLF, so the file on disk would not
                # match the sha256 the user is told to verify.
                Path(os.path.expanduser(args.out)).write_bytes(artifact["content"].encode("utf-8"))
            if as_json:
                if args.out:
                    artifact = {**artifact, "content_written_to": args.out}
                    artifact.pop("content")
                print(json.dumps(artifact, indent=2, ensure_ascii=False))
            elif args.out:
                print(f"Wrote {artifact['size_bytes']} bytes to {args.out}")
                print(f"  sha256={artifact['sha256']}")
            else:
                # Exact content on stdout so `mempalace artifact get ID | git apply`
                # works; metadata would corrupt the stream. Written through
                # .buffer because the text layer would re-translate newlines
                # on Windows — the pipe must carry the stored bytes.
                _write_stdout_exact(artifact["content"])
    finally:
        ls.close()
