#!/usr/bin/env python
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mempalace_graph.config import ConfigError, add_common_args, load_config
from mempalace_graph.sync_engine import sync_once


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync MemPalace metadata into Neo4j.")
    add_common_args(parser)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--create-schema", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, format="%(levelname)s: %(message)s")
    if not args.once and not args.watch:
        parser.error("choose --once or --watch")
    try:
        config = load_config(args)
        config.validate_mempalace_paths(require_write_log=False)
        if args.watch:
            try:
                from mempalace_graph.file_watcher import watch
            except ModuleNotFoundError as exc:
                print(f"Error: missing watch dependency: {exc}. Run pip install -r requirements.txt.", file=sys.stderr)
                return 1
            watch(config, create_schema=args.create_schema)
            return 0
        result = sync_once(config, create_schema=args.create_schema, dry_run=args.dry_run)
        print("MemPalace sync complete" if not args.dry_run else "MemPalace dry run complete")
        print(f"Files scanned: {result.files_scanned}")
        print(f"Files changed: {result.files_changed}")
        print(f"Records seen: {result.records_seen}")
        print(f"Records upserted: {result.records_upserted}")
        print(f"Records soft-deleted: {result.records_soft_deleted}")
        print(f"Records hard-deleted: {result.records_hard_deleted}")
        print(f"Errors: {len(result.errors)}")
        for error in result.errors:
            print(error)
        return 0
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if "Neo4j" in str(exc) or "ServiceUnavailable" in exc.__class__.__name__:
            print("Error: Neo4j connection failed. Check docker compose and NEO4J_PASSWORD.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
