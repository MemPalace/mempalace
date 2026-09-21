# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def _cmd_oplog_status(palace_path, as_json):
    import json

    from ..oplog import OPLOG_DB_FILENAME, OpLog

    db_path = os.path.join(palace_path, OPLOG_DB_FILENAME)
    if not os.path.exists(db_path):
        _logstream_fail(f"no op-log at {db_path} (no ops have been emitted yet)", as_json)
    with OpLog(db_path) as log:
        report = {
            "palace": palace_path,
            "replica_id": log.replica_id,
            "ops": log.count(),
            "by_kind": log.kind_histogram(),
            "version_vector": log.version_vector(),
        }
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"  op-log for {palace_path} ({report['replica_id']}): {report['ops']} ops")
        for kind, count in sorted(report["by_kind"].items()):
            print(f"    {kind}: {count}")
        for origin, top in sorted(report["version_vector"].items()):
            print(f"  origin {origin}: highest seq {top}")


def _cmd_oplog_sync(args, palace_path, as_json):
    import json

    from ..oplog import OPLOG_DB_FILENAME, OpLog
    from ..opsync import sync_all_memops, sync_memops_with_peer
    from ..transport import HttpsBearerTransport

    try:
        if getattr(args, "peer", None):
            with OpLog(os.path.join(palace_path, OPLOG_DB_FILENAME)) as log:
                stats = sync_memops_with_peer(
                    log,
                    HttpsBearerTransport(palace_path),
                    {"name": args.peer, "url": args.peer, "token": getattr(args, "token", None) or ""},
                )
                stats["peer_name"] = args.peer
                results = [stats]
        else:
            results = sync_all_memops(palace_path)
            if not results:
                _logstream_fail(
                    f"no peers configured ({palace_path}/peers.json) and no --peer given",
                    as_json,
                )
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for stats in results:
            if stats.get("error"):
                print(f"  {stats.get('peer_name', '?')}: ERROR {stats['error']}")
            else:
                print(
                    f"  {stats.get('peer_name', stats.get('peer_replica', '?'))}: "
                    f"+{stats.get('pulled_ops', 0)} ops"
                )
    if any(s.get("error") for s in results):
        sys.exit(1)


def _cmd_oplog_fold(palace_path, as_json):
    import json

    from ..knowledge_graph import KnowledgeGraph
    from ..oplog import OPLOG_DB_FILENAME, OpLog
    from ..opfold import fold_ops
    from ..palace import get_collection

    try:
        with OpLog(os.path.join(palace_path, OPLOG_DB_FILENAME)) as log:
            collection = get_collection(palace_path, create=True)
            kg_path = os.path.join(palace_path, "knowledge_graph.sqlite3")
            with KnowledgeGraph(db_path=kg_path) as kg:
                stats = fold_ops(log, collection, kg)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    if as_json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        print(f"  fold complete: {json.dumps(stats, ensure_ascii=False)}")
    if stats.get("errors"):
        sys.exit(1)


def _cmd_oplog_promote(args, palace_path, as_json):
    import json

    from ..oplog import OPLOG_DB_FILENAME, OpLog
    from ..oppromote import promote_local_rows
    from ..palace import get_collection

    try:
        collection = get_collection(palace_path, create=False)
        with OpLog(os.path.join(palace_path, OPLOG_DB_FILENAME)) as log:
            stats = promote_local_rows(
                log,
                collection,
                dry_run=bool(getattr(args, "dry_run", False)),
                limit=getattr(args, "limit", None),
                batch=max(1, int(getattr(args, "batch", 2000) or 2000)),
            )
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    if as_json:
        print(json.dumps(stats, indent=2, ensure_ascii=False))
    else:
        verb = "would promote" if stats.get("dry_run") else "promoted"
        print(
            f"  {verb} {stats.get('promoted', 0)} drawer(s) as {stats.get('replica_id', '?')}"
        )
    if stats.get("errors"):
        sys.exit(1)


def _cmd_oplog_verify(palace_path, as_json):
    import json

    from ..oplog_verify import verify_shadow

    try:
        report = verify_shadow(palace_path)
    except Exception as exc:
        _logstream_fail(str(exc), as_json)
    if as_json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(f"  verify: {'CLEAN' if report.get('clean') else 'DIVERGED'}")
        print(json.dumps({k: v for k, v in report.items() if k != 'clean'}, indent=2, ensure_ascii=False))
    if not report.get("clean"):
        sys.exit(1)


def cmd_oplog(args):
    """RFC 004 step 2a: canonical memory op-log — status, sync, fold, promote, verify."""
    as_json = getattr(args, "json", False)
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    action = getattr(args, "oplog_action", None)
    if action == "status":
        _cmd_oplog_status(palace_path, as_json)
    elif action == "sync":
        _cmd_oplog_sync(args, palace_path, as_json)
    elif action == "fold":
        _cmd_oplog_fold(palace_path, as_json)
    elif action == "promote":
        _cmd_oplog_promote(args, palace_path, as_json)
    elif action == "verify":
        _cmd_oplog_verify(palace_path, as_json)
    else:
        _logstream_fail("oplog requires a subcommand: status|sync|fold|promote|verify", as_json)


def cmd_migrate_ids(args):
    """v4 content-pure id migration (RFC 004). Dry-run by default; --apply needs --target."""
    import json as _json

    from ..knowledge_graph import KnowledgeGraph
    from ..migrate_v4 import (
        apply_v4_migration,
        copy_replica_sidecars,
        plan_v4_migration,
        read_kg_source_ids,
        remap_kg_source_ids,
    )
    from ..palace import get_collection

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    as_json = getattr(args, "json", False)
    src = get_collection(palace_path, create=False)
    if not src:
        print(f"  No palace collection at {palace_path}")
        sys.exit(1)

    kg_path = os.path.join(palace_path, "knowledge_graph.sqlite3")
    kg_ids = set()
    if os.path.exists(kg_path):
        with KnowledgeGraph(db_path=kg_path) as kg:
            kg_ids = read_kg_source_ids(kg)

    plan = plan_v4_migration(src, kg_source_ids=kg_ids)
    summary = {k: v for k, v in plan.items() if not k.startswith("_") and "sample" not in k}
    if as_json:
        print(_json.dumps(summary, indent=2))
    else:
        print(f"\n  v4 id migration plan for {palace_path}")
        for key, val in summary.items():
            print(f"    {key}: {val}")

    if not getattr(args, "apply", False):
        if not as_json:
            print("\n  DRY RUN — re-run with --apply --target <new-palace> to migrate.")
        return

    target = os.path.expanduser(args.target) if getattr(args, "target", None) else None
    if not target:
        _logstream_fail("--apply requires --target <new-palace-path>", as_json)
    if os.path.abspath(target) == os.path.abspath(palace_path):
        _logstream_fail("--target must differ from the source palace", as_json)
    os.makedirs(target, exist_ok=True)
    tgt = get_collection(target, create=True)
    stats = apply_v4_migration(src, tgt)
    if os.path.exists(kg_path):
        import shutil

        tgt_kg = os.path.join(target, "knowledge_graph.sqlite3")
        shutil.copy2(kg_path, tgt_kg)
        with KnowledgeGraph(db_path=tgt_kg) as tkg:
            remap_kg_source_ids(tkg, stats.get("alias", {}))
    sidecars = copy_replica_sidecars(palace_path, target)
    if as_json:
        print(_json.dumps({"stats": stats, "sidecars": sidecars}, indent=2, ensure_ascii=False))
    else:
        print(f"\n  MIGRATED into {target}: {stats}")
        print(f"  sidecars: {sidecars}")


def cmd_reconcile_ids(args):
    """Drain legacy v3-keyed ghost drawers to content-hash v4 ids (dry-run default)."""
    import json as _json

    from .. import server_registry
    from ..palace import get_collection
    from ..reconcile_v3 import apply_v3_reconcile, plan_v3_reconcile

    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    as_json = getattr(args, "json", False)
    col = get_collection(palace_path, create=False)
    if not col:
        print(f"  No palace collection at {palace_path}")
        sys.exit(1)

    if not getattr(args, "apply", False):
        plan = plan_v3_reconcile(col)
        if as_json:
            print(_json.dumps({k: v for k, v in plan.items() if k != "sample"}, indent=2))
        else:
            print(f"\n  v3 ghost reconcile plan for {palace_path}")
            for key, val in plan.items():
                if key == "sample":
                    continue
                print(f"    {key}: {val}")
            print("\n  DRY RUN — STOP the hub, then re-run with --apply.")
        return

    live_hub = server_registry.read_live_serverinfo(palace_path)
    if live_hub and not live_hub.get("read_only") and not getattr(args, "force_live_hub", False):
        base_url = server_registry.client_base_url(live_hub)
        message = (
            "reconcile-ids --apply writes the palace directly; STOP the hub first "
            f"(live hub at {base_url}, pid {live_hub.get('pid')})."
        )
        _logstream_fail(message, as_json)

    stats = apply_v3_reconcile(col)
    if as_json:
        print(_json.dumps(stats, indent=2))
    else:
        print(f"\n  RECONCILED {palace_path}: {stats}")
        print(f"  Confirm: mempalace --palace {palace_path} oplog verify")


def cmd_replica(args):
    """RFC 004 step 1: read-replica pull of drawers + KG from peers."""
    import json

    as_json = getattr(args, "json", False)
    palace_path = os.path.expanduser(args.palace) if args.palace else MempalaceConfig().palace_path
    action = getattr(args, "replica_action", None)
    if action != "pull":
        _logstream_fail("replica requires subcommand: pull", as_json)

    from ..replica_sync import pull_from_peers, pull_memory

    try:
        if getattr(args, "peer", None):
            results = [
                pull_memory(
                    palace_path,
                    args.peer,
                    getattr(args, "token", None) or "",
                    reconcile_deletes=not getattr(args, "no_reconcile", False),
                    pull_kg=not getattr(args, "no_kg", False),
                    with_vectors=getattr(args, "with_vectors", False),
                )
            ]
        else:
            results = pull_from_peers(
                palace_path,
                pull_kg=not getattr(args, "no_kg", False),
                with_vectors=getattr(args, "with_vectors", False),
            )
            if not results:
                _logstream_fail(
                    f"no peers configured ({palace_path}/peers.json) and no --peer given",
                    as_json,
                )
    except Exception as exc:
        _logstream_fail(str(exc), as_json)

    if as_json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for stats in results:
            if stats.get("error"):
                print(f"  {stats.get('peer_name', stats.get('origin_url', '?'))}: ERROR {stats['error']}")
            else:
                print(
                    f"  {stats.get('peer_name', stats.get('origin_url', '?'))} "
                    f"({stats.get('origin_replica', '?')}): "
                    f"{stats.get('drawers_upserted', 0)} drawers folded, "
                    f"{stats.get('drawers_deleted', 0)} reconciled away, "
                    f"KG +{stats.get('kg_entities', 0)} entities / +{stats.get('kg_triples', 0)} triples"
                )
    if any(s.get("error") for s in results):
        sys.exit(1)
