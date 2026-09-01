#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mempalace_graph.config import ConfigError, add_common_args, load_config
from mempalace_graph.mempalace_discovery import discover_mempalace
from mempalace_graph.mempalace_schema_inspector import format_schema_report, inspect_schema


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect MemPalace storage read-only.")
    add_common_args(parser)
    args = parser.parse_args()
    try:
        config = load_config(args)
        config.validate_mempalace_paths(require_write_log=False)
        discovery = discover_mempalace(config)
        inspection = inspect_schema(discovery.knowledge_graph_db.path)
        print(format_schema_report(inspection, config.mempalace_home, discovery.write_log.path))
        if discovery.chroma_db.exists:
            print("Chroma database:")
            print(discovery.chroma_db.path)
        if not discovery.write_log.exists:
            print(f"Warning: write log not found at {discovery.write_log.path}")
        return 0
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
