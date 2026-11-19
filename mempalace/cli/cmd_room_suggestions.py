# Loaded into mempalace.cli. Optional organization never runs from hooks or init.


def add_room_suggestions_parser(rooms_sub):
    parent = rooms_sub.add_parser(
        "suggestions",
        help="Optional background room suggestions; preview, explicitly apply, or undo",
    )
    commands = parent.add_subparsers(dest="suggestions_action", required=True)
    start = commands.add_parser(
        "start", help="Start a background classification job; no drawers move"
    )
    start.add_argument("--wing", required=True)
    start.add_argument(
        "--model", required=True, help="Installed local Ollama model with decision capability"
    )
    start.add_argument(
        "--endpoint", default="http://127.0.0.1:11434", help="Loopback Ollama endpoint"
    )
    start.add_argument(
        "--threshold",
        type=float,
        default=0.8,
        help="Minimum model score for a suggestion (default 0.8; not a calibrated accuracy)",
    )
    start.add_argument(
        "--limit", type=int, default=100, help="Maximum drawers per job, 1–10000 (default 100)"
    )
    start.add_argument(
        "--timeout", type=int, default=120, help="Per-request timeout in seconds, 1–600"
    )
    start.add_argument(
        "--from",
        dest="from_rooms",
        help="Comma-separated original rooms; 'all' includes manually filed rooms",
    )
    start.add_argument(
        "--foreground",
        action="store_true",
        help="Wait for classification instead of detaching a worker",
    )
    commands.add_parser("list", help="List recent jobs for this palace")
    for action, help_text in [
        ("status", "Show durable job progress and outcome counts"),
        ("preview", "Review suggestions and original verbatim excerpts"),
        ("cancel", "Stop inference after the current request; keep completed predictions"),
        ("resume", "Resume interrupted or cancelled classification"),
        ("apply", "Apply reviewed suggestions only with --yes"),
        ("undo", "Restore original rooms only with --yes; refuse later edits"),
    ]:
        command = commands.add_parser(action, help=help_text)
        command.add_argument("job", help="Job ID returned by start")
        if action == "preview":
            command.add_argument("--limit", type=int, default=20)
            command.add_argument("--offset", type=int, default=0, help="Skip reviewed rows")
        if action == "resume":
            command.add_argument("--foreground", action="store_true")
        if action in {"apply", "undo"}:
            command.add_argument(
                "--yes", action="store_true", help="Explicitly write the reviewed metadata changes"
            )
        if action == "apply":
            command.add_argument(
                "--ids", nargs="+", help="Apply only these reviewed suggested drawer IDs"
            )
    for command in commands.choices.values():
        command.add_argument("--json", action="store_true", help="Print machine-readable results")


def _print_suggestion_result(result, args):
    import json

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if isinstance(result, list):
        for job in result:
            print(f"  {job['id']}  {job['state']}  {job['created_at']}")
        return
    if result.get("dry_run"):
        print(
            f"  Preview only. Review the job, then use {result['action']} {args.job} --yes to write changes."
        )
    if "completed" in result:
        print(
            f"  {result['id']}  {result['state']}  {result['completed']}/{result['total']} classified"
        )
        print(
            f"  Model: {result['model']}  Outcomes: {result['counts']}  Cached: {result['cache_hits']}"
        )
        remaining = result.get("estimated_remaining_seconds")
        if remaining:
            print(f"  Estimated inference remaining: {remaining:.0f} seconds")
        for item in result.get("items", []):
            score = f"{item['probability']:.3f}" if item["probability"] is not None else "-"
            print(
                f"  {item['id']}  {item['current_room']} -> {item['suggested_room'] or '(keep)'}  score={score}  {item['outcome']}"
                + ("  STALE" if item["stale"] else "")
            )
            print(f"    {item['excerpt']!r}")
        if result.get("next_offset") is not None:
            print(f"  More results: preview {args.job} --offset {result['next_offset']}")
    else:
        print(f"  {result['state']}: {result.get('changed', 0)} metadata records changed")
    if result.get("error"):
        print(f"  {result['error']}")


def cmd_room_suggestions(args):
    from .. import room_suggestions as suggestions
    from ..palace import MineAlreadyRunning
    from ..room_suggestion_store import RoomSuggestionError, SuggestionStore

    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    action = args.suggestions_action
    try:
        store = SuggestionStore(palace_path)
        if action == "start":
            scope = args.from_rooms
            scope = (
                "all"
                if scope == "all"
                else [name.strip() for name in scope.split(",") if name.strip()]
                if scope is not None
                else None
            )
            job_id = suggestions.create_job(
                store,
                wing=args.wing,
                model=args.model,
                endpoint=args.endpoint,
                threshold=args.threshold,
                limit=args.limit,
                timeout=args.timeout,
                from_rooms=scope,
                backend=getattr(args, "backend", None),
            )
            if args.foreground:
                suggestions.run_worker(store, job_id)
            else:
                suggestions.launch(store, job_id)
            result = suggestions.status(store, job_id)
            if not args.json:
                print("  No drawers moved. Review with:")
                print(f"    mempalace rooms suggestions preview {job_id}")
        elif action == "list":
            result = store.jobs()
        elif action == "preview":
            result = suggestions.preview(store, args.job, limit=args.limit, offset=args.offset)
        elif action == "status":
            result = suggestions.status(store, args.job)
        elif action == "cancel":
            suggestions.cancel(store, args.job)
            result = suggestions.status(store, args.job)
        elif action == "resume":
            suggestions.resume(store, args.job, foreground=args.foreground)
            result = suggestions.status(store, args.job)
        else:
            result = suggestions.apply(
                store, args.job, ids=getattr(args, "ids", None), yes=args.yes, undo=action == "undo"
            )
        _print_suggestion_result(result, args)
        if isinstance(result, dict) and result.get("state") == "failed":
            raise SystemExit(1)
    except (RoomSuggestionError, MineAlreadyRunning, FileNotFoundError) as exc:
        print(f"mempalace: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except Exception as exc:
        print(
            f"mempalace: room suggestions stopped ({type(exc).__name__}); inspect the job and retry the interrupted operation",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
