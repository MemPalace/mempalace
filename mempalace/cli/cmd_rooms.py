# Fragment of mempalace.cli — executed into the package namespace.
# `mempalace rooms propose|apply`: closed room sets per wing (mempalace/rooms.py).


def _rooms_llm_provider(args):
    """Build the LLM provider for `rooms propose` with init's flags and consent gate."""
    provider_name = getattr(args, "llm_provider", "ollama") or "ollama"
    provider_model = getattr(args, "llm_model", "gemma4:e4b") or "gemma4:e4b"
    candidate = get_provider(
        name=provider_name,
        model=provider_model,
        endpoint=getattr(args, "llm_endpoint", None),
        api_key=getattr(args, "llm_api_key", None),
        timeout=getattr(args, "llm_timeout", 600) or 600,
    )
    if candidate.is_external_service:
        if not getattr(args, "accept_external_llm", False):
            print(
                f"  {provider_name} at {candidate.endpoint} is an EXTERNAL service: the sampled "
                "drawer excerpts would leave this machine. Re-run with --accept-external-llm "
                "to allow that, or point --llm-endpoint at a local server."
            )
            sys.exit(1)
        print(f"  Sending {getattr(args, 'sample', 0)} excerpts to EXTERNAL {provider_name}.")
    ok, msg = candidate.check_available()
    if not ok:
        print(f"  LLM unavailable ({provider_name}/{provider_model}): {msg}")
        sys.exit(1)
    return candidate


def cmd_rooms(args):
    from ..rooms import (
        DEFAULT_THRESHOLD,
        GENERIC_ROOMS,
        EmbeddingRoomDecider,
        apply_plan,
        existing_rooms,
        load_room_set,
        plan_rooms,
        propose_rooms,
        room_set_path,
        sample_drawers,
        save_room_set,
    )

    action = getattr(args, "rooms_action", None)
    if action not in {"propose", "apply"}:
        print("usage: mempalace rooms {propose,apply} --wing WING")
        sys.exit(2)
    palace_path = (
        os.path.expanduser(args.palace)
        if getattr(args, "palace", None)
        else MempalaceConfig().palace_path
    )
    config = MempalaceConfig(palace_path=palace_path)
    wing = args.wing
    from ..palace import get_collection

    if action == "propose":
        provider = _rooms_llm_provider(args)
        col = get_collection(palace_path, create=False, read_only=True)
        samples = sample_drawers(col, wing, n=args.sample, seed=args.seed)
        if not samples:
            print(f"  No drawers in wing {wing!r} at {palace_path}.")
            sys.exit(1)
        print(
            f"  Sampled {len(samples)} drawers from {wing}; asking {provider.name}/{provider.model}..."
        )
        try:
            room_set = propose_rooms(
                wing,
                samples,
                provider,
                max_rooms=args.max_rooms,
                existing=existing_rooms(col, wing),
            )
        except (ValueError, LLMError) as exc:
            print(f"  Proposal failed: {exc}")
            sys.exit(1)
        path = save_room_set(config, room_set)
        print(f"\n  Proposed {len(room_set.rooms)} rooms for {wing}:")
        for r in room_set.rooms:
            print(f"    {r.name:<28} ({len(r.exemplars):>2} exemplars) {r.description}")
        print(f"\n  Saved to {path}. Edit it, then: mempalace rooms apply --wing {wing}")
        return

    try:
        room_set = load_room_set(config, wing)
    except FileNotFoundError:
        print(f"  No room set for {wing}. Run: mempalace rooms propose --wing {wing}")
        sys.exit(1)
    except ValueError as exc:
        print(f"  Room set at {room_set_path(config, wing)} is invalid: {exc}")
        sys.exit(1)

    from ..embedding import get_embedding_function

    apply = getattr(args, "yes", False)
    threshold = args.threshold if args.threshold is not None else DEFAULT_THRESHOLD
    embed = get_embedding_function()
    col = get_collection(palace_path, create=False, read_only=not apply)
    decider = EmbeddingRoomDecider(room_set, embed, col=col)
    print(
        "  Room prototypes: "
        + ", ".join(f"{n} ({src})" for n, src in decider.prototype_source.items())
    )
    scope = getattr(args, "from_rooms", None)
    if scope == "all":
        from_rooms = None
    elif scope:
        from_rooms = frozenset(r.strip() for r in scope.split(",") if r.strip())
    else:
        from_rooms = GENERIC_ROOMS

    def report(plan):
        s = plan.summary()
        scope_label = "all rooms" if from_rooms is None else ", ".join(sorted(from_rooms))
        print(f"  Reclassifying drawers currently in: {scope_label}")
        print(
            f"  {wing}: {s['total']} drawers; {s['changed']} would move, {s['kept']} stay, "
            f"{s['below_threshold']} below threshold {threshold:.2f} (kept), "
            f"{s['no_embedding']} without embeddings (kept)."
        )
        for room, n in s["per_room"].items():
            print(f"    {room:<28} {n}")

    if not apply:
        plan = plan_rooms(col, wing, decider, threshold=threshold, from_rooms=from_rooms)
        report(plan)
        show = getattr(args, "show", 0) or 0
        if show and plan.changes:
            print("\n  Examples of what would move (confidence, excerpt):")
            for room, lines in plan.example_excerpts(col, per_room=show).items():
                print(f"    -> {room}")
                for line in lines:
                    print(f"       {line}")
        if plan.changes:
            print("\n  Dry run. Re-run with --yes to write the room changes.")
        return

    from ..palace import mine_palace_lock

    with mine_palace_lock(palace_path):
        plan = plan_rooms(col, wing, decider, threshold=threshold, from_rooms=from_rooms)
        report(plan)
        if not plan.changes:
            print("  Nothing to change.")
            return
        done = apply_plan(col, plan)
        print(f"  Moved {done} drawers. Run `mempalace audit` to see the new rooms score.")
