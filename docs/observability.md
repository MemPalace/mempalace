# Observability — MemPalace OpenTelemetry integration

MemPalace ships an opt-in OpenTelemetry instrumentation for the MCP
server. It emits all three pillars — **traces**, **metrics**, and
**logs** — and accepts W3C tracecontext propagation from MCP clients
so an agent's call and MemPalace's handling appear in one end-to-end
trace. Mapped to the working draft `memory-semconv v0.1.0`
conventions. **No telemetry is produced by default** — it activates
only when both of these are true:

1. The `[observability]` extra is installed:
   ```
   pip install 'mempalace[observability]'
   ```
2. `OTEL_EXPORTER_OTLP_ENDPOINT` is set in the process environment.

When either is missing, every telemetry call is a hard no-op: no SDK
imports, no resource allocation, no exporter threads. The default
install path is unaffected.

## What MemPalace emits

### Spans

One span is emitted per MCP tool call. The span name is
`memory.<operation>` where `<operation>` is one of:

| Operation       | Triggered by                                                              |
|-----------------|--------------------------------------------------------------------------|
| `memory.read`   | `mempalace_search`, `mempalace_kg_query`, `mempalace_status`, `mempalace_list_*`, `mempalace_get_*`, `mempalace_traverse`, `mempalace_kg_timeline`, `mempalace_kg_stats`, `mempalace_graph_stats`, `mempalace_check_duplicate`, `mempalace_diary_read`, `mempalace_get_taxonomy`, `mempalace_get_aaak_spec`, `mempalace_hook_settings`, `mempalace_memories_filed_away`, `mempalace_find_tunnels`, `mempalace_follow_tunnels` |
| `memory.write`  | `mempalace_add_drawer`, `mempalace_update_drawer`, `mempalace_delete_drawer`, `mempalace_kg_add`, `mempalace_create_tunnel`, `mempalace_delete_tunnel`, `mempalace_diary_write`, `mempalace_sync`, `mempalace_reconnect` |
| `memory.invalidate` | `mempalace_kg_invalidate` |

Span attributes:

| Attribute          | Type   | Notes                                                |
|--------------------|--------|------------------------------------------------------|
| `memory.operation` | string | `read` \| `write` \| `invalidate`                    |
| `memory.tool`      | string | The MCP tool name (e.g. `mempalace_search`)          |

> **PII discipline.** Argument values (queries, drawer content, KG
> subjects/predicates/objects) are **never** attached to spans. The
> wrapper records the operation and the tool name only.

### Metrics

| Metric                         | Type      | Unit     | When recorded                              |
|--------------------------------|-----------|----------|--------------------------------------------|
| `memory_recall_results_count`  | histogram | drawers  | Every `search_memories` call                |
| `memory_recall_top_similarity` | histogram | 0..1     | Every non-empty `search_memories` result    |

### Logs

Standard Python `logging` records emitted by the `mempalace` logger
tree (and its children) are bridged to OTel logs via `LoggingHandler`
and exported over OTLP. Each record carries the active span's
`trace_id` + `span_id`, so log lines correlate with the span tree in
any OTLP-compatible backend (filter by `trace_id` to pull every log
line tied to a single tool dispatch).

The dispatch wrapper always emits one structured log record per call:

    memory.dispatch tool=<tool_name> operation=<read|write|invalidate>

Handlers can add their own `logger.info(...)` calls and they will land
on the same span. **PII discipline still applies**: never log raw
drawer content, search queries, or KG subjects/predicates/objects.

> The OTel `LoggingHandler` writes only to OTLP; the stdio protection
> in `mcp_server.py` (stdout → stderr fd-level redirect) is unaffected.
> Handlers are attached to the `mempalace` logger, not the root, so
> third-party libraries (chromadb, posthog) keep their existing
> stderr-only behavior.

### Trace context propagation (end-to-end agent → MCP → MemPalace)

MCP clients that already own an active OTel trace SHOULD inject W3C
tracecontext headers into the `_meta` field of every `tools/call`
request:

```jsonc
{
  "method": "tools/call",
  "params": {
    "name": "mempalace_search",
    "arguments": { /* … */ },
    "_meta": {
      "traceparent": "00-<trace-id>-<parent-span-id>-01",
      "tracestate":  "vendor=opaque-value"           /* optional */
    }
  }
}
```

MemPalace extracts those headers via the standard
`TraceContextTextMapPropagator`, builds an OTel `Context`, and starts
the `memory.<op>` span as a child of the remote parent. The resulting
trace contains both the agent's outbound MCP call and MemPalace's
internal handling under one `trace_id`.

When `_meta` is missing or malformed, the span still starts — just as
a new trace. The call is observable either way; only end-to-end
correlation is lost.

**Reference client snippet (Python)** — for SDK authors wiring their
own MCP client:

```python
from opentelemetry import trace
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

tracer = trace.get_tracer("my-agent")
propagator = TraceContextTextMapPropagator()

with tracer.start_as_current_span("agent.mcp.tools_call") as span:
    span.set_attribute("mcp.tool", "mempalace_search")
    headers: dict[str, str] = {}
    propagator.inject(carrier=headers)            # fills traceparent
    rpc = {
        "jsonrpc": "2.0",
        "id": next_id(),
        "method": "tools/call",
        "params": {
            "name": "mempalace_search",
            "arguments": {"query": "..."},
            "_meta": headers,
        },
    }
    send_to_mcp_server(rpc)
```

### Resource attributes

| Attribute                  | Value          |
|----------------------------|----------------|
| `service.name`             | `mempalace-mcp` (override via `OTEL_SERVICE_NAME`) |
| `service.version`          | the running MemPalace version |
| `memory.sut.name`          | `mempalace`    |
| `memory.sut.architecture`  | `mcp`          |

`memory.sut.*` come from `memory-semconv v0.1.0` and let backends
slice memory telemetry by the System Under Test without having to
infer it from the service name.

## Enabling it

The simplest local setup ships traces and metrics to an OTLP collector
listening on `localhost:4318` (HTTP):

```bash
pip install 'mempalace[observability]'

export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
export OTEL_SERVICE_NAME=mempalace-mcp      # optional, defaults to mempalace-mcp

mempalace-mcp --palace ~/.mempalace
```

To send traces and metrics to different endpoints, use the
signal-specific OTel env vars:

```bash
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=https://otlp.example.com/v1/traces
export OTEL_EXPORTER_OTLP_METRICS_ENDPOINT=https://otlp.example.com/v1/metrics
export OTEL_EXPORTER_OTLP_HEADERS="authorization=Api-Token dt0c01.XXX"
```

These are read by the underlying OpenTelemetry SDK; MemPalace does
not interpret them.

## Verifying in Dynatrace

The reference verification DQL queries live in
`docs/verification/mempalace-baseline.dql`. They check that:

1. The expected `memory.*` span names appear.
2. Span attributes include `memory.operation` + `memory.tool`.
3. Resource attributes include `memory.sut.name=mempalace`.
4. The recall metrics surface as histograms.
5. Logs land with `trace_id` populated and join cleanly back to spans.
6. End-to-end traces from a client carry both the agent's parent
   span and MemPalace's child span under a single `trace_id`.

Run them in Dynatrace Notebook after pointing a MemPalace MCP server
at your tenant for a few minutes of typical traffic.

## Cardinality notes

`memory.tool` is bounded (30 tools today, growing slowly). It is
safe as a metric dimension. The recall metrics deliberately do **not**
carry per-call labels (no `wing`, no `room`, no `query`) because:

- Wing/room values are user-defined and unbounded.
- Query text is PII and would dominate the dimension space.

If you need per-wing recall metrics, add the dimension downstream
(OTel Collector → metricstransform / spanmetrics) after a sampling
or allow-list stage you control.

## Compatibility

* OpenTelemetry Python SDK ≥ 1.25 (stable APIs only).
* Python 3.9+ — same floor as MemPalace itself.
* Backends: Dynatrace, Grafana Tempo + Mimir, Honeycomb, Jaeger +
  Prometheus, or any OTLP-compatible collector. Nothing is
  vendor-specific.
