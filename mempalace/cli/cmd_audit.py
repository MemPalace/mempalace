# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace audit`: organization quality report (see mempalace/palace_audit.py).


def cmd_audit(args):
    """Score how navigable the palace is and list what to fix.

    Read-only and lock-free: safe to run while an MCP server or a mine holds
    the palace. ``--json`` emits the full report for tracking over time.
    """
    from ..palace_audit import audit_palace, audit_to_json, render_audit, terminal_progress

    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    as_json = getattr(args, "json", False)
    quiet = getattr(args, "quiet", False)
    progress = None
    if not as_json and not quiet and sys.stderr.isatty():
        progress = terminal_progress(sys.stderr)
    try:
        report = audit_palace(
            palace_path=palace_path,
            progress=progress,
            explicit_palace=bool(getattr(args, "palace", None)) or None,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"\n  {exc}")
        print("  Run `mempalace mine <dir>` first, or pass --palace <path>.")
        sys.exit(1)

    if as_json:
        print(audit_to_json(report))
    else:
        print(render_audit(report))

    threshold = getattr(args, "fail_under", None)
    overall = report["scores"]["overall"]
    if threshold is not None and overall is not None and overall < threshold:
        sys.exit(2)


@contextlib.contextmanager
def _repair_lock(palace_path):
    """The palace writer lock for a repair command, refused cleanly.

    ``rooms apply``, ``wings split``, ``kg normalize`` and ``hallways
    --rebuild`` write while holding ``mine_palace_lock``. When a mine or a
    running MCP hub already holds it, say who holds it on one line and exit
    1, the way ``mempalace mine`` does, instead of raising a traceback.
    """
    from ..palace import MineAlreadyRunning, mine_palace_lock

    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(mine_palace_lock(palace_path))
        except MineAlreadyRunning as exc:
            print(f"mempalace: {exc}", file=sys.stderr)
            sys.exit(1)
        yield
