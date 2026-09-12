# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_sync(args):
    """Prune drawers whose source files are gitignored, deleted, or moved (#1252)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    if getattr(args, "background", False) and not getattr(args, "daemon", False):
        print("mempalace: --background requires --daemon", file=sys.stderr)
        sys.exit(2)

    if getattr(args, "daemon", False):
        payload = {
            "dir": args.dir,
            "root": list(args.root or []),
            "wing": args.wing,
            "dry_run": args.dry_run,
        }
        _submit_daemon_cli_job("sync", payload, args, background=getattr(args, "background", False))
        return

    from ..palace import MineAlreadyRunning
    from ..wal import _wal_log
    from ..backends import detect_backend_for_path
    from ..palace import _backend_artifact_label, resolve_backend_name
    from ..sync import sync_palace

    if not os.path.isdir(palace_path):
        print(f"\n  No palace found at {palace_path}")
        return
    try:
        backend_name = resolve_backend_name(palace_path)
    except Exception as exc:  # noqa: BLE001 - user-facing CLI guard
        print(f"\n  Could not resolve palace backend: {exc}", file=sys.stderr)
        return
    if detect_backend_for_path(palace_path) is None:
        print(
            f"\n  Palace dir at {palace_path} exists but has no "
            f"{_backend_artifact_label(backend_name)} yet."
        )
        print("  Run: mempalace mine <dir>")
        return

    project_dirs = []
    if args.dir:
        project_dirs.append(os.path.expanduser(args.dir))
    project_dirs.extend(os.path.expanduser(r) for r in args.root)
    project_dirs = project_dirs or None

    print(f"\n{'=' * 55}")
    print("  MemPalace Sync -- Gitignore-aware drawer prune")
    print(f"{'=' * 55}")
    print(f"  Palace:   {palace_path}")
    if args.wing:
        print(f"  Wing:     {args.wing}")
    if project_dirs:
        for p in project_dirs:
            print(f"  Project:  {p}")
    if args.dry_run:
        print("  Mode:     DRY RUN (no deletions)")
    else:
        print("  Mode:     APPLY (deleting drawers)")
    print(f"{'-' * 55}\n")

    try:
        report = sync_palace(
            palace_path=palace_path,
            project_dirs=project_dirs,
            wing=args.wing,
            dry_run=args.dry_run,
            wal_log=_wal_log,
        )
    except MineAlreadyRunning as exc:
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"mempalace: sync failed: {exc}", file=sys.stderr)
        sys.exit(1)

    removed_suffix = "(would remove)" if args.dry_run else "(removed)"
    print(f"  Scanned:        {report['scanned']}")
    print(f"  Kept:           {report['kept']}")
    print(f"  Gitignored:     {report['gitignored']}  {removed_suffix}")
    print(f"  Missing:        {report['missing']}  {removed_suffix}")
    print(f"  Unresolved:     {report['unresolved']}  (kept)")
    print(f"  No source:      {report['no_source']}  (kept)")
    print(f"  Out of scope:   {report['out_of_scope']}  (kept)")

    by_source = report.get("by_source") or {}
    if by_source:
        top = sorted(by_source.items(), key=lambda kv: -kv[1])[:5]
        label = "Top sources to remove" if args.dry_run else "Top sources removed"
        print(f"\n  {label}:")
        for src, n in top:
            print(f"    {src}  ({n})")

    if report["unresolved"]:
        print("\n  Unresolved drawers are kept: nothing here could show their source file is gone.")
        unresolved_sources = report.get("unresolved_by_source") or {}
        if unresolved_sources:
            top = sorted(unresolved_sources.items(), key=lambda kv: -kv[1])[:5]
            for src, n in top:
                print(f"    {src}  ({n})")
            rest = len(unresolved_sources) - len(top)
            if rest:
                print(f"    and {rest} more source file(s)")

    if args.dry_run:
        if report["gitignored"] + report["missing"] > 0:
            print("\n  Re-run with --apply to commit these deletions.")
    else:
        print(
            f"\n  Removed {report['removed_drawers']} drawers, {report['removed_closets']} closets."
        )

    print(f"\n{'=' * 55}\n")


def _submit_daemon_cli_job(kind: str, payload: dict, args, *, background: bool) -> None:
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from ..daemon import DaemonError, submit_job

    try:
        job = submit_job(
            kind,
            payload,
            palace_path=palace_path,
            backend=backend,
            wait=not background,
            auto_start=True,
            # A job refused the palace lock is deferred, not failed (#2014), so
            # it never becomes terminal while the holder lives. Waiting it out
            # would strand this terminal behind a peer that can outlive the
            # default hour; report the parked job instead. A job that is really
            # running (a long mine) is still waited out.
            stop_on_lock_deferral=not background,
        )
    except DaemonError as exc:
        print(f"mempalace: daemon submission failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if background:
        print(f"Submitted daemon job {job['id']} ({kind})")
        return

    from ..daemon import job_deferred_by_lock

    if job_deferred_by_lock(job):
        reason = (job.get("error") or {}).get("message") or "the palace write lock is held"
        # --palace is global, so it has to be echoed back ahead of the
        # subcommand: without it the suggestion silently lists the DEFAULT
        # palace's queue (or nothing at all) instead of the one this job is
        # parked in -- a wrong answer that looks authoritative.
        # `daemon jobs` and not `daemon wait`: we just declined to wait out the
        # holder, so pointing the operator at a command that blocks on the very
        # state we could not wait for would undo the point of this branch.
        palace_flag = f"--palace {shlex.quote(args.palace)} " if args.palace else ""
        print(f"mempalace: {reason}", file=sys.stderr)
        print(
            f"mempalace: job {job['id']} is queued and runs when the holder exits "
            f"(check it with: mempalace {palace_flag}daemon jobs)",
            file=sys.stderr,
        )
        sys.exit(1)

    result = job.get("result") or {}
    from ..service import print_job_result

    exit_code = print_job_result(result)
    if job.get("state") != "succeeded" and exit_code == 0:
        error = job.get("error") or {}
        print(
            f"mempalace: daemon job failed: {error.get('message', 'unknown error')}",
            file=sys.stderr,
        )
        exit_code = 1
    if exit_code:
        sys.exit(exit_code)


def cmd_daemon(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    backend = _backend_arg(args)
    from ..daemon import (
        TERMINAL_STATES,
        DaemonError,
        QueueStore,
        get_client_if_running,
        job_to_dict,
        queue_path,
        start_daemon,
        stop_daemon,
    )

    action = getattr(args, "daemon_action", None)
    try:
        if action == "start":
            if args.foreground:
                start_daemon(palace_path, backend=backend, foreground=True)
                return
            client = start_daemon(palace_path, backend=backend, foreground=False)
            health = client.health()
            print(f"MemPalace daemon running on 127.0.0.1:{client.port}")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            return

        if action == "stop":
            if stop_daemon(palace_path):
                print("MemPalace daemon stopping")
            else:
                print("MemPalace daemon is not running")
            return

        if action == "status":
            client = get_client_if_running(palace_path)
            if client is None:
                print("MemPalace daemon is not running")
                sys.exit(1)
            health = client.health()
            print("MemPalace daemon is running")
            print(f"  Palace: {health.get('palace_path')}")
            print(f"  PID:    {health.get('pid')}")
            print(f"  Active: {health.get('active_job_id') or '-'}")
            print(f"  Jobs:   {health.get('counts') or {}}")
            return

        if action == "jobs":
            client = get_client_if_running(palace_path)
            if client is not None:
                jobs = client.list_jobs(limit=args.limit)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    jobs = []
                else:
                    jobs = [
                        job_to_dict(job, include_payload=False)
                        for job in QueueStore(qpath).list(args.limit)
                    ]
            for job in jobs:
                print(f"{job['id']}  {job['state']:<9}  {job['kind']:<10}  {job['created_at']}")
            return

        if action == "wait":
            client = get_client_if_running(palace_path)
            if client is not None:
                job = client.wait(args.job_id)
            else:
                qpath = queue_path(palace_path)
                if not qpath.exists():
                    raise DaemonError("daemon is not running")
                job = job_to_dict(QueueStore(qpath).get(args.job_id))
                if job.get("state") not in TERMINAL_STATES:
                    raise DaemonError(f"daemon is not running; job {args.job_id} is {job['state']}")
            result = job.get("result") or {}
            from ..service import print_job_result

            exit_code = print_job_result(result)
            if job.get("state") != "succeeded" and exit_code == 0:
                print(f"mempalace: daemon job failed: {job.get('error')}", file=sys.stderr)
                exit_code = 1
            if exit_code:
                sys.exit(exit_code)
            return
    except DaemonError as exc:
        print(f"mempalace: daemon error: {exc}", file=sys.stderr)
        sys.exit(1)
