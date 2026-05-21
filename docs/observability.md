# Observability — MemPalace OpenTelemetry integration

MemPalace ships an opt-in OpenTelemetry instrumentation for the MCP
server. It emits **traces** for every tool dispatch and **metrics** for
recall quality, mapped to the working draft `memory-semconv v0.1.0`
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
