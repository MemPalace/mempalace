# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace kg normalize`: closed predicate vocabulary (mempalace/kg_normalize.py).


def cmd_kg(args):
    from ..kg_normalize import (
        DEFAULT_VOCABULARY,
        apply_normalize,
        load_normalize_plan,
        normalize_plan_path,
        off_vocabulary_facts,
        plan_normalize,
        save_normalize_plan,
    )
    from ..knowledge_graph import KnowledgeGraph

    action = getattr(args, "kg_action", None)
    if action not in ("normalize", "import-legacy"):
        print("usage: mempalace kg {normalize,import-legacy} [options]")
        sys.exit(2)
    palace = getattr(args, "palace", None)
    palace_path = os.path.expanduser(palace) if palace else MempalaceConfig().palace_path
    config = MempalaceConfig(palace_path=palace_path)
    if action == "import-legacy":
        _kg_import_legacy(args, config)
        return
    raw = getattr(args, "vocabulary", None)
    vocabulary = (
        tuple(v.strip().lower() for v in raw.split(",") if v.strip()) if raw else DEFAULT_VOCABULARY
    )
    kg_path = paths.knowledge_graph_path(palace_path, explicit=bool(palace))
    if not os.path.isfile(kg_path):
        print(f"  No knowledge graph at {kg_path}.")
        sys.exit(1)
    kg = KnowledgeGraph(db_path=kg_path)

    if not getattr(args, "yes", False):
        facts = off_vocabulary_facts(kg, vocabulary)
        print(f"  {len(facts)} open facts use a predicate outside: {', '.join(vocabulary)}")
        if not facts:
            return
        provider = _rooms_llm_provider(args)
        try:
            plan = plan_normalize(facts, provider, vocabulary)
        except (ValueError, LLMError) as exc:
            print(f"  Proposal failed: {exc}")
            sys.exit(1)
        path = save_normalize_plan(config, plan)
        for row in plan["facts"]:
            print(
                f"    {row['subject']} | {row['old_predicate']} -> {row['predicate']} | {row['object']}"
            )
        if plan["rejected"]:
            print(
                f"  {plan['rejected']} fact(s) got no usable rewrite; add them to the plan by hand."
            )
        print(f"\n  Plan saved to {path}. Edit it, then re-run with --yes to apply.")
        return

    try:
        plan = load_normalize_plan(config)
    except FileNotFoundError:
        print(f"  No plan at {normalize_plan_path(config)}. Run without --yes first.")
        sys.exit(1)
    except ValueError as exc:
        print(f"  Plan is invalid: {exc}")
        sys.exit(1)
    # The same cross-process writer lock the other repair commands hold: the
    # graph's own lock is process-local, and a running hub or another CLI
    # must not interleave fact writes with the rewrites.
    with _repair_lock(config.palace_path):
        result = apply_normalize(kg, plan)
    print(
        f"  Rewrote {result['applied']} fact(s) at {result['boundary']}; "
        f"{result['skipped']} unchanged, {result['stale']} no longer open (left alone). "
        "Old wordings remain valid before that instant."
    )


def _kg_import_legacy(args, config):
    """Copy another graph file's facts into this palace's own graph, on request only."""
    from ..knowledge_graph import import_graph_rows

    source = os.path.expanduser(args.source) if args.source else paths.legacy_knowledge_graph_path()
    target = os.path.join(config.palace_path, paths.KNOWLEDGE_GRAPH_FILENAME)
    if not os.path.isfile(source):
        print(f"  No knowledge graph at {source}.")
        sys.exit(1)
    if paths.same_path(source, target):
        print(f"  {source} is already this palace's knowledge graph.")
        sys.exit(1)
    apply = bool(getattr(args, "yes", False))
    if apply:
        # The same cross-process writer lock as normalize: a hub or another
        # CLI must not write facts while rows are copied in.
        with _repair_lock(config.palace_path):
            counts = import_graph_rows(source, target, apply=True)
    else:
        counts = import_graph_rows(source, target)
    verb = "Copied" if apply else "Would copy"
    for table, numbers in counts.items():
        print(
            f"  {verb} {numbers['added']} {table} from {source}; "
            f"{numbers['present']} already in {target} were kept as they are."
        )
    conflicts = counts["triples"]["conflicts"]
    if conflicts:
        print(
            f"\n  {len(conflicts)} current fact(s) not copied: the target or an earlier "
            "accepted source fact holds a "
            "different current value for the same subject and predicate. Add the ones "
            "you want by hand (kg_supersede or kg_add):"
        )
        for c in conflicts:
            print(
                f"    {c['subject']} | {c['predicate']} | {c['object']} "
                f"(here: {c['current_object']})"
            )
    id_conflicts = counts["triples"]["id_conflicts"]
    if id_conflicts:
        print(
            f"\n  {len(id_conflicts)} fact ID conflict(s) not copied: the target keeps "
            "a different fact or validity window under the same ID. Review these "
            "facts before importing them:"
        )
        for triple_id in id_conflicts:
            print(f"    {triple_id}")
    if not apply:
        print("\n  Nothing written. Re-run with --yes to copy them.")
