# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace tunnels propose|prune`: reviewable cross-wing links (mempalace/tunnels_tool.py).


def _wings_from_sqlite(config, palace_path):
    from ..palace_graph import sqlite_grouped_counts_reader

    reader = sqlite_grouped_counts_reader(config)
    rows = reader(palace_path, config.collection_name) if reader else None
    if rows is None:
        return None
    return {str(r[1]) for r in rows if r[1]}


def cmd_tunnels(args):
    from ..hallways import list_hallways
    from ..palace import mine_lock
    from ..palace_graph import _get_tunnel_file, _load_tunnels, _save_tunnels
    from ..tunnels_tool import (
        apply_proposal,
        load_proposal,
        proposal_path,
        propose_tunnels,
        prune_tunnels,
        save_proposal,
    )

    action = getattr(args, "tunnels_action", None)
    if action not in {"propose", "prune"}:
        print("usage: mempalace tunnels {propose,prune} [--yes]")
        sys.exit(2)
    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    wings = _wings_from_sqlite(config, palace_path)
    if wings is None:
        print("  tunnels needs a sqlite-readable palace to know which wings exist.")
        sys.exit(1)
    apply = getattr(args, "yes", False)

    if action == "prune":
        if not apply:
            kept, report = prune_tunnels(_load_tunnels(config), wings)
        else:
            # Load, prune and save under the same lock every tunnel writer
            # takes, or a mine's create_tunnel in between is silently lost.
            with mine_lock(_get_tunnel_file(config)):
                kept, report = prune_tunnels(_load_tunnels(config), wings)
                if report["removed"]:
                    _save_tunnels(kept, config)
        print(
            f"  {report['removed']} of {report['total']} tunnels are artifacts: "
            f"{report['generic']} generic tokens, {report['dangling']} dangling endpoints, "
            f"{report['duplicates']} duplicate spellings."
        )
        if not report["removed"]:
            return
        if apply:
            print(f"  Removed {report['removed']}.")
        else:
            print("  Dry run. Re-run with --yes to remove them.")
        return

    if not apply:
        plan = propose_tunnels(
            list_hallways(config=config),
            wings,
            max_tunnels=getattr(args, "max", 60) or 60,
            existing_tunnels=_load_tunnels(config),
        )
        if not plan["tunnels"]:
            print("  No shared entities strong enough to propose a tunnel that does not exist.")
            return
        path = save_proposal(config, plan)
        print(
            f"  {plan['candidates']} candidate links; proposing the strongest {len(plan['tunnels'])}:"
        )
        for row in plan["tunnels"][:30]:
            print(
                f"    {row['entity']:<28} {row['wing_a']} <-> {row['wing_b']}  ({row['strength']})"
            )
        if len(plan["tunnels"]) > 30:
            print(f"    ... {len(plan['tunnels']) - 30} more in the plan file")
        print(f"\n  Plan saved to {path}. Delete rows you do not want, then re-run with --yes.")
        return

    try:
        plan = load_proposal(config)
    except FileNotFoundError:
        print(f"  No proposal at {proposal_path(config)}. Run without --yes first.")
        sys.exit(1)
    except ValueError as exc:
        print(f"  Proposal is invalid: {exc}")
        sys.exit(1)
    # The plan was written for review, so a wing may have been renamed, split
    # or removed since. `create_tunnel` does not validate entity endpoints, so
    # a stale row would be written as a tunnel the audit then counts as an
    # artifact. Drop those rows instead of creating them.
    from ..config import normalize_wing_name

    known = {normalize_wing_name(w) or w for w in wings}
    fresh = [
        row
        for row in plan["tunnels"]
        if {
            normalize_wing_name(row["wing_a"]) or row["wing_a"],
            normalize_wing_name(row["wing_b"]) or row["wing_b"],
        }
        <= known
    ]
    stale = len(plan["tunnels"]) - len(fresh)
    if not fresh:
        print(f"  Every proposed tunnel names a wing that no longer exists ({stale} rows).")
        print("  Re-run `mempalace tunnels propose` against the current wings.")
        sys.exit(1)
    created = apply_proposal({**plan, "tunnels": fresh}, config=config)
    note = f" Skipped {stale} row(s) naming a wing that no longer exists." if stale else ""
    existing = len(fresh) - created
    if existing:
        note += f" {existing} already existed, possibly under another spelling."
    print(f"  Created {created} tunnels.{note}")
