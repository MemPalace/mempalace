"""OpenTelemetry integration for MemPalace.

Opt-in. Disabled unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set in the
environment. When disabled, every public function in this module is a
no-op — no SDK imports, no resource allocation, no exporter spin-up — so
the default install path is unaffected.

When enabled, emits the ``memory.*`` span set described in the
``memory-semconv v0.1.0`` working draft:

    memory.read         — non-mutating queries (search, kg_query, …)
    memory.write        — mutating writes (add_drawer, kg_add, …)
    memory.invalidate   — explicit fact retraction (kg_invalidate)
    memory.embed        — embedding-vector computation (search-time)
    memory.consolidate  — background dedup/sweep (not emitted from MCP)

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
    # ---- invalidate ----------------------------------------------------
    "mempalace_kg_invalidate": "invalidate",
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

    if _ENABLED:
        return True

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        return False

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
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

    _ENABLED = True
    logger.info("MemPalace telemetry initialized → %s", endpoint)
    return True


# --- Span emission -----------------------------------------------------


@contextmanager
def memory_operation(
    tool_name: str,
    operation: Optional[str] = None,
    **attributes: Any,
) -> Iterator[Any]:
    """Context manager that emits a ``memory.<operation>`` span.

    No-op when telemetry is disabled. When enabled, sets:

      * span name = ``memory.<operation>``
      * ``memory.operation`` = operation
      * ``memory.tool`` = ``tool_name``
      * any ``attributes`` passed in (must already be PII-clean —
        callers MUST NOT pass raw user content here)

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

    with _TRACER.start_as_current_span(span_name, attributes=span_attrs) as span:
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
