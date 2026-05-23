"""Unit tests for the opt-in OTel integration.

These cover the public contract of ``mempalace.telemetry`` when the
SDK extras are NOT installed / OTEL endpoint NOT set — i.e. the no-op
path that protects the default install. Live-SDK behavior is validated
end-to-end against a Dynatrace tenant during PR verification (see
docs/verification/mempalace-baseline.dql).
"""

from __future__ import annotations

import logging

from mempalace import telemetry


def test_default_disabled(monkeypatch):
    """With OTEL_EXPORTER_OTLP_ENDPOINT unset, init returns False."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    # Reset module-level state for a clean read.
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    monkeypatch.setattr(telemetry, "_TRACER", None)
    monkeypatch.setattr(telemetry, "_PROPAGATOR", None)

    assert telemetry.init_telemetry() is False
    assert telemetry.is_enabled() is False


def test_memory_operation_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    monkeypatch.setattr(telemetry, "_TRACER", None)

    with telemetry.memory_operation("mempalace_search") as span:
        # The yielded sentinel must implement the bits the dispatch path
        # touches: setting attributes, status, exceptions.
        span.set_attribute("memory.tool", "x")
        span.set_attributes({"k": "v"})
        span.set_status("ok")
        span.record_exception(RuntimeError("noop"))


def test_record_recall_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_ENABLED", False)

    # Both signatures must complete without side effects.
    telemetry.record_recall(0)
    telemetry.record_recall(7, top_similarity=0.62)


def test_extract_trace_context_returns_sentinel_when_disabled(monkeypatch):
    """When telemetry is off, context extraction yields the sentinel."""
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    monkeypatch.setattr(telemetry, "_PROPAGATOR", None)

    out = telemetry.extract_trace_context(
        {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    )
    assert out is telemetry._NO_PARENT_CONTEXT


def test_extract_trace_context_handles_missing_or_bad_meta(monkeypatch):
    """Absent/malformed _meta must never raise."""
    monkeypatch.setattr(telemetry, "_ENABLED", False)
    monkeypatch.setattr(telemetry, "_PROPAGATOR", None)

    for bad in [None, "", [], 42, {}, {"tracestate": "v=1"}]:
        assert telemetry.extract_trace_context(bad) is telemetry._NO_PARENT_CONTEXT


def test_operation_for_tool_known_and_unknown():
    """Known tools resolve; unknowns fall back to ``read``."""
    assert telemetry.operation_for_tool("mempalace_search") == "read"
    assert telemetry.operation_for_tool("mempalace_add_drawer") == "write"
    assert telemetry.operation_for_tool("mempalace_kg_invalidate") == "invalidate"
    assert telemetry.operation_for_tool("totally_made_up_tool") == "read"


def test_mempalace_loggers_unmodified_when_disabled(monkeypatch):
    """The LoggingHandler must NOT be attached to any MemPalace logger
    name when telemetry is off."""
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(telemetry, "_ENABLED", False)

    names = (
        "mempalace",
        "mempalace_mcp",
        "mempalace_graph",
        "mempalace_hallways",
        "mempalace_format_miner",
    )
    before = {n: list(logging.getLogger(n).handlers) for n in names}
    telemetry.init_telemetry()
    after = {n: list(logging.getLogger(n).handlers) for n in names}
    assert before == after, "telemetry must not touch logging when disabled"
