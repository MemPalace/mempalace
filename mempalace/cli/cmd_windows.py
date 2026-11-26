# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace windows build|status`: sub-window vectors of long drawers.


def cmd_windows(args):
    from ..backends import PalaceNotFoundError
    from ..embedding import EmbeddingFunctionUnavailableError, UnknownEmbeddingModelError
    from ..palace import MineAlreadyRunning, mine_palace_lock
    from ..subwindows import SubwindowsUnsupported, build_windows

    action = getattr(args, "windows_action", None)
    if action not in ("build", "status"):
        print("usage: mempalace windows {build [--wing WING] [--dry-run] | status}")
        sys.exit(2)
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    dry_run = action == "status" or getattr(args, "dry_run", False)
    if not MempalaceConfig().embedding_subwindows and not dry_run:
        print(
            "  Note: embedding_subwindows is off, so search ignores these windows until\n"
            "  you set it (config.json or MEMPALACE_EMBEDDING_SUBWINDOWS=1)."
        )

    def report(stats, final=False):
        line = (
            f"  drawers {stats['drawers']:,}  long {stats['long_drawers']:,}  "
            f"windows needed {stats['windows_needed']:,}"
        )
        if not dry_run:
            line += f"  embedded {stats['embedded']:,}  already fresh {stats['skipped']:,}"
        print(line, flush=True)

    try:
        # The lock is taken per batch, so mines and sync keep writing during a
        # long build; it waits for a writer that holds it between batches.
        stats = build_windows(
            palace_path,
            wing=getattr(args, "wing", None),
            dry_run=dry_run,
            progress=report,
            lock=None if dry_run else (lambda: mine_palace_lock(palace_path)),
        )
    except MineAlreadyRunning:
        print(
            "  Another writer (a mine or the hub) kept this palace's lock too long;\n"
            "  run `mempalace windows build` again: it resumes where it stopped."
        )
        sys.exit(1)
    except PalaceNotFoundError:
        # Also covers CollectionNotInitializedError: a palace with no drawers yet.
        print(f"  No palace with drawers at {palace_path}. Run `mempalace mine` first.")
        sys.exit(1)
    except (SubwindowsUnsupported, UnknownEmbeddingModelError) as exc:
        print(f"  {exc}")
        sys.exit(1)
    except EmbeddingFunctionUnavailableError as exc:
        print(f"  The embedding model could not be loaded: {exc}")
        sys.exit(1)
    print()
    print(f"  Profile:          {stats['profile']}")
    print(f"  Drawers:          {stats['drawers']:,}")
    share = stats["long_drawers"] / stats["drawers"] if stats["drawers"] else 0.0
    print(f"  Over the window:  {stats['long_drawers']:,} ({share:.1%})")
    print(f"  Windows needed:   {stats['windows_needed']:,}")
    if dry_run:
        print(f"  Window rows now:  {stats['window_rows']:,}")
        print(f"  Orphans to prune: {stats['orphans_pruned']:,}")
        return
    print(f"  Embedded:         {stats['embedded']:,}")
    print(f"  Already fresh:    {stats['skipped']:,}")
    print(f"  Removed (stale):  {stats['removed'] + stats['orphans_pruned']:,}")
    print(f"  Window rows:      {stats['window_rows']:,}")
