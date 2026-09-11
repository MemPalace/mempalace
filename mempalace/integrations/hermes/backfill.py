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

    # --- JSONL: one SESSION object per line (`hermes sessions export`
    # writes ``{**session, "messages": [...]}``) or one message per line
    # (raw transcripts). Session objects get the same unwrap the .json
    # dict branch applies — without it a real export file silently
    # parses to zero exchanges.
    if path.suffix == ".jsonl":
        messages = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if "messages" in obj or "turns" in obj:
                inner = obj.get("messages", obj.get("turns", []))
                if isinstance(inner, list):
                    messages.extend(inner)
            else:
                messages.append(obj)
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
    """Segment raw messages into exchange dicts.

    Delegates to the live provider's ``_segment_turns`` — the same
    anchoring the runtime safety net uses: a turn spans one real user
    message plus everything (tool_use, tool results, the FINAL answer)
    up to the next real user message, all normalized by
    ``_normalize_content``. A hand-rolled pairing that grabs only the
    immediately-following assistant message takes the tool_use stub as
    "the" answer and drops the real one — and tool-using turns are the
    COMMON case for a coding agent, so that quietly violated verbatim
    for most of a palace's history. One segmentation implementation,
    not two kept in sync by hand (same rule as ``classify_wing``).
    """
    from mempalace.integrations.hermes import _segment_turns

    return [
        {"user": seg["user"], "assistant": seg["assistant"]} for seg in _segment_turns(messages)
    ]


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
        exchanges.append({"user": pending_user, "assistant": "\n".join(current_lines).strip()})

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
    """Return the best matching wing name, or 'wing_general'.

    Delegates to the live provider's ``_match_wing_by_keywords`` — one
    implementation instead of two copies kept in sync by hand, so
    backfilled drawers route to exactly the same wings as live writes.
    The absolute import works even though this file is loaded via
    ``spec_from_file_location``: the install flow pip-installs mempalace
    into the Hermes venv before backfill ever runs (filing already
    imports ``mempalace.backends.chroma`` the same way).
    """
    from mempalace.integrations.hermes import _match_wing_by_keywords

    return _match_wing_by_keywords(text, wing_config)


# ---------------------------------------------------------------------------
# Filing
# ---------------------------------------------------------------------------


def file_exchange(
    exchange: dict,
    wing: str,
    source_file: str,
    *,
    collection=None,
    dry_run: bool = False,
) -> bool:
    """File a single exchange into the palace. Returns True on success.

    Routes through ``convo_miner.file_conversation_exchange`` — the same
    canonical write path the live provider's ``_file_turn`` uses — so
    routing, normalization, and metadata conventions stay identical
    between historical and live ingest.

    ``collection`` is a ChromaDB collection produced once by ``backfill()`` via
    ``mempalace.backends.chroma.ChromaBackend.get_or_create_collection`` — the
    same construction path the runtime provider uses. Pre-built so we don't
    spin up a fresh ``PersistentClient`` per exchange, and so the canonical
    embedding function is bound (avoiding the dim-mismatch bug that broke the
    prior in-tree Hermes-side PRs on existing palaces).
    """
    user_msg = exchange.get("user", "").strip()
    assistant_msg = exchange.get("assistant", "").strip()
    # Only both-empty is unfillable. Assistant-only exchanges (anchorless
    # preamble segments from a transcript starting mid-turn) are verbatim
    # history too.
    if not user_msg and not assistant_msg:
        return False

    from mempalace.integrations.hermes import _compose_exchange_text

    # The live provider's composition, verbatim — the dedup safety net's
    # exact-text matching compares against precisely this string.
    text = _compose_exchange_text(user_msg, assistant_msg)

    if dry_run:
        preview = text[:120].replace("\n", " ")
        logger.info("  [dry-run] [%s] %s...", wing, preview)
        return True

    if collection is None:
        logger.debug("file_exchange: collection is None — skipping write")
        return False

    try:
        from mempalace.convo_miner import file_conversation_exchange

        # Session files are historical — stamp authored_at from the file's
        # mtime so date filters reflect when the conversation happened,
        # not when the backfill ran.
        authored_at = None
        try:
            authored_at = datetime.fromtimestamp(Path(source_file).stat().st_mtime).isoformat()
        except OSError:
            pass
        drawer_id = file_conversation_exchange(
            collection,
            wing=wing,
            room="conversations",
            text=text,
            source_file=source_file,
            agent="hermes",
            authored_at=authored_at,
            extra_metadata={"source": "hermes_backfill"},
        )
        return drawer_id is not None
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

    # Open the collection once via ChromaBackend so the canonical embedding
    # function is bound and we don't recreate a PersistentClient per exchange.
    collection = None
    if not dry_run:
        try:
            from mempalace.backends.chroma import ChromaBackend

            backend = ChromaBackend()
            collection = backend.get_or_create_collection(palace_path, "mempalace_drawers")
        except Exception as exc:
            logger.error("Could not open palace collection at %s: %s", palace_path, exc)
            return 0

    total_filed = 0
    total_skipped = 0

    for i, session_file in enumerate(session_files, 1):
        # Exchange drawer ids are salted with filed_at, so re-filing the
        # same session mints fresh ids instead of overwriting — without
        # this guard a re-run of ``mempalace hermes install`` duplicates
        # every exchange. Same per-source idempotency check the convo
        # miner uses; extract_mode scoping matches the drawers
        # ``file_conversation_exchange`` writes.
        if collection is not None:
            from mempalace.palace import file_already_mined

            if file_already_mined(collection, str(session_file), extract_mode="exchange"):
                if verbose:
                    logger.info(
                        "  [%d/%d] %s — already backfilled",
                        i,
                        len(session_files),
                        session_file.name,
                    )
                continue

        exchanges = parse_session_file(session_file)
        if not exchanges:
            if verbose:
                logger.info("  [%d/%d] %s — no exchanges", i, len(session_files), session_file.name)
            continue

        filed_in_file = 0
        wing = "skipped"  # last-filed wing, shown in the verbose summary line
        for exchange in exchanges:
            combined = (exchange.get("user", "") + " " + exchange.get("assistant", "")).strip()
            wing = classify_wing(combined, wing_config)
            ok = file_exchange(
                exchange,
                wing=wing,
                source_file=str(session_file),
                collection=collection,
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

    sessions_dir = (
        Path(args.sessions_dir).expanduser()
        if args.sessions_dir
        else Path.home() / ".hermes" / "sessions"
    )
    palace_path = (
        str(Path(args.palace_path).expanduser())
        if args.palace_path
        else str(Path.home() / ".mempalace" / "palace")
    )

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
