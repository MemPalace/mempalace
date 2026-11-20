# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_export(args):
    """Export the palace to a portable directory tree (#452)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path

    from ..backends import detect_backend_for_path

    if not os.path.isdir(palace_path) or detect_backend_for_path(palace_path) is None:
        print(f"\n  No palace found at {palace_path}", file=sys.stderr)
        sys.exit(1)

    # The default follows the config directory rather than a fixed ~/.mempalace, so
    # an XDG install keeps its export beside its palace instead of in a second root.
    if args.output:
        output_dir = os.path.expanduser(args.output)
    else:
        output_dir = os.path.join(MempalaceConfig().config_dir, "export")
    print(f"\n{'=' * 55}")
    print(f"  Exporting palace ({args.format})")
    print(f"{'=' * 55}\n")
    from ..exporter import export_palace, export_palace_jsonl

    export = export_palace_jsonl if args.format == "jsonl" else export_palace
    try:
        export(palace_path, output_dir)
    except (ValueError, OSError) as exc:
        # The exporter refuses symlinked targets with ValueError, and an unwritable
        # output directory surfaces as OSError; report either, as `import` does.
        print(f"  ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_import(args):
    """Merge a JSONL export into the palace (#452)."""
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    input_dir = os.path.expanduser(args.dir)

    # Routed like `mine`, dry runs included: the dry_run flag travels in the
    # payload, and a dry run never opens the palace wherever it executes.
    routing = _resolve_cli_write_routing_or_exit(args, "import")

    print(f"\n{'=' * 55}")
    print("  Importing palace export" + (" (dry run)" if args.dry_run else ""))
    print(f"{'=' * 55}\n")

    if routing.use_daemon:
        _submit_daemon_cli_job(
            "import",
            # Absolute: the daemon resolves paths against its own cwd (#2467).
            {"input_dir": os.path.abspath(input_dir), "dry_run": args.dry_run},
            args,
            background=bool(getattr(args, "background", False)),
            auto_start=routing.decision.auto_start_daemon,
        )
        return

    from ..importer import import_palace
    from ..palace import MineAlreadyRunning

    try:
        import_palace(palace_path, input_dir, dry_run=args.dry_run)
    except MineAlreadyRunning as exc:
        # The writer lease is non-blocking: a mine or MCP server already writing
        # this palace refuses the import. Name the holder and exit non-zero, as
        # `mine` and `sync` do, rather than surfacing a traceback.
        print(f"mempalace: {exc}", file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"  ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
