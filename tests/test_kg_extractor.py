import json
import os
import sqlite3

from mempalace.kg_extractor import (
    LLMCallResult,
    LLMConfig,
    benchmark_kg_models,
    extract_kg_triples,
)
from mempalace.kg_extractor import _call_llm, _parse_fact_candidates


class FakeCollection:
    def __init__(self, ids, documents, metadatas):
        self._ids = ids
        self._documents = documents
        self._metadatas = metadatas

    def count(self):
        return len(self._ids)

    def get(self, limit, offset, include):
        end = offset + limit
        return {
            "ids": self._ids[offset:end],
            "documents": self._documents[offset:end],
            "metadatas": self._metadatas[offset:end],
        }


def test_call_llm_applies_no_think_and_max_tokens(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": '{"facts":[]}'}}], "usage": {}}
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("mempalace.kg_extractor.urllib.request.urlopen", fake_urlopen)

    result = _call_llm(
        LLMConfig(
            endpoint="http://localhost:11434/v1",
            model="ornith:latest",
            no_think=True,
            max_output_tokens=128,
        ),
        source_file="opencode://session-1",
        drawer_id="drawer_123",
        wing="liquid_llm",
        room="technical",
        content="Liquid LLM connects to Ollama.",
        snippets=[(1, "Liquid LLM connects to Ollama.")],
        timeout_s=7,
    )

    assert result.parsed == {"facts": []}
    assert captured["body"]["max_tokens"] == 128
    assert captured["body"]["messages"][0]["content"].startswith("/no_think\n")
    assert captured["timeout"] == 7


def test_call_llm_invalid_json_error_includes_preview(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "I found one fact."}}], "usage": {}}
            ).encode("utf-8")

    monkeypatch.setattr(
        "mempalace.kg_extractor.urllib.request.urlopen", lambda req, timeout: FakeResponse()
    )

    result = _call_llm(
        LLMConfig(endpoint="http://localhost:11434/v1", model="bad-json-model"),
        source_file="opencode://session-1",
        drawer_id="drawer_123",
        wing="liquid_llm",
        room="technical",
        content="Liquid LLM connects to Ollama.",
        snippets=[(1, "Liquid LLM connects to Ollama.")],
        timeout_s=7,
    )

    assert result.parsed is None
    assert "invalid_model_json" in result.error
    assert "I found one fact." in result.error


def test_call_llm_ollama_provider_uses_native_think_false(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "message": {"content": '{"facts":[]}'},
                    "prompt_eval_count": 11,
                    "eval_count": 7,
                }
            ).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("mempalace.kg_extractor.urllib.request.urlopen", fake_urlopen)

    result = _call_llm(
        LLMConfig(
            endpoint="http://localhost:11434/v1",
            model="qwen3:1.7b",
            provider="ollama",
            no_think=True,
            max_output_tokens=128,
        ),
        source_file="opencode://session-1",
        drawer_id="drawer_123",
        wing="liquid_llm",
        room="technical",
        content="Liquid LLM connects to Ollama.",
        snippets=[(1, "Liquid LLM connects to Ollama.")],
        timeout_s=7,
    )

    assert result.parsed == {"facts": []}
    assert result.usage == {"prompt_tokens": 11, "completion_tokens": 7}
    assert captured["url"] == "http://localhost:11434/api/chat"
    assert captured["body"]["format"] == "json"
    assert captured["body"]["think"] is False
    assert captured["body"]["options"]["num_predict"] == 128
    assert not captured["body"]["messages"][0]["content"].startswith("/no_think")
    assert captured["timeout"] == 7


def test_extract_kg_triples_writes_provenance(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    content = "Liquid LLM connects to Ollama on localhost."
    collection = FakeCollection(
        ["drawer_123"],
        [content],
        [{"wing": "liquid_llm", "room": "architecture", "source_file": "opencode://session-1"}],
    )

    monkeypatch.setattr("mempalace.kg_extractor.get_collection", lambda *a, **kw: collection)
    monkeypatch.setattr(
        "mempalace.kg_extractor._call_llm",
        lambda *a, **kw: (
            {
                "facts": [
                    {
                        "subject": "Liquid LLM",
                        "predicate": "connects_to",
                        "object": "Ollama",
                        "valid_from": None,
                        "valid_to": None,
                        "confidence": 0.91,
                        "evidence": content,
                    }
                ]
            },
            {"prompt_tokens": 10, "completion_tokens": 5},
        ),
    )

    result = extract_kg_triples(
        str(palace),
        cfg=LLMConfig(endpoint="http://localhost:11434/v1", model="test-model"),
    )

    assert result["success"] is True
    assert result["processed"] == 1
    assert result["candidate_facts"] == 1
    assert result["written"] == 1

    conn = sqlite3.connect(str(palace / "knowledge_graph.sqlite3"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT source_file, source_drawer_id, source_closet, adapter_name FROM triples"
    ).fetchone()
    conn.close()

    assert row["source_file"] == "opencode://session-1"
    assert row["source_drawer_id"] == "drawer_123"
    assert row["source_closet"] == content
    assert row["adapter_name"] == "kg-llm:test-model"


def test_extract_kg_triples_rejects_non_verbatim_evidence(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    collection = FakeCollection(
        ["drawer_123"],
        ["Liquid LLM connects to Ollama on localhost."],
        [{"wing": "liquid_llm", "room": "architecture", "source_file": "opencode://session-1"}],
    )

    monkeypatch.setattr("mempalace.kg_extractor.get_collection", lambda *a, **kw: collection)
    monkeypatch.setattr(
        "mempalace.kg_extractor._call_llm",
        lambda *a, **kw: (
            {
                "facts": [
                    {
                        "subject": "Liquid LLM",
                        "predicate": "connects_to",
                        "object": "Ollama",
                        "valid_from": None,
                        "valid_to": None,
                        "confidence": 0.91,
                        "evidence": "Liquid LLM uses Ollama.",
                    }
                ]
            },
            None,
        ),
    )

    result = extract_kg_triples(
        str(palace),
        cfg=LLMConfig(endpoint="http://localhost:11434/v1", model="test-model"),
    )

    assert result["candidate_facts"] == 0
    assert result["rejected_facts"] == 1
    assert result["written"] == 0
    assert os.path.exists(palace / "knowledge_graph.sqlite3")


def test_extract_kg_triples_surfaces_llm_failure_reason(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    collection = FakeCollection(
        ["drawer_123"],
        ["Liquid LLM connects to Ollama on localhost."],
        [{"wing": "liquid_llm", "room": "architecture", "source_file": "opencode://session-1"}],
    )

    monkeypatch.setattr("mempalace.kg_extractor.get_collection", lambda *a, **kw: collection)
    monkeypatch.setattr(
        "mempalace.kg_extractor._call_llm",
        lambda *a, **kw: LLMCallResult(None, error="timeout_after_5s"),
    )

    result = extract_kg_triples(
        str(palace),
        cfg=LLMConfig(endpoint="http://localhost:11434/v1", model="test-model"),
        timeout_s=5,
    )

    assert result["success"] is False
    assert result["failed"] == 1
    assert result["errors"] == [{"drawer_id": "drawer_123", "error": "timeout_after_5s"}]


def test_benchmark_kg_models_reports_success_and_failures(tmp_path, monkeypatch):
    palace = tmp_path / "palace"
    palace.mkdir()
    content = "Liquid LLM connects to Ollama on localhost."
    collection = FakeCollection(
        ["drawer_123"],
        [content],
        [{"wing": "liquid_llm", "room": "architecture", "source_file": "opencode://session-1"}],
    )

    def fake_call_llm(cfg, **kwargs):
        if cfg.model == "slow-model":
            return LLMCallResult(None, error="timeout_after_5s")
        return LLMCallResult(
            {
                "facts": [
                    {
                        "subject": "Liquid LLM",
                        "predicate": "connects_to",
                        "object": "Ollama",
                        "valid_from": None,
                        "valid_to": None,
                        "confidence": 0.91,
                        "evidence_id": 1,
                    }
                ]
            },
            {"prompt_tokens": 10, "completion_tokens": 5},
        )

    monkeypatch.setattr("mempalace.kg_extractor.get_collection", lambda *a, **kw: collection)
    monkeypatch.setattr("mempalace.kg_extractor._call_llm", fake_call_llm)

    result = benchmark_kg_models(
        str(palace),
        models=["good-model", "slow-model"],
        endpoint="http://localhost:11434/v1",
        samples=1,
        timeout_s=5,
    )

    assert result["success"] is True
    assert result["dry_run"] is True
    assert result["sample_count"] == 1
    assert len(result["models"]) == 2
    assert result["models"][0]["model"] == "good-model"
    assert result["models"][0]["successful_drawers"] == 1
    assert result["models"][0]["candidate_facts"] == 1
    assert result["models"][1]["model"] == "slow-model"
    assert result["models"][1]["failed"] == 1
    assert result["models"][1]["errors"] == [{"error": "timeout_after_5s", "count": 1}]
    assert not os.path.exists(palace / "knowledge_graph.sqlite3")


def test_parse_fact_candidates_canonicalizes_whitespace_evidence():
    content = "missing DEVELOPER_DIR path:\n/Applications"
    parsed = {
        "facts": [
            {
                "subject": "DEVELOPER_DIR",
                "predicate": "configured_with",
                "object": "/Applications",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.8,
                "evidence": "missing DEVELOPER_DIR path: /Applications",
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(parsed, content, 0.6)

    assert rejected == []
    assert len(facts) == 1
    assert facts[0]["evidence"] == "missing DEVELOPER_DIR path:\n/Applications"


def test_parse_fact_candidates_rejects_unsupported_predicate():
    content = "Liquid LLM connects to Ollama on localhost."
    parsed = {
        "facts": [
            {
                "subject": "Liquid LLM",
                "predicate": "random_relation",
                "object": "Ollama",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.8,
                "evidence": content,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(parsed, content, 0.0)

    assert facts == []
    assert rejected[0]["reason"] == "unsupported_predicate"


def test_parse_fact_candidates_uses_evidence_id_snippet():
    content = "Liquid LLM connects to Ollama on localhost."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "Liquid LLM",
                "predicate": "connects_to",
                "object": "Ollama",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.8,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(parsed, content, 0.6, snippets=snippets)

    assert rejected == []
    assert len(facts) == 1
    assert facts[0]["evidence"] == content


def test_parse_fact_candidates_canonicalizes_project_subject():
    content = "Xcode 27.0 beta with iOS 27.0 SDK."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid llm",
                "predicate": "uses_sdk",
                "object": "iOS 27.0 SDK",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["subject"] == "liquid_llm"


def test_parse_fact_candidates_rejects_project_progress_fact():
    content = "liquid_llm works on building the app."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "works_on",
                "object": "building the app",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "project_progress_fact"


def test_parse_fact_candidates_maps_liquid_glass_sdk_to_ui_framework():
    content = "The app uses Apple Liquid Glass UI components."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "uses_sdk",
                "object": "Apple Liquid Glass UI components",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["predicate"] == "uses_ui_framework"


def test_parse_fact_candidates_maps_project_api_component_to_uses_api():
    content = "Xcode 27.0 beta with iOS 27.0 SDK -- this has the Liquid Glass APIs."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "has_component",
                "object": "Liquid Glass APIs",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["predicate"] == "uses_api"


def test_parse_fact_candidates_maps_api_framework_to_uses_api():
    content = "Xcode 27.0 beta with iOS 27.0 SDK -- this has the Liquid Glass APIs."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "uses_ui_framework",
                "object": "Liquid Glass APIs",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["predicate"] == "uses_api"


def test_parse_fact_candidates_rejects_toolchain_as_platform():
    content = "Xcode 27.0 beta with iOS 27.0 SDK -- this has the Liquid Glass APIs."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "targets_platform",
                "object": "Xcode 27.0 beta with iOS 27.0 SDK",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "non_platform_target"


def test_parse_fact_candidates_maps_runs_on_platform_to_targets_platform():
    content = "This project targets iOS/macOS/visionOS 27."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "runs_on",
                "object": "iOS/macOS/visionOS 27",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["predicate"] == "targets_platform"


def test_parse_fact_candidates_rejects_open_editor_session_state():
    content = "> I have xcode live and open, you just need to edit the files"
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "built_with",
                "object": "xcode live",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "progress_evidence"


def test_parse_fact_candidates_rejects_missing_version_in_evidence():
    content = "Xcode beta is installed but xcode-select points to a missing path."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "uses_sdk",
                "object": "Xcode 26.3",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "object_not_supported_by_evidence"


def test_parse_fact_candidates_maps_xcode_sdk_predicate_to_configured_with():
    content = "This is an Xcode 26.3 project."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "uses_sdk",
                "object": "Xcode 26.3",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert facts[0]["predicate"] == "configured_with"


def test_parse_fact_candidates_rejects_unproven_fixed_issue():
    content = "ContentView.swift uses Swift Playgrounds format, which won't build as an app."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "fixed_issue",
                "object": "import Playgrounds",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "unproven_fixed_issue"


def test_parse_fact_candidates_rejects_object_not_in_evidence():
    content = "There's already an Xcode project here."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "Xcode project",
                "predicate": "configured_with",
                "object": "Apple Liquid Glass UI components",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "object_not_supported_by_evidence"


def test_parse_fact_candidates_rejects_progress_evidence():
    content = "Let me verify it builds for the iOS simulator."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "targets_platform",
                "object": "iOS simulator",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "progress_evidence"


def test_parse_fact_candidates_rejects_file_only_error_object():
    content = "One brace issue in ModelsView.swift."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "has_error",
                "object": "ModelsView.swift",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert facts == []
    assert rejected[0]["reason"] == "underspecified_error"


def test_parse_fact_candidates_accepts_specific_error_object():
    content = "One brace issue in ModelsView.swift."
    snippets = [(1, content)]
    parsed = {
        "facts": [
            {
                "subject": "liquid_llm",
                "predicate": "has_error",
                "object": "brace issue in ModelsView.swift",
                "valid_from": None,
                "valid_to": None,
                "confidence": 0.9,
                "evidence_id": 1,
            }
        ]
    }

    facts, rejected = _parse_fact_candidates(
        parsed, content, 0.0, snippets=snippets, project_entity="liquid_llm"
    )

    assert rejected == []
    assert len(facts) == 1
    assert facts[0]["object"] == "brace issue in ModelsView.swift"
