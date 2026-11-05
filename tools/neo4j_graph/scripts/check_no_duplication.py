#!/usr/bin/env python
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mempalace_graph.config import add_common_args, load_config
from mempalace_graph.neo4j_client import Neo4jClient
from mempalace_graph.sync_state import SyncState


def main() -> int:
    parser = argparse.ArgumentParser(description="Check that no full memory content was duplicated.")
    add_common_args(parser)
    args = parser.parse_args()
    try:
        config = load_config(args)
        client = Neo4jClient(config.neo4j_uri, config.neo4j_username, config.neo4j_password, config.neo4j_database)
        try:
            duplicated_nodes = client.check_no_duplication()
        finally:
            client.close()
        state = SyncState(config.sync_state_path)
        try:
            forbidden_columns = state.forbidden_columns()
        finally:
            state.close()
        print("No duplicated memory content found." if duplicated_nodes == 0 and not forbidden_columns else "Duplicated memory content found.")
        print(f"Neo4j duplicated nodes: {duplicated_nodes}")
        print(f"SQLite forbidden columns: {len(forbidden_columns)}")
        for column in forbidden_columns:
            print(column)
        return 0 if duplicated_nodes == 0 and not forbidden_columns else 1
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
