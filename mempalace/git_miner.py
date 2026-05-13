#!/usr/bin/env python3
"""
git_miner.py — Mines a git repo commit by commit, branch by branch.

Extracts commit messages, diffs, metadata, and files changed.
Stores verbatim content as drawers. No summaries. Ever.
"""

import subprocess
import hashlib
from datetime import datetime
from pathlib import Path
from collections import defaultdict

from .palace import (
    build_closet_lines,
    file_already_mined,
    get_closets_collection,
    get_collection,
    mine_lock,
    purge_file_closets,
    upsert_closet_lines,
)

# Chunking constants (matching miner.py)
CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200
MIN_CHUNK_SIZE = 50


def run_git(repo_path: Path, *args) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git"] + list(args),
        cwd=repo_path,
        capture_output=True,
    )
    return result.stdout.decode("utf-8", errors="replace").strip()


def get_branches(repo_path: Path) -> list:
    """Get all local and remote branch names."""
    output = run_git(repo_path, "branch", "--format=%(refname:short)")
    if not output:
        return []
    return [b.strip() for b in output.split("\n") if b.strip()]


def get_all_commits(repo_path: Path, branch: str = None) -> list:
    """Get all commits for a branch (newest first)."""
    cmd = ["log", "--format=%H%x00%ai%x00%an%x00%ae%x00%s%x00%b%x00"]
    if branch:
        cmd.insert(1, branch)
    output = run_git(repo_path, *cmd)

    if not output:
        return []

    commits = []
    entries = output.split("\x00")
    i = 0
    while i < len(entries):
        if not entries[i].strip():
            i += 1
            continue
        if len(entries) - i < 6:
            break
        hash_val = entries[i].strip()
        if not hash_val or len(hash_val) < 7:
            i += 1
            continue
        commits.append({
            "hash": hash_val,
            "date": entries[i + 1],
            "author": entries[i + 2],
            "email": entries[i + 3],
            "subject": entries[i + 4],
            "body": entries[i + 5],
        })
        i += 6
    return commits


def get_commit_diff(repo_path: Path, commit_hash: str) -> str:
    """Get the diff for a commit (excluding binary files)."""
    return run_git(repo_path, "show", commit_hash, "--format=", "--diff-filter=ACM")


def get_commit_files_changed(repo_path: Path, commit_hash: str) -> list:
    """List of files changed in a commit."""
    output = run_git(repo_path, "diff-tree", "--no-commit-id", "--name-only", "-r", commit_hash)
    if not output:
        return []
    return [f.strip() for f in output.split("\n") if f.strip()]


def detect_room_from_files(filepaths: list, fallback: str = "general") -> str:
    """Infer room from files changed in a commit."""
    if not filepaths:
        return fallback

    # Count extensions from actual filenames
    ext_counts = defaultdict(int)
    # Count path components (for directory-based detection)
    component_counts = defaultdict(int)

    for fp in filepaths:
        # Get the filename (last component) and its extension
        filename = Path(fp).name
        ext = Path(filename).suffix.lower()
        if ext:
            ext_counts[ext] += 1

        # Count all path components for directory detection
        parts = fp.split("/")
        for part in parts:
            if part and not part.startswith("."):
                component_counts[part] += 1

    # Room mapping for extensions
    room_mapping = {
        ".py": "code",
        ".js": "code",
        ".ts": "code",
        ".jsx": "code",
        ".tsx": "code",
        ".go": "code",
        ".rs": "code",
        ".java": "code",
        ".md": "docs",
        ".txt": "docs",
        ".yaml": "config",
        ".yml": "config",
        ".json": "config",
        ".toml": "config",
        ".sql": "database",
        ".css": "styles",
        ".scss": "styles",
    }

    # Check if the most common extension maps to a room
    if ext_counts:
        top_ext = max(ext_counts, key=ext_counts.get)
        if top_ext in room_mapping:
            return room_mapping[top_ext]

    # Fall back to most common path component
    if not component_counts:
        return fallback

    top_component = max(component_counts, key=component_counts.get)
    # Check if the top component has a mappable extension
    comp_ext = Path(top_component).suffix.lower()
    if comp_ext in room_mapping:
        return room_mapping[comp_ext]

    return top_component if component_counts[top_component] > 1 else fallback


def chunk_diff(diff: str) -> list:
    """Split diff into drawer-sized chunks. Returns list of (chunk_index, chunk_content)."""
    if not diff or len(diff) <= CHUNK_SIZE:
        return [(0, diff)]

    chunks = []
    start = 0
    chunk_index = 0

    while start < len(diff):
        end = min(start + CHUNK_SIZE, len(diff))

        # Try to break at a reasonable boundary (diff section header or newline)
        if end < len(diff):
            # Try to break at diff section header (@@)
            header_pos = diff.rfind("@@", start, end)
            if header_pos > start + CHUNK_SIZE // 2:
                end = header_pos
            else:
                # Try to break at newline
                newline_pos = diff.rfind("\n", start, end)
                if newline_pos > start + CHUNK_SIZE // 2:
                    end = newline_pos

        chunk = diff[start:end].strip()
        if len(chunk) >= MIN_CHUNK_SIZE:
            chunks.append((chunk_index, chunk))
            chunk_index += 1

        start = end - CHUNK_OVERLAP if end < len(diff) else end

    return chunks


def build_commit_header(commit: dict, files_changed: list) -> str:
    """Build the commit metadata header (same for all chunks)."""
    commit_hash = commit["hash"]
    subject = commit.get("subject", "")
    body = commit.get("body", "")
    author = commit.get("author", "")
    email = commit.get("email", "")
    date = commit.get("date", "")

    return f"""commit {commit_hash}
Author: {author} <{email}>
Date:   {date}

{subject}

{body}

Files changed:
{chr(10).join(files_changed) if files_changed else '(none)'}

Diff:
"""


def add_git_drawer(
    collection,
    wing: str,
    room: str,
    content: str,
    source: str,
    chunk_index: int,
    agent: str,
):
    """Add one drawer for a git commit chunk."""
    drawer_id = f"git_{wing}_{room}_{hashlib.sha256((source + str(chunk_index)).encode()).hexdigest()[:24]}"
    try:
        metadata = {
            "wing": wing,
            "room": room,
            "source_file": source,
            "chunk_index": chunk_index,
            "added_by": agent,
            "filed_at": datetime.now().isoformat(),
        }
        collection.upsert(
            documents=[content],
            ids=[drawer_id],
            metadatas=[metadata],
        )
        return True
    except Exception:
        raise


def process_commit(
    repo_path: Path,
    commit: dict,
    collection,
    closets_col,
    wing: str,
    agent: str,
    dry_run: bool,
):
    """Process a single commit into the palace, chunking diff into multiple drawers."""
    commit_hash = commit["hash"]
    source = f"{repo_path}:{commit_hash[:8]}"

    if not dry_run:
        if file_already_mined(collection, source, check_mtime=False):
            return 0, "general"

    diff = get_commit_diff(repo_path, commit_hash)
    files_changed = get_commit_files_changed(repo_path, commit_hash)
    room = detect_room_from_files(files_changed)

    if dry_run:
        chunks = chunk_diff(diff)
        print(f"    [DRY RUN] {commit_hash[:8]} -> room:{room} ({len(chunks)} chunks)")
        return len(chunks), room

    with mine_lock(source):
        if file_already_mined(collection, source, check_mtime=False):
            return 0, room

        try:
            collection.delete(where={"source_file": source})
        except Exception:
            pass

        # Build header (commit metadata)
        header = build_commit_header(commit, files_changed)

        # Chunk the diff
        diff_chunks = chunk_diff(diff)

        if not diff_chunks:
            # No valid chunks, still file with header only
            added = add_git_drawer(
                collection=collection,
                wing=wing,
                room=room,
                content=header + "(no diff)",
                source=source,
                chunk_index=0,
                agent=agent,
            )
            total_added = 1 if added else 0
        else:
            total_added = 0
            drawer_ids = []

            for chunk_index, diff_chunk in diff_chunks:
                content = header + diff_chunk
                added = add_git_drawer(
                    collection=collection,
                    wing=wing,
                    room=room,
                    content=content,
                    source=source,
                    chunk_index=chunk_index,
                    agent=agent,
                )
                if added:
                    drawer_id = f"git_{wing}_{room}_{hashlib.sha256((source + str(chunk_index)).encode()).hexdigest()[:24]}"
                    drawer_ids.append(drawer_id)
                    total_added += 1

            # Build closet for all drawers
            if closets_col and drawer_ids:
                all_content = header + diff
                closet_lines = build_closet_lines(source, drawer_ids, all_content, wing, room)
                closet_id_base = f"closet_{wing}_{room}_{hashlib.sha256(source.encode()).hexdigest()[:24]}"
                closet_meta = {
                    "wing": wing,
                    "room": room,
                    "source_file": source,
                    "drawer_count": len(drawer_ids),
                    "filed_at": datetime.now().isoformat(),
                }
                purge_file_closets(closets_col, source)
                upsert_closet_lines(closets_col, closet_id_base, closet_lines, closet_meta)

        return total_added, room


def mine_git(
    repo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    branches: list = None,
):
    """Mine a git repo commit by commit."""
    repo_path = Path(repo_dir).expanduser().resolve()

    if not (repo_path / ".git").exists():
        print(f"  Not a git repo: {repo_path}")
        return

    all_branches = get_branches(repo_path)
    if not all_branches:
        print(f"  No branches found in {repo_path}")
        return

    target_branches = branches if branches else all_branches

    wing_name = wing or repo_path.name

    print(f"\n{'=' * 55}")
    print("  MemPalace Git Mine")
    print(f"{'=' * 55}")
    print(f"  Repo:    {repo_path}")
    print(f"  Wing:    {wing_name}")
    print(f"  Branches: {len(target_branches)}")
    print(f"  Palace:  {palace_path}")
    if dry_run:
        print("  DRY RUN — nothing will be filed")
    print(f"{'-' * 55}\n")

    if not dry_run:
        collection = get_collection(palace_path)
        closets_col = get_closets_collection(palace_path)
    else:
        collection = None
        closets_col = None

    total_drawers = 0
    commits_processed = 0
    commits_skipped = 0
    branch_counts = defaultdict(int)
    visited = set()

    for branch in target_branches:
        print(f"  Branch: {branch}")
        commits = get_all_commits(repo_path, branch)

        for commit in commits:
            if limit > 0 and commits_processed >= limit:
                break

            commit_hash = commit["hash"]
            if commit_hash in visited:
                continue
            visited.add(commit_hash)

            drawers, room = process_commit(
                repo_path=repo_path,
                commit=commit,
                collection=collection,
                closets_col=closets_col,
                wing=wing_name,
                agent=agent,
                dry_run=dry_run,
            )

            if drawers == 0:
                commits_skipped += 1
            else:
                total_drawers += drawers
                branch_counts[branch] += 1
                commits_processed += 1
                if not dry_run:
                    print(f"    + {commit['hash'][:8]} [{drawers} drawers] {commit['subject'][:40]}")

        if limit > 0 and commits_processed >= limit:
            break

    print(f"\n{'=' * 55}")
    print("  Done.")
    print(f"  Commits processed: {commits_processed}")
    print(f"  Commits skipped (already filed): {commits_skipped}")
    print(f"  Drawers filed: {total_drawers}")
    print("  Next: mempalace search \"what you're looking for\"")
    print(f"{'=' * 55}\n")
