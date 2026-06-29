"""Summarise MemPalace MCP audit telemetry."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path


def default_audit_path() -> Path:
    return Path(
        os.environ.get(
            "MEMPALACE_AUDIT_FILE",
            os.path.expanduser("~/.mempalace/service_logs/mcp_audit.jsonl"),
        )
    )


def _parse_ts(value: str):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def iter_events(path: Path, *, since: datetime | None = None):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(event.get("timestamp"))
            if since and ts and ts < since:
                continue
            yield event


def _percentile(values: list[float], pct: float):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return ordered[index]


def summarize_events(events: list[dict]) -> dict:
    tools = Counter()
    methods = Counter()
    searches = []
    empty_searches = []
    writes = []
    errors = []
    latencies = []
    clients = Counter()
    matched_via = Counter()
    top_queries = Counter()

    write_tools = {
        "mempalace_add_drawer",
        "mempalace_checkpoint",
        "mempalace_create_tunnel",
        "mempalace_delete_by_source",
        "mempalace_delete_drawer",
        "mempalace_delete_tunnel",
        "mempalace_diary_write",
        "mempalace_kg_add",
        "mempalace_kg_invalidate",
        "mempalace_update_drawer",
    }

    for event in events:
        method = event.get("method") or "(parse-error)"
        methods[method] += 1
        if event.get("remote"):
            clients[event["remote"]] += 1
        if isinstance(event.get("latency_ms"), (int, float)):
            latencies.append(float(event["latency_ms"]))
        if not event.get("ok", False):
            errors.append(event)

        tool = event.get("tool")
        if not tool:
            continue
        tools[tool] += 1
        if tool in write_tools:
            writes.append(event)
        if tool == "mempalace_search":
            searches.append(event)
            result = event.get("result") or {}
            query = result.get("query") or (event.get("tool_args") or {}).get("query")
            if query:
                top_queries[str(query)] += 1
            if (result.get("result_count") or 0) == 0:
                empty_searches.append(event)
            for hit in result.get("top_results") or []:
                if hit.get("matched_via"):
                    matched_via[hit["matched_via"]] += 1

    return {
        "events": len(events),
        "methods": methods,
        "tools": tools,
        "search_count": len(searches),
        "empty_search_count": len(empty_searches),
        "write_count": len(writes),
        "error_count": len(errors),
        "clients": clients,
        "matched_via": matched_via,
        "top_queries": top_queries,
        "latency_p50": _percentile(latencies, 0.50),
        "latency_p95": _percentile(latencies, 0.95),
        "recent_searches": searches[-10:],
        "recent_errors": errors[-10:],
    }


def print_summary(
    path: Path | None = None,
    *,
    hours: float = 24,
    limit: int = 10,
    show_text: bool = False,
    text_chars: int = 500,
) -> None:
    path = path or default_audit_path()
    since = datetime.now() - timedelta(hours=hours) if hours else None
    events = list(iter_events(path, since=since) or [])
    summary = summarize_events(events)

    print(f"Audit file: {path}")
    print(f"Window: last {hours:g} hours" if hours else "Window: all events")
    print(f"Events: {summary['events']}")
    print(f"Errors: {summary['error_count']}")
    if summary["latency_p50"] is not None:
        print(
            "Latency: "
            f"p50={summary['latency_p50']:.1f}ms "
            f"p95={summary['latency_p95']:.1f}ms"
        )

    print("\nMethods")
    for name, count in summary["methods"].most_common(limit):
        print(f"  {count:5d}  {name}")

    print("\nTools")
    for name, count in summary["tools"].most_common(limit):
        print(f"  {count:5d}  {name}")

    print("\nSearches")
    search_count = summary["search_count"]
    empty_count = summary["empty_search_count"]
    empty_rate = (empty_count / search_count * 100) if search_count else 0.0
    print(f"  total={search_count} empty={empty_count} empty_rate={empty_rate:.1f}%")
    if summary["matched_via"]:
        print("  matched_via:")
        for name, count in summary["matched_via"].most_common(limit):
            print(f"    {count:5d}  {name}")

    if summary["top_queries"]:
        print("\nTop Queries")
        for query, count in summary["top_queries"].most_common(limit):
            print(f"  {count:5d}  {query}")

    print("\nRecent Searches")
    for event in summary["recent_searches"][-limit:]:
        result = event.get("result") or {}
        print(
            f"  {event.get('timestamp')} "
            f"{result.get('result_count', '?')} hits "
            f"{result.get('query')}"
        )
        for hit in (result.get("top_results") or [])[:3]:
            print(
                "         "
                f"#{hit.get('rank')} {hit.get('wing')}/{hit.get('room')} "
                f"sim={hit.get('similarity')} via={hit.get('matched_via')} "
                f"src={hit.get('source_file')}"
            )
            response_text = hit.get("response_text") or {}
            text = response_text.get("text")
            if show_text and text:
                snippet = str(text).replace("\r", " ").replace("\n", " ")
                if text_chars > 0 and len(snippet) > text_chars:
                    snippet = snippet[:text_chars] + "..."
                suffix = " truncated" if response_text.get("truncated") else ""
                print(f"             response{suffix}: {snippet}")

    if summary["recent_errors"]:
        print("\nRecent Errors")
        for event in summary["recent_errors"][-limit:]:
            error = event.get("error") or {}
            print(
                f"  {event.get('timestamp')} "
                f"{event.get('method')} {event.get('tool') or ''} "
                f"{error.get('code')}: {error.get('message')}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarise MemPalace MCP audit telemetry")
    parser.add_argument("--file", default=None, help="Audit JSONL file to read")
    parser.add_argument("--hours", type=float, default=24, help="Hours to include; 0 = all")
    parser.add_argument("--limit", type=int, default=10, help="Rows to print per section")
    parser.add_argument("--show-text", action="store_true", help="Print stored search-response text snippets")
    parser.add_argument("--text-chars", type=int, default=500, help="Characters per response snippet")
    args = parser.parse_args(argv)
    print_summary(
        Path(args.file) if args.file else None,
        hours=args.hours,
        limit=args.limit,
        show_text=args.show_text,
        text_chars=args.text_chars,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
