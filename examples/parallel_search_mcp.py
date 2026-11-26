#!/usr/bin/env python3
"""
research-capture — Parallel Search MCP → MemPalace integration example
=======================================================================

A guided, interactive workflow that lets you turn a public research query
into a verbatim drawer in your palace, with explicit control over what goes
online and what gets saved.

  1.  Issue a public research query.

  2.  Run it against the Parallel Search MCP server
      (https://docs.parallel.ai/integrations/mcp/search-mcp) at
      https://search.parallel.ai/mcp — free web search and page fetching,
      no API key (anonymous rate limits apply).

  3.  Show the search results and fetch the text of the URL you pick.

  4.  Preview the extracted page text.

  5.  ONLY after you explicitly confirm, save that text — unchanged, with its
      source URL recorded — into your MemPalace wing / room via
      ``mempalace_add_drawer``.

Design rules
------------

* **Extracted, not original.**  The text stored is the *extracted page text*
  returned by the fetcher, byte-for-byte.  Its provenance (source URL) is
  carried in ``source_file`` and ``added_by`` so it is never mistaken for the
  original page bytes.  No re-encoding, trimming, or "normalisation" is applied
  between fetch and store.

* **Explicit confirmation gate.**  Nothing is written to the palace before you
  see the preview and type ``y``.

* **No palace content in web requests.**  What is sent to Parallel Search is a
  plain free-text query and, later, a single public URL.  Existing drawer
  content is never read into a web request.

* **Isolated dependency.**  The MCP client (``mcp``) and the network fetch
  path live entirely in this file.  Core MemPalace, its dependencies, and the
  offline memory path are unchanged.  Install the extra with
  ``pip install 'mcp[cli]'`` (see ``examples/parallel_search_mcp/README.md``).

* **Offline-testable.**  The pure logic — building the drawer payload, text
  preservation, the confirm/cancel gate, and the write → recall round-trip —
  is exercised in ``tests/test_parallel_search_example.py`` with no live
  network and no embedding service, using the repo's existing
  ``_patch_mcp_server`` test pattern.

Run
---
    python examples/parallel_search_mcp.py --wing project_a --room research_notes

    # non-interactive (pipe in the query, auto-yes, dry-run)
    echo "rust async runtime comparison 2025" | \
        python examples/parallel_search_mcp.py --wing p --room r --auto --dry-run

CLI flags
---------
--mcp-url       MemPalace MCP endpoint (default http://127.0.0.1:9090/mcp)
--mcp-token     Bearer token for the MemPalace MCP server
--wing          Wing to file into   (default: research)
--room          Room to file into   (default: captured_web)
--json          Emit the drawer payload as JSON (machine-readable)
--quiet         Suppress progress / status lines
--auto          Skip the interactive pick + confirm prompts (for piping)
--dry-run       Build the drawer payload without calling the MCP server
--max-results   Max search results to show (default: 5)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

# ── Parallel Search MCP (no API key for basic usage) ──────────────────
PARALLEL_SEARCH_MCP_URL = "https://search.parallel.ai/mcp"

# ── MemPalace MCP default ──────────────────────────────────────────────
DEFAULT_MEMPALACE_URL = "http://127.0.0.1:9090/mcp"

# Provenance label recorded on every captured drawer so the stored text is
# identified as *extracted page text* (never the original page bytes).
PROVENANCE_SOURCE = "parallel_search_extracted_text"
ADDED_BY_LABEL = "parallel_search_example"

# MCP client is isolated to this example.  Core MemPalace does not need it.
try:  # pragma: no cover - exercised at runtime, not in offline tests
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    HAS_MCP_CLIENT = True
except ImportError:  # pragma: no cover
    ClientSession = None  # type: ignore[assignment]
    streamablehttp_client = None  # type: ignore[assignment]
    HAS_MCP_CLIENT = False


# ═══════════════════════════════════════════════════════════════════════
#  Data model — pure, offline-testable
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class SearchResult:
    """A single search-hit summary returned by Parallel Search MCP."""

    title: str
    url: str
    description: str = ""


@dataclass
class CapturedItem:
    """One unit of *extracted* text to be filed into MemPalace on confirm."""

    wing: str
    room: str
    content: str  # extracted page text, byte-for-byte
    source_url: str  # URL the text was fetched from
    captured_at: str  # ISO-8601 UTC
    captured_by: str = ADDED_BY_LABEL

    def to_add_drawer_kwargs(self) -> dict:
        """kwargs for ``mempalace_add_drawer``.

        The ``content`` is the extracted text, unchanged.  Provenance travels in
        ``source_file`` (the URL) and ``added_by`` (the capture label), so the
        drawer is identifiable as extracted web text but the text itself is not
        wrapped, rewritten, or truncated.
        """
        return {
            "wing": self.wing,
            "room": self.room,
            "content": self.content,
            "source_file": f"{PROVENANCE_SOURCE} :: {self.source_url}",
            "added_by": self.captured_by,
        }


def build_captured(
    wing: str,
    room: str,
    content: str,
    source_url: str,
    captured_by: str = ADDED_BY_LABEL,
) -> CapturedItem:
    """Construct a CapturedItem from a fetch result.  Pure / offline-testable."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return CapturedItem(
        wing=wing,
        room=room,
        content=content,
        source_url=source_url,
        captured_at=now,
        captured_by=captured_by,
    )


def preview(content: str, limit: int = 3000) -> str:
    """Return ``content`` clipped for display only (store always uses raw)."""
    if len(content) <= limit:
        return content
    head = content[: limit - 40]
    tail_note = f"\n… [{len(content) - limit + 40} more chars — full text will be saved]"
    return head + tail_note


def confirm(prompt: str = "File into MemPalace? [y/N] ") -> bool:
    """Read confirmation from stdin.  Any non-yes answer (incl. EOF) → False."""
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ═══════════════════════════════════════════════════════════════════════
#  Network layer — isolated, injected-or-live
# ═══════════════════════════════════════════════════════════════════════


def _build_headers(token: str) -> dict:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _post_jsonrpc(url: str, name: str, arguments: dict, token: str = "", id: int = 1) -> Any:
    """Send one MCP ``tools/call`` over HTTP JSON-RPC and return the parsed result.

    Used against MemPalace's MCP HTTP transport (``mempalace/mcp_server/http.py``).
    """
    payload = {
        "jsonrpc": "2.0",
        "id": id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=_build_headers(token),
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {err[:400]}") from e

    if "error" in body:
        raise RuntimeError(f"MCP error: {body['error']}")
    result = body.get("result", {})
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if block.get("type") == "text":
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return block["text"]
    return result


def file_captured(mcp_url: str, mcp_token: str, item: CapturedItem, dry_run: bool = False) -> dict:
    """Send a confirmed CapturedItem to MemPalace via ``mempalace_add_drawer``."""
    kwargs = item.to_add_drawer_kwargs()
    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "drawer_id": f"dry_run::{item.wing}/{item.room}",
            "would_file": kwargs,
        }
    return _post_jsonrpc(mcp_url, "mempalace_add_drawer", kwargs, token=mcp_token)


def _run(coro):
    """Run an MCP client coroutine on a fresh event loop."""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _mcp_tool_first_match(tool_names: tuple, arguments: dict, block_type: str = "text"):
    """Try a list of tool names on Parallel Search MCP; return first text block."""
    if not HAS_MCP_CLIENT:
        raise RuntimeError(
            "The `mcp` client is not installed.  This is isolated to the example:\n"
            "    pip install 'mcp[cli]'"
        )
    headers = {"User-Agent": "mempalace-research-capture/1.0"}
    async with streamablehttp_client(PARALLEL_SEARCH_MCP_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as session:
            await session.initialize()
            last_err: Optional[Exception] = None
            for tool_name in tool_names:
                try:
                    return await session.call_tool(tool_name, arguments)
                except Exception as exc:  # tool may not exist under this name
                    last_err = exc
                    continue
            raise RuntimeError(
                f"No working tool found on Parallel Search MCP (tried {tool_names}): {last_err}"
            )


async def parallel_search(query: str, max_results: int = 5) -> list[SearchResult]:
    """Query Parallel Search MCP.  Returns list[SearchResult] (may be empty)."""
    result = await _mcp_tool_first_match(
        ("web_search", "search", "web_search_v2"),
        {"query": query, "max_results": max_results},
    )
    hits: list[SearchResult] = []
    for block in result.content:
        if block.type != "text":
            continue
        try:
            data = json.loads(block.text)
        except json.JSONDecodeError:
            continue
        for item in data.get("results", data.get("hits", [])):
            hits.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", item.get("link", "")),
                    description=item.get("description", item.get("snippet", "")),
                )
            )
    return hits


async def parallel_fetch(url: str) -> str:
    """Fetch page content from Parallel Search MCP; return extracted text."""
    result = await _mcp_tool_first_match(
        ("web_fetch", "fetch", "page_fetch"),
        {"url": url},
    )
    for block in result.content:
        if block.type != "text":
            continue
        try:
            data = json.loads(block.text)
            return data.get("text", data.get("content", ""))
        except json.JSONDecodeError:
            return block.text
    return ""


# ═══════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Research capture: Parallel Search MCP → MemPalace drawer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--mcp-url", default=DEFAULT_MEMPALACE_URL)
    p.add_argument("--mcp-token", default=os.environ.get("MEMPALACE_MCP_HTTP_TOKEN", ""))
    p.add_argument("--wing", default="research")
    p.add_argument("--room", default="captured_web")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument(
        "--auto",
        action="store_true",
        help="Skip interactive pick + confirm (first result, auto-yes)",
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-results", type=int, default=5)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = (lambda *a, **k: None) if args.quiet else print
    stream = sys.stderr

    out("Research Capture — Parallel Search MCP → MemPalace", file=stream)

    # 1. query
    if sys.stdin.isatty():
        query = input("\nResearch query: ").strip()
    else:
        query = sys.stdin.readline().strip()
    if not query:
        out("No query supplied.", file=stream)
        return 1

    # 2. search
    out(f"Searching: {query}", file=stream)
    hits = _run(parallel_search(query, max_results=args.max_results))
    if not hits:
        out("No search results returned.", file=stream)
        return 1

    for i, h in enumerate(hits, 1):
        out(f"  {i}. {h.title}\n     {h.url}", file=stream)

    # 3. pick
    if args.auto:
        chosen = hits[0]
    elif sys.stdin.isatty():
        pick = input(f"Fetch result # [1-{len(hits)}] (0 to skip): ").strip()
        if pick == "0":
            out("Skipped.", file=stream)
            return 0
        idx = int(pick) if pick.isdigit() else 1
        chosen = hits[idx - 1] if 1 <= idx <= len(hits) else None
        if chosen is None:
            out("Invalid pick.", file=stream)
            return 1
    else:
        pick = sys.stdin.readline().strip() or "1"
        idx = int(pick) if pick.isdigit() else 1
        chosen = hits[idx - 1] if 1 <= idx <= len(hits) else hits[0]

    # 4. fetch
    out(f"Fetching: {chosen.url}", file=stream)
    raw = _run(parallel_fetch(chosen.url))
    if not raw:
        out("Page returned no extractable text.", file=stream)
        return 1

    # 5. preview
    if not args.json:
        out("── Preview (extracted page text, not original HTML) ──", file=stream)
        out(preview(raw), file=stream)
        out(f"Wing: {args.wing}   Room: {args.room}   Length: {len(raw)} chars", file=stream)

    # 6. confirm gate
    if not args.auto and not confirm("Save this to MemPalace? [y/N] "):
        out("Cancelled — nothing was saved.", file=stream)
        return 0

    # 7. file
    item = build_captured(args.wing, args.room, raw, chosen.url)
    out("Filing drawer…", file=stream)
    result = file_captured(args.mcp_url, args.mcp_token, item, dry_run=args.dry_run)

    if isinstance(result, dict) and result.get("success"):
        out(f"Filed as drawer_id: {result.get('drawer_id', '?')}", file=stream)
        if args.json:
            print(json.dumps(result, indent=2))
        return 0
    out(f"Server error: {result}", file=stream)
    return 1


__all__ = [
    "CapturedItem",
    "SearchResult",
    "PARALLEL_SEARCH_MCP_URL",
    "DEFAULT_MEMPALACE_URL",
    "PROVENANCE_SOURCE",
    "ADDED_BY_LABEL",
    "build_captured",
    "preview",
    "confirm",
    "file_captured",
    "parallel_search",
    "parallel_fetch",
    "parse_args",
    "main",
]

if __name__ == "__main__":
    sys.exit(main())
