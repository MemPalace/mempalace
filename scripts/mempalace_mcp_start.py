#!/usr/bin/env python3
"""
mempalace_mcp_start.py — Repo-owned launcher for the MemPalace MCP server.

Replaces the fragile .bat launcher. Provides:
- Path resolution via pathlib (no hardcoded drive letters or Python paths)
- Pre-flight dependency check (mempalace, chromadb)
- HNSW quarantine with errors logged, not suppressed
- Palace openability healthcheck — exits 1 with clear message on failure
- Fail-loud: any startup failure prints to stderr and exits non-zero
"""

import argparse
import logging
import sys
from pathlib import Path

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[mempalace-launcher] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _check_deps() -> None:
    missing = []
    for pkg in ("mempalace", "chromadb"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        logger.error(
            "Missing required packages: %s — run: pip install %s",
            ", ".join(missing),
            " ".join(missing),
        )
        sys.exit(1)


def _quarantine(palace_path: str) -> None:
    try:
        from mempalace.backends.chroma import quarantine_stale_hnsw

        quarantine_stale_hnsw(palace_path, stale_seconds=30.0)
        logger.info("HNSW quarantine check complete")
    except Exception as e:
        logger.warning("HNSW quarantine raised: %s (continuing)", e)


def _healthcheck(palace_path: str) -> None:
    try:
        import chromadb

        client = chromadb.PersistentClient(palace_path)
        client.list_collections()
        logger.info("Palace healthcheck passed (%s)", palace_path)
    except Exception as e:
        logger.error("Palace healthcheck FAILED: %s", e)
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="MemPalace MCP launcher")
    parser.add_argument("--palace", required=True, help="Path to palace directory")
    args, remaining = parser.parse_known_args()

    palace_path = str(Path(args.palace).resolve())

    if not Path(palace_path).exists():
        logger.error("Palace directory does not exist: %s", palace_path)
        sys.exit(1)

    _check_deps()
    _quarantine(palace_path)
    _healthcheck(palace_path)

    logger.info("Starting MemPalace MCP server (palace=%s)", palace_path)
    sys.argv = ["mempalace-mcp", "--palace", palace_path] + remaining
    from mempalace.mcp_server import main as mcp_main

    mcp_main()


if __name__ == "__main__":
    main()
