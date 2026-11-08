"""Storage dispatch observations carry only reviewed primitive facts."""

import json
import sys

import pytest

# Independent contract oracle: never import the serializer's key set.
ROOT_KEYS = set(
    """
schema_version event worker_generation record_seq call_id timestamp_utc elapsed_us
build_revision build_version tool_name dispatch_mode startup_phase rss_before_bytes
rss_after_bytes rss_anon_before_bytes rss_anon_after_bytes rss_file_before_bytes
rss_file_after_bytes rss_source rss_status raw_metadata_cache_hit taxonomy_cache_hit
metadata_fallback fallback_kind model_backend metadata_rows aggregate_pair_count
ef_instance_count chroma_client_count kg_handle_count logstream_handle_count
restricted_pickle_load_count collection_open_count model_session_create_count
active_call_count lost_record_count lifecycle_reason outcome
""".split()
)


@pytest.fixture
def serving(monkeypatch, tmp_path):
    from mempalace import mcp_server as server

    original = getattr(server, "_worker_observer_factory", None)
    instances = []

    def factory(directory):
        observer = original(directory, synchronous=True)
        instances.append(observer)
        return observer

    monkeypatch.setattr(server, "_worker_observer_factory", factory, raising=False)
    monkeypatch.setattr(server, "_worker_observer", None, raising=False)
    monkeypatch.setattr(server, "_hub_proxy_target", lambda: None)
    monkeypatch.setattr(server._config, "_config_dir", tmp_path)
    monkeypatch.delenv("MEMPALACE_WORKER_MEMORY_OBSERVE", raising=False)
    monkeypatch.setattr(server, "_mcp_tool_preflight_refusal", lambda *a, **k: None)
    monkeypatch.setitem(
        server.TOOLS,
        "mempalace_status",
        {
            "handler": lambda **kwargs: {"private": "RESULT-CANARY"},
            "input_schema": {"type": "object", "properties": {}},
        },
    )

    def invoke(name="mempalace_status", arguments=None):
        return server._dispatch_stdio_request(
            {
                "jsonrpc": "2.0",
                "id": "REQUEST-ID-CANARY",
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    def records():
        path = tmp_path / "diagnostics" / "worker-memory.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    yield server, invoke, records, instances
    for observer in instances:
        observer.close()


def test_default_paired_records_use_generated_ids(serving):
    _, invoke, records, _ = serving
    invoke()
    calls = [r for r in records() if r["event"].startswith("call_")]
    assert calls, "serving path must emit observation records"
    assert [r["event"] for r in calls] == ["call_begin", "call_end"]
    assert calls[0]["call_id"] == calls[1]["call_id"] == 1
    assert calls[0]["worker_generation"] == calls[1]["worker_generation"]
    if sys.platform.startswith("linux"):
        assert calls[0]["rss_before_bytes"] > 0
        assert calls[1]["rss_after_bytes"] > 0
    else:
        assert calls[0]["rss_before_bytes"] is None
        assert calls[1]["rss_after_bytes"] is None
        assert calls[0]["rss_status"] == calls[1]["rss_status"] == "unsupported"
    assert calls[1]["outcome"] == "returned"


def test_cache_flags_are_observed_not_defaulted(serving, monkeypatch):
    server, invoke, records, _ = serving
    col = type("Collection", (), {"get_all_metadata": lambda self, **kw: [{}]})()
    monkeypatch.setattr(server, "_metadata_cache", None)
    monkeypatch.setattr(server, "_taxonomy_cache", None)
    monkeypatch.setattr(server, "_is_chroma_backend", lambda: False)
    monkeypatch.setattr(server, "_selected_backend_name", lambda: "unsupported")

    def handler(**kwargs):
        server._get_cached_metadata(col)
        server._sqlite_taxonomy()
        return {}

    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", handler)
    invoke()
    invoke()
    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", lambda **kw: {})
    invoke()
    ends = [r for r in records() if r["event"] == "call_end"]
    assert ends, "observed cache branches must emit records"
    assert [r["raw_metadata_cache_hit"] for r in ends] == [False, True, None]
    assert [r["taxonomy_cache_hit"] for r in ends] == [False, False, None]
    assert ends[0]["fallback_kind"] == "full_metadata"
    assert ends[2]["model_backend"] == {"model": None, "backend": None}
    assert ends[2]["model_session_create_count"] is None


def test_private_values_never_enter_observation(serving, monkeypatch):
    server, invoke, records, _ = serving
    monkeypatch.setenv("MEMPALACE_FAKE_SECRET", "CONFIG-SECRET-CANARY")
    invoke(arguments={"private": "ARGUMENT-CONTENT-CANARY"})
    invoke(name="UNKNOWN-TOOL-CANARY")

    def raises(**kw):
        raise RuntimeError("EXCEPTION-CANARY")

    monkeypatch.setitem(server.TOOLS["mempalace_status"], "handler", raises)
    invoke()
    rows = records()
    assert rows, "absence of records is not privacy proof"
    for row in rows:
        assert set(row) == ROOT_KEYS
        assert set(row["model_backend"]) == {"model", "backend"}
    assert "CANARY" not in json.dumps(rows)
    assert any(r["tool_name"] == "unknown" for r in rows)
