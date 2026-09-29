# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_search(args):
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if _search_args_forwardable(args) and _forward_search_to_hub(args, palace_path):
        return

    from ..searcher import search, SearchError

    try:
        search(
            query=args.query,
            palace_path=palace_path,
            wing=args.wing,
            room=args.room,
            n_results=args.results,
            since=args.since,
            before=args.before,
        )
    except SearchError:
        sys.exit(1)


def cmd_wakeup(args):
    """Show L0 (identity) + L1 (essential story) — the wake-up context."""
    from ..layers import MemoryStack

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    stack = MemoryStack(palace_path=palace_path)

    text = stack.wake_up(wing=args.wing)
    tokens = len(text) // 4
    print(f"Wake-up text (~{tokens} tokens):")
    print("=" * 50)
    print(text)


def cmd_split(args):
    """Split concatenated transcript mega-files into per-session files."""
    from ..split_mega_files import main as split_main
    import sys

    # Rebuild argv for split_mega_files argparse
    # Expand ~ and resolve to absolute path so split_mega_files sees a real path
    argv = ["--source", str(Path(args.dir).expanduser().resolve())]
    if args.output_dir:
        argv += ["--output-dir", args.output_dir]
    if args.dry_run:
        argv.append("--dry-run")
    if args.min_sessions != 2:
        argv += ["--min-sessions", str(args.min_sessions)]

    old_argv = sys.argv
    sys.argv = ["mempalace split"] + argv
    try:
        split_main()
    finally:
        sys.argv = old_argv


def cmd_migrate(args):
    """Migrate palace from a different ChromaDB version."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    if not _maintenance_requires_chroma(palace_path, "migrate"):
        raise SystemExit(2)
    from ..migrate import migrate

    migrate(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_migrate_wings(args):
    """Normalize legacy wing names (strip leading/trailing separators)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    from ..migrate import migrate_wing_names

    migrate_wing_names(
        palace_path=palace_path,
        dry_run=args.dry_run,
        confirm=getattr(args, "yes", False),
    )


def cmd_hallways(args):
    """List within-wing entity hallways (the auto-built associative graph)."""
    from ..hallways import list_hallways

    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    if getattr(args, "rebuild", False):
        from ..hallways import compute_hallways_for_wing
        from ..palace import get_collection
        from ..palace_graph import sqlite_grouped_counts_reader

        col = get_collection(palace_path, create=False, read_only=True)
        wing_filter = getattr(args, "wing", None)
        if wing_filter:
            wings = [wing_filter]
        else:
            reader = sqlite_grouped_counts_reader(config)
            rows = reader(palace_path, config.collection_name) if reader else None
            if rows is None:
                print("  --rebuild without --wing needs a sqlite-readable palace.")
                sys.exit(1)
            wings = sorted({str(r[1]) for r in rows if r[1]})
        # Scan and replace under the palace writer lock, like the other repair
        # commands: the hallway-file lock alone lets a mine add drawers and
        # save a newer snapshot between this scan and its save, which would
        # then be overwritten with stale results.
        total = 0
        with _repair_lock(palace_path):
            for w in wings:
                created = compute_hallways_for_wing(w, col=col, config=config)
                total += len(created)
                print(f"  {w:<36} {len(created):>7} hallways")
        print(f"  Rebuilt {total} hallways across {len(wings)} wing(s).")
        return
    if getattr(args, "prune_spellings", False):
        from ..hallways import prune_spelling_hallways

        apply = getattr(args, "yes", False)
        report = prune_spelling_hallways(config=config, apply=apply)
        doomed = report["self_links"] + report["duplicates"]
        print(
            f"  {doomed} of {report['total']} hallways are spelling artifacts: "
            f"{report['self_links']} self-links, {report['duplicates']} duplicate spellings."
        )
        for wing, n in list(report["by_wing"].items())[:8]:
            print(f"    {wing}: {n}")
        for label in report["sample"][:5]:
            print(f"    e.g. {label}")
        if not doomed:
            return
        if apply:
            print(f"  Removed {report['removed']}.")
        else:
            print("  Dry run. Re-run with --yes to remove them.")
        return

    rows = list_hallways(getattr(args, "wing", None), config=config)
    if not rows:
        print("No hallways yet -- they are built from drawer entities when you mine.")
        return
    rows.sort(key=lambda h: h.get("co_occurrence_count", 0), reverse=True)
    print(f"  {len(rows)} hallway(s):")
    for h in rows[: max(0, args.limit)]:
        label = h.get("label") or f"{h.get('entity_a', '?')} <-> {h.get('entity_b', '?')}"
        print(f"    {label}")


def cmd_status(args):
    from ..miner import status

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    status(palace_path=palace_path)
