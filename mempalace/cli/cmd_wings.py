# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace wings split`: one wing per source project (mempalace/wing_split.py).


def cmd_wings(args):
    from ..wing_split import (
        apply_split,
        load_split_plan,
        plan_split,
        plan_targets,
        save_split_plan,
        split_plan_path,
    )

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
    from ..palace import mine_palace_lock

    from ..palace import get_closets_collection

    with mine_palace_lock(palace_path):
        col = get_collection(palace_path, create=False)
        try:
            closets_col = get_closets_collection(palace_path, create=False)
        except Exception:
            closets_col = None
        try:
            result = apply_split(col, plan, config=config, closets_col=closets_col)
        except KeyboardInterrupt:
            print(
                "\n  Interrupted. Rows already moved stay moved; re-run --yes to finish the rest."
            )
            raise
    print(
        f"\n  Moved {result['moved']} drawers into {len(result['per_target'])} wings and "
        f"{result['closets_moved']} closets; {result['skipped']} skipped; "
        f"{result['hallways_dropped']} hallway records of {wing} dropped "
        f"(run `mempalace hallways --rebuild`)."
    )
