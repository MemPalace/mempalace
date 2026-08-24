#!/usr/bin/env python3
"""
convo_miner.py — Mine conversations into the palace.

Ingests chat exports (Claude Code, ChatGPT, Slack, plain text transcripts).
Normalizes format, chunks by exchange pair (Q+A = one unit), files to palace.

Same palace as project mining. Different ingest strategy.
"""

import os
import sys
import hashlib
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import chromadb

from . import file_state
from .normalize import normalize


# File types that might contain conversations
CONVO_EXTENSIONS = {
    ".txt",
    ".md",
    ".json",
    ".jsonl",
}

SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "dist",
    "build",
    ".next",
    ".mempalace",
    "tool-results",
    "memory",
}

MIN_CHUNK_SIZE = 30


# =============================================================================
# CHUNKING — exchange pairs for conversations
# =============================================================================


def chunk_exchanges(content: str) -> list:
    """
    Chunk by exchange pair: one > turn + AI response = one unit.
    Falls back to paragraph chunking if no > markers.
    """
    lines = content.split("\n")
    quote_lines = sum(1 for line in lines if line.strip().startswith(">"))

    if quote_lines >= 3:
        return _chunk_by_exchange(lines)
    else:
        return _chunk_by_paragraph(content)


def _chunk_by_exchange(lines: list) -> list:
    """One user turn (>) + the AI response that follows = one chunk."""
    chunks = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(">"):
            user_turn = line.strip()
            i += 1

            ai_lines = []
            while i < len(lines):
                next_line = lines[i]
                if next_line.strip().startswith(">") or next_line.strip().startswith("---"):
                    break
                if next_line.strip():
                    ai_lines.append(next_line.strip())
                i += 1

            ai_response = " ".join(ai_lines[:8])
            content = f"{user_turn}\n{ai_response}" if ai_response else user_turn

            if len(content.strip()) > MIN_CHUNK_SIZE:
                chunks.append(
                    {
                        "content": content,
                        "chunk_index": len(chunks),
                    }
                )
        else:
            i += 1

    return chunks


def _chunk_by_paragraph(content: str) -> list:
    """Fallback: chunk by paragraph breaks."""
    chunks = []
    paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]

    # If no paragraph breaks and long content, chunk by line groups
    if len(paragraphs) <= 1 and content.count("\n") > 20:
        lines = content.split("\n")
        for i in range(0, len(lines), 25):
            group = "\n".join(lines[i : i + 25]).strip()
            if len(group) > MIN_CHUNK_SIZE:
                chunks.append({"content": group, "chunk_index": len(chunks)})
        return chunks

    for para in paragraphs:
        if len(para) > MIN_CHUNK_SIZE:
            chunks.append({"content": para, "chunk_index": len(chunks)})

    return chunks


# =============================================================================
# ROOM DETECTION — topic-based for conversations
# =============================================================================

TOPIC_KEYWORDS = {
    "technical": [
        "code",
        "python",
        "function",
        "bug",
        "error",
        "api",
        "database",
        "server",
        "deploy",
        "git",
        "test",
        "debug",
        "refactor",
    ],
    "architecture": [
        "architecture",
        "design",
        "pattern",
        "structure",
        "schema",
        "interface",
        "module",
        "component",
        "service",
        "layer",
    ],
    "planning": [
        "plan",
        "roadmap",
        "milestone",
        "deadline",
        "priority",
        "sprint",
        "backlog",
        "scope",
        "requirement",
        "spec",
    ],
    "decisions": [
        "decided",
        "chose",
        "picked",
        "switched",
        "migrated",
        "replaced",
        "trade-off",
        "alternative",
        "option",
        "approach",
    ],
    "problems": [
        "problem",
        "issue",
        "broken",
        "failed",
        "crash",
        "stuck",
        "workaround",
        "fix",
        "solved",
        "resolved",
    ],
}


def detect_convo_room(content: str) -> str:
    """Score conversation content against topic keywords."""
    content_lower = content[:3000].lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in content_lower)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)
    return "general"


# =============================================================================
# PALACE OPERATIONS
# =============================================================================


def get_collection(palace_path: str):
    os.makedirs(palace_path, exist_ok=True)
    client = chromadb.PersistentClient(path=palace_path)
    try:
        return client.get_collection("mempalace_drawers")
    except Exception:
        return client.create_collection("mempalace_drawers")


def file_already_mined(collection, source_file: str) -> bool:
    """Path-only check: does this path have any drawer at all?

    Mining uses file_state.decide() instead, which also compares mtime and
    content hash so a session log that kept growing gets re-filed.
    """
    try:
        results = collection.get(where={"source_file": source_file}, limit=1)
        return len(results.get("ids", [])) > 0
    except Exception:
        return False


# =============================================================================
# SCAN FOR CONVERSATION FILES
# =============================================================================


def scan_convos(convo_dir: str) -> list:
    """Find all potential conversation files."""
    convo_path = Path(convo_dir).expanduser().resolve()
    files = []
    for root, dirs, filenames in os.walk(convo_path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for filename in filenames:
            if filename.endswith(".meta.json"):
                continue
            filepath = Path(root) / filename
            if filepath.suffix.lower() in CONVO_EXTENSIONS:
                files.append(filepath)
    return files


# =============================================================================
# MINE CONVERSATIONS
# =============================================================================


def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
    """Mine a directory of conversation files into the palace.

    extract_mode:
        "exchange" — default exchange-pair chunking (Q+A = one unit)
        "general"  — general extractor: decisions, preferences, milestones, problems, emotions
    """

    convo_path = Path(convo_dir).expanduser().resolve()
    if not wing:
        wing = convo_path.name.lower().replace(" ", "_").replace("-", "_")

    files = scan_convos(convo_dir)
    if limit > 0:
        files = files[:limit]

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Conversations")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Source:  {convo_path}")
    print(f"  Files:   {len(files)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'-' * 55}\n")

    collection = get_collection(palace_path) if not dry_run else None

    total_drawers = 0
    files_skipped = 0
    files_remined = 0
    files_empty = 0
    room_counts = defaultdict(int)

    for i, filepath in enumerate(files, 1):
        source_file = str(filepath)

        # Skip only if the file is byte-for-byte what we already filed.
        if dry_run:
            action, content_hash, source_mtime = file_state.MINE, None, None
        else:
            action, content_hash, source_mtime = file_state.decide(collection, source_file)
            if action == file_state.SKIP:
                files_skipped += 1
                continue

        # Normalize format
        try:
            content = normalize(str(filepath))
        except (OSError, ValueError):
            # Couldn't read it — leave whatever is already filed alone.
            continue

        if action == file_state.REMINE:
            # Content moved on. Drop the old drawers before filing the new
            # ones: exchange boundaries shift as a session grows, so writing
            # by id would leave the previous version's tail behind.
            file_state.drop_drawers(collection, source_file)

        if not content or len(content.strip()) < MIN_CHUNK_SIZE:
            files_empty += 1
            continue

        # Chunk — either exchange pairs or general extraction
        if extract_mode == "general":
            from .general_extractor import extract_memories

            chunks = extract_memories(content)
            # Each chunk already has memory_type; use it as the room name
        else:
            chunks = chunk_exchanges(content)

        if not chunks:
            files_empty += 1
            continue

        # Detect room from content (general mode uses memory_type instead)
        if extract_mode != "general":
            room = detect_convo_room(content)
        else:
            room = None  # set per-chunk below

        if dry_run:
            if extract_mode == "general":
                from collections import Counter

                type_counts = Counter(c.get("memory_type", "general") for c in chunks)
                types_str = ", ".join(f"{t}:{n}" for t, n in type_counts.most_common())
                print(f"    [DRY RUN] {filepath.name} → {len(chunks)} memories ({types_str})")
            else:
                print(f"    [DRY RUN] {filepath.name} → room:{room} ({len(chunks)} drawers)")
            total_drawers += len(chunks)
            # Track room counts
            if extract_mode == "general":
                for c in chunks:
                    room_counts[c.get("memory_type", "general")] += 1
            else:
                room_counts[room] += 1
            continue

        if extract_mode != "general":
            room_counts[room] += 1

        # File each chunk
        drawers_added = 0
        for chunk in chunks:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            if extract_mode == "general":
                room_counts[chunk_room] += 1
            drawer_id = f"drawer_{wing}_{chunk_room}_{hashlib.md5((source_file + str(chunk['chunk_index'])).encode(), usedforsecurity=False).hexdigest()[:16]}"
            try:
                collection.add(
                    documents=[chunk["content"]],
                    ids=[drawer_id],
                    metadatas=[
                        {
                            "wing": wing,
                            "room": chunk_room,
                            "source_file": source_file,
                            "chunk_index": chunk["chunk_index"],
                            "added_by": agent,
                            "filed_at": datetime.now().isoformat(),
                            "ingest_mode": "convos",
                            "extract_mode": extract_mode,
                            # The stamp the next run reads to tell
                            # "unchanged" from "edited".
                            **(
                                {"content_hash": content_hash}
                                if content_hash is not None
                                else {}
                            ),
                            **(
                                {"source_mtime": float(source_mtime)}
                                if source_mtime is not None
                                else {}
                            ),
                        }
                    ],
                )
                drawers_added += 1
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise

        if action == file_state.REMINE:
            files_remined += 1
        total_drawers += drawers_added
        mark = "↻" if action == file_state.REMINE else "✓"
        print(f"  {mark} [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Files processed: {len(files) - files_skipped}")
    print(f"  Files skipped (unchanged): {files_skipped}")
    print(f"  Files re-filed (content changed): {files_remined}")
    print(f"  Files with nothing to file: {files_empty}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room:20} {count} files")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python convo_miner.py <convo_dir> [--palace PATH] [--limit N] [--dry-run]")
        sys.exit(1)
    from .config import MempalaceConfig

    mine_convos(sys.argv[1], palace_path=MempalaceConfig().palace_path)
