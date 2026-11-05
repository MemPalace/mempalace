#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mempalace_graph.config import add_common_args, load_config
from mempalace_graph.content_resolver import ResolveError, resolve_content
from mempalace_graph.neo4j_client import Neo4jClient


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve full memory content from original MemPalace source.")
    parser.add_argument("memory_id")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--open-source", action="store_true")
    add_common_args(parser)
    args = parser.parse_args()
    try:
        config = load_config(args)
        client = Neo4jClient(config.neo4j_uri, config.neo4j_username, config.neo4j_password, config.neo4j_database)
        try:
            pointer = client.get_memory_source(args.memory_id)
        finally:
            client.close()
        resolved = resolve_content(pointer.source_path, pointer.source_record_locator)
        if args.open_source:
            print(resolved.source_path)
            return 0
        if args.json:
            print(json.dumps(resolved.__dict__, indent=2, ensure_ascii=False))
            return 0
        print(f"Title: {resolved.title or ''}")
        print(f"Source: {resolved.source_path}")
        print(f"Locator: {resolved.source_record_locator}")
        print("Metadata:")
        print(json.dumps(resolved.metadata, indent=2, ensure_ascii=False))
        print("Content:")
        print(resolved.content)
        return 0
    except ResolveError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
