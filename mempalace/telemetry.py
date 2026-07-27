"""OpenTelemetry integration for MemPalace.

Opt-in. Disabled unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set in the
environment. When disabled, every public function in this module is a
no-op — no SDK imports, no resource allocation, no exporter spin-up — so
the default install path is unaffected.

When enabled, MemPalace emits all three pillars of telemetry — traces,
metrics, and logs — mapped to the ``memory-semconv v0.1.0`` working
draft:

    memory.read         — non-mutating queries (search, kg_query, …)
    memory.write        — mutating writes (add_drawer, kg_add, …)
    memory.invalidate   — explicit fact retraction (kg_invalidate)
    memory.embed        — embedding-vector computation (search-time)
    memory.consolidate  — background dedup/sweep (not emitted from MCP)

Trace context propagation
-------------------------
The MCP dispatch wrapper inspects each incoming ``tools/call`` request
for a ``_meta`` object containing W3C tracecontext headers
(``traceparent``, optionally ``tracestate``). When present, the
``memory.<op>`` span is started as a child of that remote parent,
producing a single end-to-end trace that spans the agent's MCP client
and MemPalace's internal handling.

Logs
----
Standard Python ``logging`` records emitted while a ``memory_operation``
span is active are captured by an OTel ``LoggingHandler`` and exported
via OTLP. Each log record carries the active span's trace_id/span_id so
log lines correlate with the span tree in any OTLP-compatible backend.

Resource attributes always set on the provider when enabled:

    memory.sut.name = "mempalace"
    memory.sut.architecture = "mcp"
    service.name, service.version

Install hook:

    pip install mempalace[observability]

then run ``mempalace-mcp`` with ``OTEL_EXPORTER_OTLP_ENDPOINT`` set.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .version import __version__

logger = logging.getLogger(__name__)


# --- Operation taxonomy ------------------------------------------------
# Map every MCP tool name to a memory-semconv operation. Tools whose
# behavior is purely a read of palace state map to ``read``; tools that
# mutate state map to ``write``; ``kg_invalidate`` is the sole
# ``invalidate``. Adding a new MCP tool? Register its kind here.

_TOOL_OPERATION: dict[str, str] = {
    # ---- read (non-mutating) -------------------------------------------
    "mempalace_status": "read",
    "mempalace_list_wings": "read",
    "mempalace_list_rooms": "read",
    "mempalace_get_taxonomy": "read",
    "mempalace_get_aaak_spec": "read",
    "mempalace_kg_query": "read",
    "mempalace_kg_timeline": "read",
    "mempalace_kg_stats": "read",
    "mempalace_traverse": "read",
    "mempalace_find_tunnels": "read",
    "mempalace_graph_stats": "read",
    "mempalace_list_tunnels": "read",
    "mempalace_follow_tunnels": "read",
    "mempalace_search": "read",
    "mempalace_check_duplicate": "read",
    "mempalace_get_drawer": "read",
    "mempalace_list_drawers": "read",
    "mempalace_diary_read": "read",
    "mempalace_hook_settings": "read",
    "mempalace_memories_filed_away": "read",
    "mempalace_list_hallways": "read",
    # ---- write (mutating) ----------------------------------------------
    "mempalace_kg_add": "write",
    "mempalace_create_tunnel": "write",
    "mempalace_delete_tunnel": "write",
    "mempalace_add_drawer": "write",
    "mempalace_delete_drawer": "write",
    "mempalace_update_drawer": "write",
    "mempalace_diary_write": "write",
    "mempalace_sync": "write",
    "mempalace_reconnect": "write",
    # checkpoint files drawers + a diary entry; delete_by_source and
    # delete_hallway remove palace state; mine ingests a directory — all
    # mutate, so they are ``write`` (added after the 2026 fork sync
    # introduced these tools; ISI-1920).
    "mempalace_checkpoint": "write",
    "mempalace_delete_by_source": "write",
    "mempalace_delete_hallway": "write",
    "mempalace_mine": "write",
    # ---- invalidate ----------------------------------------------------
    "mempalace_kg_invalidate": "invalidate",
    # kg_supersede atomically retires a fact and writes its successor at a
    # shared boundary — the retraction half makes it an ``invalidate`` in
    # the memory-semconv taxonomy, matching kg_invalidate.
    "mempalace_kg_supersede": "invalidate",
}


def operation_for_tool(tool_name: str) -> str:
    """Return the memory-semconv operation kind for ``tool_name``.

    Unknown tools default to ``read`` so the call is still observable;
    the dispatch wrapper logs at debug level so callers notice and
    backfill the map.
    """
    op = _TOOL_OPERATION.get(tool_name)
    if op is None:
        logger.debug(
            "telemetry: no operation mapping for %s — defaulting to 'read'", tool_name
        )
        return "read"
    return op


# --- Lazy state --------------------------------------------------------
# Initialization sets a single module-level flag plus tracer/meter
# handles. Anything else stays None so the no-op path keeps zero
# overhead.

_ENABLED: bool = False
_TRACER: Any = None
_METER: Any = None
_RECALL_RESULTS_HISTOGRAM: Any = None
_RECALL_TOP_SIMILARITY_GAUGE: Any = None
# Logs pillar — the LoggingHandler is attached to the ``mempalace``
# logger tree so any ``logger.info(...)`` emitted by MemPalace code
# inside an active span produces an OTLP log record carrying the
# trace_id + span_id of that span. ``_LOG_PROVIDER`` is retained so
# ``init_telemetry`` is idempotent and so shutdown can flush it.
_LOG_PROVIDER: Any = None
_LOG_HANDLER: Any = None
# W3C tracecontext propagator instance — re-used for every dispatch
# extraction. None when telemetry is disabled.
_PROPAGATOR: Any = None


def is_enabled() -> bool:
    """Return whether telemetry is initialized and active."""
    return _ENABLED


def init_telemetry() -> bool:
    """Initialize OTel providers if the env opts in.

    Idempotent. Returns ``True`` when telemetry was wired up,
    ``False`` when disabled (env unset or SDK missing).

    Activation rules:
      * ``OTEL_EXPORTER_OTLP_ENDPOINT`` must be set; if unset, this is
        a hard no-op even if the SDK is installed.
      * ``opentelemetry-api`` + ``opentelemetry-sdk`` +
        ``opentelemetry-exporter-otlp`` must import. Missing imports
        degrade to a warning + no-op, never a crash.
    """
    global _ENABLED, _TRACER, _METER
    global _RECALL_RESULTS_HISTOGRAM, _RECALL_TOP_SIMILARITY_GAUGE
    global _LOG_PROVIDER, _LOG_HANDLER, _PROPAGATOR

    if _ENABLED:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace.propagation.tracecontext import (
            TraceContextTextMapPropagator,
        )
    except ImportError as exc:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OpenTelemetry SDK is "
            "not installed (%s). Install with `pip install mempalace[observability]`. "
            "Telemetry disabled.",
            exc,
        )
        return False

    resource = Resource.create(
        {
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "mempalace-mcp"),
            "service.version": __version__,
            # memory-semconv v0.1.0 resource contract
            "memory.sut.name": "mempalace",
            "memory.sut.architecture": "mcp",
        }
    )

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(meter_provider)

    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(log_provider)
    # Bridge stdlib ``logging`` → OTel logs. Attaching to specific
    # MemPalace-owned logger names (not root) keeps the MCP stdio
    # protection intact: chromadb / posthog stay on stderr, only
    # MemPalace's own loggers fan out to OTLP.
    #
    # MemPalace doesn't use a single root namespace — historically it
    # uses ``mempalace_mcp`` (underscore) for runtime modules and the
    # ``mempalace.*`` (dotted, via ``__name__``) tree for newer ones.
    # We attach to BOTH so dispatch / search / miner logs all land,
    # plus the ``mempalace_graph`` / ``mempalace_hallways`` /
    # ``mempalace_format_miner`` siblings.
    handler = LoggingHandler(level=logging.INFO, logger_provider=log_provider)
    _MEMPALACE_LOGGER_NAMES = (
        "mempalace",
        "mempalace_mcp",
        "mempalace_graph",
        "mempalace_hallways",
        "mempalace_format_miner",
    )
    for name in _MEMPALACE_LOGGER_NAMES:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        # Ensure INFO records propagate to the OTel handler even if the
        # app hasn't configured a level. Existing stderr handlers keep
        # their own levels — this only opens the gate for our handler.
        if lg.level == logging.NOTSET or lg.level > logging.INFO:
            lg.setLevel(logging.INFO)
    _LOG_PROVIDER = log_provider
    _LOG_HANDLER = handler

    _TRACER = trace.get_tracer("mempalace", __version__)
    _METER = metrics.get_meter("mempalace", __version__)
    _RECALL_RESULTS_HISTOGRAM = _METER.create_histogram(
        name="memory_recall_results_count",
        description="Number of drawers returned by a search/recall call",
        unit="{drawer}",
    )
    _RECALL_TOP_SIMILARITY_GAUGE = _METER.create_histogram(
        name="memory_recall_top_similarity",
        description="Cosine similarity of the top-1 search result (0=unrelated, 1=identical)",
        unit="1",
    )

    _PROPAGATOR = TraceContextTextMapPropagator()

    _ENABLED = True
    logger.info("MemPalace telemetry initialized → %s", endpoint)
    return True


# --- Trace context propagation -----------------------------------------
# Memory clients (an agent, a sub-agent, an orchestrator) that already
# own an active trace SHOULD inject W3C tracecontext headers into the
# MCP ``params._meta`` field of every ``tools/call`` request:
#
#     {
#       "method": "tools/call",
#       "params": {
#         "name": "mempalace_search",
#         "arguments": {...},
#         "_meta": {
#           "traceparent": "00-<trace-id>-<parent-span-id>-01",
#           "tracestate":  "vendor=value"            (optional)
#         }
#       }
#     }
#
# MemPalace extracts that header set, builds an OTel Context from it,
# and starts the ``memory.<op>`` span as a child of the remote parent
# so the entire call chain (agent → MCP → MemPalace) lands in one
# trace. When ``_meta`` is missing or malformed, the span starts a
# new trace — the call is still observable, just not end-to-end
# correlated.

# Sentinel for "no parent context available." Kept distinct from
# ``None`` so callers can pass ``None`` explicitly without us trying
# to interpret it as a propagator carrier.
_NO_PARENT_CONTEXT = object()


def extract_trace_context(meta: Optional[Any]) -> Any:
    """Build an OTel ``Context`` from an MCP ``_meta`` header map.

    Accepts the raw ``params._meta`` value (must be a dict for a hit;
    anything else is treated as absent). Returns:

      * an OTel ``Context`` when telemetry is on AND ``meta`` contains
        a parseable ``traceparent``
      * ``_NO_PARENT_CONTEXT`` otherwise (signalling "start a new
        trace") — never raises.

    The function is a no-op when telemetry is disabled.
    """
    if not _ENABLED or _PROPAGATOR is None:
        return _NO_PARENT_CONTEXT
    if not isinstance(meta, dict) or "traceparent" not in meta:
        return _NO_PARENT_CONTEXT
    try:
        return _PROPAGATOR.extract(carrier=meta)
    except Exception:
        # Malformed header should never crash dispatch — fall back to a
        # new trace and log at debug.
        logger.debug("telemetry: failed to extract trace context", exc_info=True)
        return _NO_PARENT_CONTEXT


# --- Span emission -----------------------------------------------------


@contextmanager
def memory_operation(
    tool_name: str,
    operation: Optional[str] = None,
    parent_context: Any = _NO_PARENT_CONTEXT,
    **attributes: Any,
) -> Iterator[Any]:
    """Context manager that emits a ``memory.<operation>`` span.

    No-op when telemetry is disabled. When enabled, sets:

      * span name = ``memory.<operation>``
      * ``memory.operation`` = operation
      * ``memory.tool`` = ``tool_name``
      * any ``attributes`` passed in (must already be PII-clean —
        callers MUST NOT pass raw user content here)

    ``parent_context`` accepts the value returned by
    ``extract_trace_context``. When it points at a remote parent,
    the new span is started under that parent so the agent's call
    and MemPalace's handling share one trace_id.

    Yields the underlying span object (real or no-op) so the caller can
    add metric-derived attributes after the call returns (e.g.
    result counts, similarity scores).
    """
    if not _ENABLED or _TRACER is None:
        yield _NOOP_SPAN
        return

    kind = operation or operation_for_tool(tool_name)
    span_name = f"memory.{kind}"

    span_attrs = {
        "memory.operation": kind,
        "memory.tool": tool_name,
    }
    span_attrs.update({k: v for k, v in attributes.items() if v is not None})

    ctx_kwargs: dict[str, Any] = {}
    if parent_context is not _NO_PARENT_CONTEXT and parent_context is not None:
        ctx_kwargs["context"] = parent_context

    with _TRACER.start_as_current_span(
        span_name, attributes=span_attrs, **ctx_kwargs
    ) as span:
        try:
            yield span
        except Exception as exc:
            try:
                from opentelemetry.trace import Status, StatusCode

                span.set_status(Status(StatusCode.ERROR, str(exc)))
                span.record_exception(exc)
            except Exception:
                pass
            raise


# --- Metrics -----------------------------------------------------------


def record_recall(results_count: int, top_similarity: Optional[float] = None) -> None:
    """Record recall metrics for a search call.

    No-op when telemetry is disabled. ``results_count`` is the number of
    drawers actually returned to the caller (post-rerank, post-trim).
    ``top_similarity`` is the cosine similarity (0..1) of the highest-
    ranked hit, or ``None`` when the result set is empty.
    """
    if not _ENABLED:
        return
    try:
        if _RECALL_RESULTS_HISTOGRAM is not None:
            _RECALL_RESULTS_HISTOGRAM.record(int(results_count))
        if top_similarity is not None and _RECALL_TOP_SIMILARITY_GAUGE is not None:
            _RECALL_TOP_SIMILARITY_GAUGE.record(float(top_similarity))
    except Exception:
        # Metrics must never break the search path.
        logger.debug("telemetry: failed to record recall metrics", exc_info=True)


# --- No-op span sentinel ------------------------------------------------
# Returned by ``memory_operation`` when telemetry is disabled. Mirrors
# the bits of the OTel Span API the dispatch code touches: setting
# attributes after the fact and recording exceptions.


class _NoopSpan:
    def set_attribute(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_attributes(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_exception(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def set_status(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_NOOP_SPAN = _NoopSpan()
