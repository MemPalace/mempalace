#!/usr/bin/env python3
"""
backfill.py — Mine existing Hermes session history into MemPalace.

Scans the Hermes session store for conversation files and files them
into the palace using the same wing-classification logic as the live
provider. Run this once after installing the MemPalace provider to
seed your palace with historical context.

Usage:
    cd ~/.hermes/hermes-agent
    venv/bin/python3 -m plugins.memory.mempalace.backfill

    # Or with options:
    venv/bin/python3 -m plugins.memory.mempalace.backfill \\
        --sessions-dir ~/.hermes/sessions \\
        --palace-path ~/.mempalace/palace \\
        --limit 100 \\
        --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("mempalace.backfill")


# ---------------------------------------------------------------------------
# Session discovery
# ---------------------------------------------------------------------------


def find_session_files(sessions_dir: Path) -> list[Path]:
    """
    Recursively find Hermes session files.
    Supports .json, .jsonl, and .md conversation exports.
    """
    files: list[Path] = []
    for ext in ("*.json", "*.jsonl", "*.md"):
        files.extend(sessions_dir.rglob(ext))
    return sorted(files)


def parse_session_file(path: Path) -> list[dict]:
    """
    Parse a session file into a list of {user, assistant} exchange dicts.
    Handles JSON array, JSONL, and plain markdown conversation formats.
    """
    exchanges: list[dict] = []
    text = path.read_text(encoding="utf-8", errors="replace")

    # --- JSONL: one message object per line ---
    if path.suffix == ".jsonl":
        messages = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        exchanges = _messages_to_exchanges(messages)

    # --- JSON: array of messages or {"messages": [...]} ---
    elif path.suffix == ".json":
        try:
            data = json.loads(text)
            if isinstance(data, list):
                exchanges = _messages_to_exchanges(data)
            elif isinstance(data, dict):
                messages = data.get("messages", data.get("turns", []))
                exchanges = _messages_to_exchanges(messages)
        except json.JSONDecodeError:
            pass

    # --- Markdown: "User:" / "Assistant:" block format ---
    elif path.suffix == ".md":
        exchanges = _parse_markdown_session(text)

    return exchanges


def _messages_to_exchanges(messages: list[dict]) -> list[dict]:
    """Pair consecutive user/assistant messages into exchange dicts."""
    exchanges: list[dict] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")
        content = msg.get("content", "") or ""
        if isinstance(content, list):
            # Handle structured content blocks
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if role == "user" and content.strip():
            assistant_content = ""
            if i + 1 < len(messages) and messages[i + 1].get("role") == "assistant":
                next_content = messages[i + 1].get("content", "") or ""
                if isinstance(next_content, list):
                    next_content = " ".join(
                        block.get("text", "")
                        for block in next_content
                        if isinstance(block, dict)
                    )
                assistant_content = next_content
                i += 1
            exchanges.append({"user": content.strip(), "assistant": assistant_content.strip()})
        i += 1
    return exchanges


def _parse_markdown_session(text: str) -> list[dict]:
    """Parse a markdown session with 'User:' / 'Assistant:' headings."""
    exchanges: list[dict] = []
    current_role = None
    current_lines: list[str] = []
    pending_user = ""

    for line in text.splitlines():
        if line.lower().startswith("user:") or line.lower().startswith("**user**"):
            if current_role == "assistant" and pending_user:
                exchanges.append(
                    {"user": pending_user, "assistant": "\n".join(current_lines).strip()}
                )
                pending_user = ""
                current_lines = []
            elif current_role == "user":
                pending_user = "\n".join(current_lines).strip()
                current_lines = []
            current_role = "user"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                current_lines.append(rest)
        elif line.lower().startswith("assistant:") or line.lower().startswith("**assistant**"):
            if current_role == "user":
                pending_user = "\n".join(current_lines).strip()
                current_lines = []
            current_role = "assistant"
            rest = line.split(":", 1)[1].strip() if ":" in line else ""
            if rest:
                current_lines.append(rest)
        else:
            current_lines.append(line)

    # Flush last block
    if current_role == "assistant" and pending_user:
        exchanges.append(
            {"user": pending_user, "assistant": "\n".join(current_lines).strip()}
        )

    return exchanges


# ---------------------------------------------------------------------------
# Wing classification
# ---------------------------------------------------------------------------


def load_wing_config(palace_path: Path) -> dict:
    """Load wing_config.json if present, else return empty dict."""
    wing_config_path = palace_path.parent / "wing_config.json"
    if wing_config_path.exists():
        try:
            with open(wing_config_path) as f:
                return json.load(f).get("wings", {})
        except Exception as exc:
            logger.warning("Could not load wing_config.json: %s", exc)
    return {}


def classify_wing(text: str, wing_config: dict) -> str:
    """Return the best matching wing name, or 'wing_general'."""
    if not wing_config:
        return "wing_general"
    text_lower = text.lower()
    for wing_name, wing_def in wing_config.items():
        keywords = wing_def.get("keywords", [])
        if any(kw.lower() in text_lower for kw in keywords):
            return wing_name
    return "wing_general"


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


def file_exchange(
    exchange: dict,
    wing: str,
    palace_path: str,
    source_file: str,
    dry_run: bool = False,
) -> bool:
    """File a single exchange into the palace. Returns True on success."""
    user_msg = exchange.get("user", "").strip()
    assistant_msg = exchange.get("assistant", "").strip()
    if not user_msg:
        return False

    text = f"User: {user_msg}"
    if assistant_msg:
        text += f"\n\nAssistant: {assistant_msg}"

    if dry_run:
        preview = text[:120].replace("\n", " ")
        logger.info("  [dry-run] [%s] %s...", wing, preview)
        return True

    try:
        import chromadb

        ts = datetime.utcnow().isoformat()
        doc_id = hashlib.sha256(f"{source_file}:{text[:120]}".encode()).hexdigest()[:16]

        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")
        col.upsert(
            ids=[doc_id],
            documents=[text],
            metadatas=[
                {
                    "wing": wing,
                    "room": "conversations",
                    "source": "hermes_backfill",
                    "source_file": source_file,
                    "ts": ts,
                }
            ],
        )
        return True
    except Exception as exc:
        logger.debug("Error filing exchange: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Main backfill routine
# ---------------------------------------------------------------------------


def backfill(
    sessions_dir: Path,
    palace_path: str,
    limit: int = 0,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """
    Scan sessions_dir, parse each session file, and file exchanges into
    the palace. Returns the number of exchanges successfully filed.
    """
    if not sessions_dir.exists():
        logger.error("Sessions directory not found: %s", sessions_dir)
        return 0

    wing_config = load_wing_config(Path(palace_path))
    if wing_config:
        logger.info("Wing config loaded: %d wings", len(wing_config))
    else:
        logger.info("No wing config — all sessions will go to wing_general")

    session_files = find_session_files(sessions_dir)
    if not session_files:
        logger.info("No session files found in %s", sessions_dir)
        return 0

    logger.info("Found %d session files", len(session_files))
    if limit:
        session_files = session_files[:limit]
        logger.info("Processing first %d files (--limit)", limit)

    total_filed = 0
    total_skipped = 0

    for i, session_file in enumerate(session_files, 1):
        exchanges = parse_session_file(session_file)
        if not exchanges:
            if verbose:
                logger.info("  [%d/%d] %s — no exchanges", i, len(session_files), session_file.name)
            continue

        filed_in_file = 0
        for exchange in exchanges:
            combined = (exchange.get("user", "") + " " + exchange.get("assistant", "")).strip()
            wing = classify_wing(combined, wing_config)
            ok = file_exchange(
                exchange,
                wing=wing,
                palace_path=palace_path,
                source_file=str(session_file),
                dry_run=dry_run,
            )
            if ok:
                filed_in_file += 1
            else:
                total_skipped += 1

        total_filed += filed_in_file
        if verbose or dry_run:
            logger.info(
                "  [%d/%d] %s — %d exchanges → %s",
                i,
                len(session_files),
                session_file.name,
                filed_in_file,
                wing if filed_in_file else "skipped",
            )

    action = "Would file" if dry_run else "Filed"
    logger.info("\n%s %d exchanges from %d session files", action, total_filed, len(session_files))
    if total_skipped:
        logger.info("Skipped %d (empty or unparseable)", total_skipped)

    return total_filed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Backfill existing Hermes sessions into MemPalace.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sessions-dir",
        default=None,
        help="Path to Hermes sessions directory (default: ~/.hermes/sessions)",
    )
    parser.add_argument(
        "--palace-path",
        default=None,
        help="Path to MemPalace palace directory (default: ~/.mempalace/palace)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max session files to process (0 = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be filed without writing anything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-file progress",
    )

    args = parser.parse_args()

    sessions_dir = Path(args.sessions_dir).expanduser() if args.sessions_dir else Path.home() / ".hermes" / "sessions"
    palace_path = str(Path(args.palace_path).expanduser()) if args.palace_path else str(Path.home() / ".mempalace" / "palace")

    if args.dry_run:
        logger.info("(dry run — nothing will be written)")

    n = backfill(
        sessions_dir=sessions_dir,
        palace_path=palace_path,
        limit=args.limit,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if n == 0 and not args.dry_run:
        sys.exit(1)


if __name__ == "__main__":
    main()
