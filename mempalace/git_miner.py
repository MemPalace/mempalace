#!/usr/bin/env python3
"""
git_miner.py — Mine git history into the palace.

Extracts commits from a git repo: messages, authors, dates, files changed.
Each commit becomes one drawer. No diffs — just the story of what changed and why.

Same palace as project and conversation mining. Different ingest strategy.
"""

import os
import sys
import hashlib
import subprocess
from pathlib import Path
from datetime import datetime
from collections import defaultdict

import chromadb

# Delimiter unlikely to appear in commit messages
_DELIM = "---MEMPALACE_COMMIT---"
_FIELD_DELIM = "---MEMPALACE_FIELD---"

# Keyword sets for room detection (reused from convo_miner's TOPIC_KEYWORDS)
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


# =============================================================================
# GIT LOG EXTRACTION
# =============================================================================


def run_git_log(repo_dir, since=None, until=None, max_commits=0, branch=None):
    """Run git log and return a list of parsed commit dicts.

    Each dict has: hash, author, date, subject, body, files.
    """
    repo_path = Path(repo_dir).expanduser().resolve()

    fmt = _FIELD_DELIM.join(["%h", "%an", "%aI", "%s", "%b"])
    fmt = _DELIM + fmt

    cmd = [
        "git",
        "-C",
        str(repo_path),
        "log",
        f"--format={fmt}",
        "--name-only",
    ]
    if since:
        cmd.append(f"--since={since}")
    if until:
        cmd.append(f"--until={until}")
    if max_commits > 0:
        cmd.append(f"-n{max_commits}")
    if branch:
        cmd.append(branch)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        print("  ERROR: git not found. Is git installed?")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print("  ERROR: git log timed out (60s). Try --limit or --since to narrow the range.")
        sys.exit(1)

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "not a git repository" in stderr.lower():
            print(f"  ERROR: {repo_path} is not a git repository.")
        else:
            print(f"  ERROR: git log failed: {stderr}")
        sys.exit(1)

    return _parse_git_log(result.stdout)


def _parse_git_log(raw):
    """Parse the structured git log output into commit dicts."""
    commits = []
    # Split on our delimiter; first element is empty
    entries = raw.split(_DELIM)

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue

        # The first line contains our delimited fields, followed by
        # file names on subsequent lines
        lines = entry.split("\n")
        header = lines[0]

        parts = header.split(_FIELD_DELIM)
        if len(parts) < 4:
            continue

        commit_hash = parts[0].strip()
        author = parts[1].strip()
        date = parts[2].strip()
        subject = parts[3].strip()
        body = parts[4].strip() if len(parts) > 4 else ""

        # Remaining non-empty lines are changed file paths
        files = [line.strip() for line in lines[1:] if line.strip()]

        commits.append(
            {
                "hash": commit_hash,
                "author": author,
                "date": date,
                "subject": subject,
                "body": body,
                "files": files,
            }
        )

    return commits


# =============================================================================
# CONTENT FORMATTING
# =============================================================================


def format_commit_content(commit):
    """Format a commit dict into a document string for storage."""
    parts = [commit["subject"]]

    if commit["body"]:
        parts.append(commit["body"])

    if commit["files"]:
        # Show up to 20 files
        file_list = commit["files"][:20]
        suffix = f" (+{len(commit['files']) - 20} more)" if len(commit["files"]) > 20 else ""
        parts.append("Files: " + ", ".join(file_list) + suffix)

    return "\n\n".join(parts)


# =============================================================================
# ROOM DETECTION — hybrid: file paths + keyword scoring
# =============================================================================


def detect_git_room(commit, rooms_config=None):
    """Detect the best room for a commit.

    Strategy:
    1. If rooms_config provided, check if changed files map to a room name
    2. Score commit message against topic keywords
    3. Fallback: "general"
    """
    # Strategy 1: file-path matching against project rooms
    if rooms_config:
        room_scores = defaultdict(int)
        for filepath in commit["files"]:
            path_lower = filepath.lower()
            for room in rooms_config:
                room_name = room["name"].lower()
                if room_name in path_lower:
                    room_scores[room["name"]] += 1
                # Also check room keywords against file path
                for kw in room.get("keywords", []):
                    if kw.lower() in path_lower:
                        room_scores[room["name"]] += 1
        if room_scores:
            return max(room_scores, key=room_scores.get)

    # Strategy 2: keyword scoring on commit message
    text = (commit["subject"] + " " + commit["body"]).lower()
    scores = {}
    for room, keywords in TOPIC_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scores[room] = score
    if scores:
        return max(scores, key=scores.get)

    return "general"


# =============================================================================
# PALACE OPERATIONS
# =============================================================================


def get_collection(palace_path):
    os.makedirs(palace_path, exist_ok=True)
    client = chromadb.PersistentClient(path=palace_path)
    try:
        return client.get_collection("mempalace_drawers")
    except Exception:
        return client.create_collection("mempalace_drawers")


def commit_already_mined(collection, source_uri):
    try:
        results = collection.get(where={"source_file": source_uri}, limit=1)
        return len(results.get("ids", [])) > 0
    except Exception:
        return False


# =============================================================================
# MAIN: MINE GIT LOG
# =============================================================================


def mine_git_log(
    repo_dir,
    palace_path,
    wing=None,
    agent="mempalace",
    limit=0,
    dry_run=False,
    extract_mode="exchange",
    since=None,
    until=None,
    branch=None,
):
    """Mine git history from a repository into the palace."""

    repo_path = Path(repo_dir).expanduser().resolve()
    if not wing:
        wing = repo_path.name.lower().replace(" ", "_").replace("-", "_")

    # Load project room config if available
    rooms_config = None
    for config_name in ("mempalace.yaml", "mempal.yaml"):
        config_file = repo_path / config_name
        if config_file.exists():
            import yaml

            with open(config_file) as f:
                cfg = yaml.safe_load(f)
            rooms_config = cfg.get("rooms", None)
            break

    commits = run_git_log(
        repo_dir,
        since=since,
        until=until,
        max_commits=limit,
        branch=branch,
    )

    print(f"\n{'=' * 55}")
    print("  MemPalace Mine — Git Log")
    print(f"{'=' * 55}")
    print(f"  Wing:    {wing}")
    print(f"  Repo:    {repo_path}")
    print(f"  Commits: {len(commits)}")
    print(f"  Palace:  {palace_path}")
    if since:
        print(f"  Since:   {since}")
    if until:
        print(f"  Until:   {until}")
    if branch:
        print(f"  Branch:  {branch}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'─' * 55}\n")

    collection = get_collection(palace_path) if not dry_run else None

    total_drawers = 0
    commits_skipped = 0
    room_counts = defaultdict(int)

    for i, commit in enumerate(commits, 1):
        source_uri = f"git://{repo_path}#{commit['hash']}"

        # Dedup
        if not dry_run and commit_already_mined(collection, source_uri):
            commits_skipped += 1
            continue

        content = format_commit_content(commit)
        if len(content.strip()) < 20:
            continue

        # Room detection or general extraction
        if extract_mode == "general":
            from .general_extractor import extract_memories

            chunks = extract_memories(content)
            if not chunks:
                # Fall back to filing the whole commit as-is
                room = detect_git_room(commit, rooms_config)
                chunks = [{"content": content, "memory_type": room, "chunk_index": 0}]
        else:
            room = detect_git_room(commit, rooms_config)
            chunks = [{"content": content, "chunk_index": 0}]

        if dry_run:
            if extract_mode == "general":
                types = ", ".join(c.get("memory_type", "?") for c in chunks)
                print(f"    [DRY RUN] {commit['hash']} {commit['subject'][:50]} → {types}")
            else:
                print(f"    [DRY RUN] {commit['hash']} {commit['subject'][:50]} → room:{room}")
            total_drawers += len(chunks)
            for c in chunks:
                room_counts[c.get("memory_type", room)] += 1
            continue

        drawers_added = 0
        for chunk in chunks:
            chunk_room = chunk.get("memory_type", room) if extract_mode == "general" else room
            room_counts[chunk_room] += 1
            drawer_id = (
                f"drawer_{wing}_{chunk_room}_"
                f"{hashlib.md5((source_uri + str(chunk['chunk_index'])).encode()).hexdigest()[:16]}"
            )
            try:
                collection.add(
                    documents=[chunk["content"]],
                    ids=[drawer_id],
                    metadatas=[
                        {
                            "wing": wing,
                            "room": chunk_room,
                            "source_file": source_uri,
                            "chunk_index": chunk["chunk_index"],
                            "added_by": agent,
                            "filed_at": datetime.now().isoformat(),
                            "ingest_mode": "git-log",
                            "commit_hash": commit["hash"],
                            "commit_author": commit["author"],
                            "commit_date": commit["date"],
                            "files_changed": ", ".join(commit["files"][:30]),
                        }
                    ],
                )
                drawers_added += 1
            except Exception as e:
                if "already exists" not in str(e).lower():
                    raise

        total_drawers += drawers_added
        if drawers_added:
            print(
                f"  \u2713 [{i:4}/{len(commits)}] {commit['hash']} {commit['subject'][:45]:45} +{drawers_added}"
            )

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Commits processed: {len(commits) - commits_skipped}")
    print(f"  Commits skipped (already filed): {commits_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    if room_counts:
        print("\n  By room:")
        for room_name, count in sorted(room_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"    {room_name:20} {count} commits")
    print('\n  Next: mempalace search "what you\'re looking for"')
    print(f"{'=' * 55}\n")
