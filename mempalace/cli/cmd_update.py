# Loaded into mempalace.cli via exec (see __init__.py). Not a standalone module.
if __name__ != "mempalace.cli":
    raise ImportError(f"{__name__} is an implementation fragment; import mempalace.cli")


def cmd_update(args):
    """Configure, check, or prepare updates without installing automatically."""
    import json

    from ..update_awareness import check_updates, configure_updates, prepare_upgrade

    try:
        if args.update_action == "configure":
            result = configure_updates(
                enabled=args.enabled,
                interval_days=args.interval_days,
                installer=args.installer,
            )
        elif args.update_action == "check":
            result = check_updates(force=True)
        elif args.update_action == "plan":
            result = prepare_upgrade(installer=args.installer)
        else:
            raise ValueError("choose update configure, check, or plan")
    except (OSError, ValueError) as exc:
        print(f"mempalace update: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, ensure_ascii=False))
