# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace wings split`: one wing per source project (mempalace/wing_split.py).


def cmd_wings(args):
    from ..wing_split import (
        apply_split,
        load_split_plan,
        plan_split,
        plan_targets,
        save_split_plan,
        split_pending_path,
        split_plan_path,
    )
    from datetime import datetime, timezone

    action = getattr(args, "wings_action", None)
    if action != "split":
        print("usage: mempalace wings split --wing WING [--yes]")
        sys.exit(2)
    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    wing = args.wing
    from ..palace import get_collection
    from ..palace_graph import sqlite_grouped_counts_reader

    def existing_wings():
        reader = sqlite_grouped_counts_reader(config)
        rows = reader(palace_path, config.collection_name) if reader else None
        if rows is None:
            return set()
        return {str(r[1]) for r in rows if r[1]}

    def report(plan):
        projects = plan["projects"]
        print(
            f"  {wing}: {len(projects)} source projects, "
            f"{sum(p['drawers'] for p in projects.values())} drawers with a project key, "
            f"{plan['unresolved']} without one (stay)."
        )
        print("  Targets:")
        for target, n in list(plan_targets(plan).items())[:25]:
            hows = {p["how"] for p in projects.values() if p["target"] == target}
            print(f"    {target:<32} {n:>7}  ({', '.join(sorted(hows))})")
        if len(plan_targets(plan)) > 25:
            print(f"    ... {len(plan_targets(plan)) - 25} more targets in the plan file")

    if not getattr(args, "yes", False):
        if os.path.exists(split_pending_path(config, wing)):
            # Re-planning now would see only the drawers not yet moved and
            # overwrite the plan the interrupted split is following, edited
            # targets included, so the rest would split by different targets.
            print(
                f"  A split of {wing} was interrupted. Its plan at "
                f"{split_plan_path(config, wing)} is kept as is; re-run with --yes to finish it."
            )
            return
        col = get_collection(palace_path, create=False, read_only=True)
        plan = plan_split(col, wing, existing_wings())
        if not plan["projects"]:
            print(f"  No drawer in {wing} carries a project key; nothing to split.")
            return
        path = save_split_plan(config, plan)
        report(plan)
        print(f"\n  Plan saved to {path}. Edit targets there, then re-run with --yes.")
        return

    try:
        plan = load_split_plan(config, wing)
    except FileNotFoundError:
        print(
            f"  No plan for {wing}. Run without --yes first to create {split_plan_path(config, wing)}."
        )
        sys.exit(1)
    except ValueError as exc:
        print(f"  Plan at {split_plan_path(config, wing)} is invalid: {exc}")
        sys.exit(1)
    report(plan)
    from ..backends import CollectionNotInitializedError
    from ..palace import get_closets_collection

    with _repair_lock(palace_path):
        col = get_collection(palace_path, create=False)
        # Only a closet collection that was never created means "no closets".
        # Any other failure to open it must stop the command with its
        # recovery marker kept, or the closet phase is skipped for good.
        try:
            closets_col = get_closets_collection(palace_path, create=False)
        except CollectionNotInitializedError:
            closets_col = None
        # Drawers, then closets, then the hallway drop. A retry that finds no
        # drawer left must still finish the later phases, so the marker says a
        # split started; it is removed only after every phase has run.
        marker = split_pending_path(config, wing)
        resuming = os.path.exists(marker)
        if resuming:
            print("  Resuming an interrupted split: finishing drawers, closets and hallways.")
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat() + "\n")
        try:
            result = apply_split(
                col, plan, config=config, closets_col=closets_col, resuming=resuming
            )
        except KeyboardInterrupt:
            print(
                "\n  Interrupted. Rows already moved stay moved; re-run --yes to finish the rest."
            )
            raise
        os.remove(marker)
    print(
        f"\n  Moved {result['moved']} drawers into {len(result['per_target'])} wings and "
        f"{result['closets_moved']} closets; {result['skipped']} skipped; "
        f"{result['hallways_dropped']} hallway records of {wing} dropped "
        f"(run `mempalace hallways --rebuild`)."
    )
