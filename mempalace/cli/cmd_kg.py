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
    from ..palace_audit import resolve_kg_path

    action = getattr(args, "kg_action", None)
    if action != "normalize":
        print("usage: mempalace kg normalize [--vocabulary a,b,c] [--yes]")
        sys.exit(2)
    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    raw = getattr(args, "vocabulary", None)
    vocabulary = (
        tuple(v.strip().lower() for v in raw.split(",") if v.strip()) if raw else DEFAULT_VOCABULARY
    )
    kg_path = resolve_kg_path(palace_path)
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
    result = apply_normalize(kg, plan)
    print(
        f"  Rewrote {result['applied']} fact(s) at {result['boundary']}; "
        f"{result['skipped']} unchanged. Old wordings remain valid before that instant."
    )
