"""Tests for mempalace.llm_refine.

Uses a fake provider for deterministic, offline tests. No network.
"""

from dataclasses import dataclass


from mempalace.llm_client import LLMError, LLMResponse
from mempalace.llm_refine import (
    _apply_classifications,
    _build_user_prompt,
    _collect_contexts,
    _extract_json_candidates,
    _is_authoritative_person,
    _is_authoritative_project,
    _parse_response,
    collect_corpus_text,
    refine_entities,
)


# ── fake provider ───────────────────────────────────────────────────────


@dataclass
class FakeProvider:
    """Returns a caller-supplied JSON string on every classify call."""

    response_text: str = ""
    should_raise: Exception = None
    call_count: int = 0
    interrupt_on_call: int = -1

    def classify(self, system, user, json_mode=True):
        self.call_count += 1
        if self.call_count == self.interrupt_on_call:
            raise KeyboardInterrupt()
        if self.should_raise is not None:
            raise self.should_raise
        return LLMResponse(text=self.response_text, model="fake", provider="fake", raw={})

    def check_available(self):
        return True, "ok"


# ── _collect_contexts ───────────────────────────────────────────────────


def test_collect_contexts_finds_matches():
    lines = [
        "Something about Alice",
        "Bob said hello",
        "Alice was here",
        "Alice walked by",
    ]
    out = _collect_contexts(lines, "Alice", max_lines=2)
    assert len(out) == 2
    assert all("alice" in line.lower() for line in out)


def test_collect_contexts_case_insensitive():
    lines = ["lowercase alice mention"]
    out = _collect_contexts(lines, "Alice")
    assert out == ["lowercase alice mention"]


def test_collect_contexts_uses_token_boundaries():
    lines = [
        "forgot should not match",
        "Go is a language.",
        "go-v1 shipped.",
    ]
    out = _collect_contexts(lines, "Go", max_lines=5)
    assert out == ["Go is a language.", "go-v1 shipped."]


def test_collect_contexts_dedupes_identical_lines():
    lines = ["Alice", "Alice", "Alice was here"]
    out = _collect_contexts(lines, "Alice", max_lines=5)
    # two unique lines, not three
    assert len(out) == 2


def test_collect_contexts_truncates_long_lines():
    lines = ["Alice " + ("x" * 1000)]
    out = _collect_contexts(lines, "Alice")
    assert len(out[0]) <= 240


def test_collect_contexts_no_matches():
    assert _collect_contexts(["nothing here"], "Alice") == []


# ── _build_user_prompt ──────────────────────────────────────────────────


def test_build_user_prompt_numbers_and_includes_contexts():
    prompt = _build_user_prompt(
        [
            ("Alice", "uncertain", ["Alice said hi"]),
            ("Bob", "project", []),
        ]
    )
    assert "1. Alice" in prompt
    assert "2. Bob" in prompt
    assert "Alice said hi" in prompt
    assert "(no context available)" in prompt


# ── _parse_response ─────────────────────────────────────────────────────


def test_parse_response_canonicalizes_label():
    text = '{"classifications": [{"name": "Alice", "label": "person", "reason": "x"}]}'
    out = _parse_response(text, ["Alice"])
    assert out["Alice"] == ("PERSON", "x")


def test_parse_response_accepts_type_alias():
    """LLMs may return 'type' instead of 'label'."""
    text = '{"classifications": [{"name": "Bob", "type": "PROJECT"}]}'
    out = _parse_response(text, ["Bob"])
    assert out["Bob"][0] == "PROJECT"


def test_parse_response_maps_unknown_label_to_ambiguous():
    text = '{"classifications": [{"name": "X", "label": "WEIRD"}]}'
    out = _parse_response(text, ["X"])
    assert out["X"][0] == "AMBIGUOUS"


def test_parse_response_restores_canonical_casing():
    """Model may lowercase the name; we restore against the expected set."""
    text = '{"classifications": [{"name": "mempalace", "label": "PROJECT"}]}'
    out = _parse_response(text, ["MemPalace"])
    assert "MemPalace" in out
    assert out["MemPalace"][0] == "PROJECT"


def test_parse_response_strips_code_fences():
    text = '```json\n{"classifications": [{"name": "X", "label": "TOPIC"}]}\n```'
    out = _parse_response(text, ["X"])
    assert out["X"][0] == "TOPIC"


def test_parse_response_extracts_json_after_prose():
    text = 'Sure, here is the JSON: {"classifications": [{"name": "X", "label": "TOPIC"}]}'
    out = _parse_response(text, ["X"])
    assert out["X"][0] == "TOPIC"


def test_parse_response_extracts_fenced_json_after_prose():
    text = 'Sure:\n```json\n{"classifications": [{"name": "X", "label": "PROJECT"}]}\n```'
    out = _parse_response(text, ["X"])
    assert out["X"][0] == "PROJECT"


def test_extract_json_candidates_handles_embedded_array():
    text = 'prefix [{"name": "Y", "label": "PERSON"}] suffix'
    candidates = _extract_json_candidates(text)
    assert '[{"name": "Y", "label": "PERSON"}]' in candidates


def test_parse_response_ignores_non_json_brackets_before_payload():
    text = 'See [note] first. JSON: {"classifications": [{"name": "X", "label": "TOPIC"}]}'
    out = _parse_response(text, ["X"])
    assert out["X"][0] == "TOPIC"


def test_parse_response_malformed_returns_empty():
    out = _parse_response("not json at all", ["X"])
    assert out == {}


def test_parse_response_accepts_top_level_list():
    """Some models skip the wrapping object and return the list directly."""
    text = '[{"name": "Y", "label": "PERSON"}]'
    out = _parse_response(text, ["Y"])
    assert out["Y"][0] == "PERSON"


# ── _apply_classifications ──────────────────────────────────────────────


def test_apply_classifications_moves_to_correct_bucket():
    detected = {
        "people": [],
        "projects": [
            {
                "name": "Foo",
                "type": "project",
                "confidence": 0.8,
                "frequency": 3,
                "signals": ["old"],
            }
        ],
        "uncertain": [
            {"name": "Alice", "type": "uncertain", "confidence": 0.4, "frequency": 5, "signals": []}
        ],
    }
    decisions = {
        "Foo": ("PROJECT", "real project name"),
        "Alice": ("PERSON", "clearly a person"),
    }
    new, reclass, dropped = _apply_classifications(detected, decisions)
    assert len(new["people"]) == 1
    assert new["people"][0]["name"] == "Alice"
    assert new["people"][0]["type"] == "person"
    assert reclass == 1  # Alice moved uncertain -> people
    assert dropped == 0


def test_apply_classifications_drops_common_word():
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "Never",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 20,
                "signals": [],
            }
        ],
    }
    decisions = {"Never": ("COMMON_WORD", "adverb")}
    new, _, dropped = _apply_classifications(detected, decisions)
    assert dropped == 1
    assert new["uncertain"] == []


def test_apply_classifications_keeps_unvisited_entries():
    detected = {
        "people": [
            {
                "name": "Igor",
                "type": "person",
                "confidence": 0.99,
                "frequency": 100,
                "signals": ["git"],
            }
        ],
        "projects": [],
        "uncertain": [],
    }
    # No decision for Igor — should stay untouched
    new, reclass, dropped = _apply_classifications(detected, {})
    assert new["people"][0]["name"] == "Igor"
    assert reclass == 0
    assert dropped == 0


def test_apply_classifications_appends_reason_signal():
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "Foo",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
    }
    decisions = {"Foo": ("PERSON", "spoken of by name")}
    new, _, _ = _apply_classifications(detected, decisions)
    assert any("LLM: person" in s for s in new["people"][0]["signals"])
    assert any("spoken of by name" in s for s in new["people"][0]["signals"])


def test_apply_classifications_topic_goes_to_topics_bucket():
    """TOPIC classifications now route to a dedicated ``topics`` bucket so the
    miner can use them as cross-wing tunnel signal (issue #1180)."""
    detected = {
        "people": [],
        "projects": [
            {
                "name": "Paris",
                "type": "project",
                "confidence": 0.7,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
        "uncertain": [],
    }
    decisions = {"Paris": ("TOPIC", "city, not a project")}
    new, reclass, _ = _apply_classifications(detected, decisions)
    assert len(new["projects"]) == 0
    assert len(new["uncertain"]) == 0
    assert len(new["topics"]) == 1
    assert new["topics"][0]["name"] == "Paris"
    assert new["topics"][0]["type"] == "topic"
    assert reclass == 1


def test_apply_classifications_ambiguous_still_goes_to_uncertain():
    detected = {
        "people": [],
        "projects": [
            {
                "name": "Foo",
                "type": "project",
                "confidence": 0.7,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
        "uncertain": [],
    }
    decisions = {"Foo": ("AMBIGUOUS", "context insufficient")}
    new, reclass, _ = _apply_classifications(detected, decisions)
    assert len(new["projects"]) == 0
    assert len(new["uncertain"]) == 1
    assert new["uncertain"][0]["name"] == "Foo"
    assert reclass == 1


def test_apply_classifications_can_block_llm_only_project_promotion():
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "Terraform",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
    }
    decisions = {"Terraform": ("PROJECT", "tool")}
    new, reclass, _ = _apply_classifications(
        detected,
        decisions,
        allow_project_promotions=False,
    )
    assert new["projects"] == []
    assert new["uncertain"][0]["name"] == "Terraform"
    assert new["uncertain"][0]["type"] == "uncertain"
    assert reclass == 0


def test_apply_classifications_allows_project_promotion_for_prose_only_mode():
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "Project Aurora",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
    }
    decisions = {"Project Aurora": ("PROJECT", "user effort")}
    new, reclass, _ = _apply_classifications(detected, decisions)
    assert new["projects"][0]["name"] == "Project Aurora"
    assert new["projects"][0]["type"] == "project"
    assert reclass == 1


# ── authoritative source filters ────────────────────────────────────────


def test_is_authoritative_person_requires_git_signal():
    assert _is_authoritative_person({"signals": ["5 commits across 2 repos"]})
    assert not _is_authoritative_person({"signals": ["pronoun nearby (5x)"]})


def test_is_authoritative_project_requires_manifest_or_git_signal():
    assert _is_authoritative_project({"signals": ["package.json, 12 of your commits"]})
    assert _is_authoritative_project({"signals": ["57 commits (none by you)"]})
    assert not _is_authoritative_project({"signals": ["code file reference (5x)"]})


# ── refine_entities ─────────────────────────────────────────────────────


def _sample_detected():
    return {
        "people": [
            {
                "name": "Igor",
                "type": "person",
                "confidence": 0.99,
                "frequency": 100,
                "signals": ["git"],
            }
        ],
        "projects": [
            {
                "name": "Foo",
                "type": "project",
                "confidence": 0.7,
                "frequency": 5,
                "signals": ["regex"],
            }
        ],
        "uncertain": [
            {
                "name": "Never",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 10,
                "signals": [],
            },
            {
                "name": "Alice",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 5,
                "signals": [],
            },
        ],
    }


def test_refine_entities_end_to_end_with_fake_provider():
    provider = FakeProvider(
        response_text=(
            '{"classifications": ['
            '{"name": "Foo", "label": "PROJECT", "reason": "real"},'
            '{"name": "Never", "label": "COMMON_WORD"},'
            '{"name": "Alice", "label": "PERSON", "reason": "name"}'
            "]}"
        )
    )
    result = refine_entities(
        _sample_detected(),
        corpus_text="Alice said hi. Foo was shipped. Never gonna.",
        provider=provider,
        show_progress=False,
    )
    assert result.batches_total == 1
    assert result.batches_completed == 1
    assert not result.cancelled
    # Alice → people, Never → dropped, Foo stays in projects
    names_in_people = [e["name"] for e in result.merged["people"]]
    assert "Alice" in names_in_people
    assert "Igor" in names_in_people  # untouched
    assert "Never" not in [e["name"] for e in result.merged["uncertain"]]
    assert result.dropped == 1


def test_refine_entities_skips_high_confidence_projects():
    """Manifest-backed projects (conf >= 0.95) aren't sent to the LLM."""
    detected = {
        "people": [],
        "projects": [
            {
                "name": "manifest-backed",
                "type": "project",
                "confidence": 0.99,
                "frequency": 50,
                "signals": ["pyproject.toml"],
            }
        ],
        "uncertain": [],
    }
    provider = FakeProvider(response_text='{"classifications": []}')
    refine_entities(detected, "", provider, show_progress=False)
    # Should not have called the LLM at all
    assert provider.call_count == 0


def test_refine_entities_refines_high_confidence_regex_projects():
    """High-confidence regex projects still need LLM review without source signal."""
    detected = {
        "people": [],
        "projects": [
            {
                "name": "OpenAPI",
                "type": "project",
                "confidence": 0.99,
                "frequency": 5,
                "signals": ["code file reference (5x)"],
            }
        ],
        "uncertain": [],
    }
    provider = FakeProvider(
        response_text=(
            '{"classifications": [{"name": "OpenAPI", "label": "TOPIC", "reason": "technology"}]}'
        )
    )
    result = refine_entities(detected, "OpenAPI schemas", provider, show_progress=False)
    assert provider.call_count == 1
    assert result.reclassified == 1
    assert result.merged["projects"] == []
    # TOPIC labels go to the dedicated ``topics`` bucket so the miner can
    # use them for cross-wing tunnel computation (issue #1180).
    assert result.merged["topics"][0]["name"] == "OpenAPI"


def test_refine_entities_refines_regex_people_but_skips_git_people():
    detected = {
        "people": [
            {
                "name": "Igor Lins e Silva",
                "type": "person",
                "confidence": 0.99,
                "frequency": 100,
                "signals": ["100 commits across 3 repos"],
            },
            {
                "name": "Tool",
                "type": "person",
                "confidence": 0.99,
                "frequency": 5,
                "signals": ["pronoun nearby (5x)"],
            },
        ],
        "projects": [],
        "uncertain": [],
    }
    provider = FakeProvider(
        response_text='{"classifications": [{"name": "Tool", "label": "COMMON_WORD"}]}'
    )
    result = refine_entities(detected, "Tool is a common noun.", provider, show_progress=False)
    assert provider.call_count == 1
    names = [e["name"] for e in result.merged["people"]]
    assert names == ["Igor Lins e Silva"]
    assert result.dropped == 1


def test_refine_entities_can_keep_llm_only_project_in_uncertain():
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": "Terraform",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 9,
                "signals": ["regex"],
            }
        ],
    }
    provider = FakeProvider(
        response_text='{"classifications": [{"name": "Terraform", "label": "PROJECT"}]}'
    )
    result = refine_entities(
        detected,
        "Terraform config",
        provider,
        show_progress=False,
        allow_project_promotions=False,
    )
    assert result.merged["projects"] == []
    assert result.merged["uncertain"][0]["name"] == "Terraform"
    assert any("LLM: project" in s for s in result.merged["uncertain"][0]["signals"])


def test_refine_entities_empty_candidates_returns_noop():
    detected = {"people": [], "projects": [], "uncertain": []}
    provider = FakeProvider()
    result = refine_entities(detected, "", provider, show_progress=False)
    assert result.batches_total == 0
    assert result.reclassified == 0
    assert result.merged == detected


def test_refine_entities_handles_batch_error_gracefully():
    provider = FakeProvider(should_raise=LLMError("transport broke"))
    result = refine_entities(
        _sample_detected(),
        corpus_text="",
        provider=provider,
        show_progress=False,
    )
    assert result.errors
    assert "transport broke" in result.errors[0]
    # Detected unchanged (no successful decisions)
    assert result.reclassified == 0
    assert result.cancelled is False


def test_refine_entities_ctrl_c_returns_partial():
    """Ctrl-C during refinement marks cancelled=True and returns partial result."""
    # Two batches' worth of candidates
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {
                "name": f"Cand{i}",
                "type": "uncertain",
                "confidence": 0.4,
                "frequency": 3,
                "signals": [],
            }
            for i in range(50)
        ],
    }
    provider = FakeProvider(
        response_text='{"classifications": []}',
        interrupt_on_call=2,  # interrupt on second batch
    )
    result = refine_entities(detected, "", provider, batch_size=25, show_progress=False)
    assert result.cancelled is True
    assert result.batches_completed == 1  # first batch finished; second interrupted
    assert result.batches_total == 2


def test_refine_entities_malformed_response_recorded_as_error():
    provider = FakeProvider(response_text="not json")
    result = refine_entities(_sample_detected(), "", provider, show_progress=False)
    assert any("could not parse" in e for e in result.errors)


# ── collect_corpus_text ─────────────────────────────────────────────────


def test_collect_corpus_text_reads_prose_files(tmp_path):
    (tmp_path / "a.md").write_text("hello world")
    (tmp_path / "b.txt").write_text("more prose")
    (tmp_path / "c.py").write_text("import os")  # not prose, skipped
    text = collect_corpus_text(str(tmp_path))
    assert "hello world" in text
    assert "more prose" in text
    assert "import os" not in text


def test_collect_corpus_text_prefers_recent(tmp_path):
    import os
    import time

    old = tmp_path / "old.md"
    old.write_text("OLD_CONTENT")
    time.sleep(0.01)
    new = tmp_path / "new.md"
    new.write_text("NEW_CONTENT")
    # Force old to be older still
    old_mtime = old.stat().st_mtime - 3600
    os.utime(old, (old_mtime, old_mtime))

    text = collect_corpus_text(str(tmp_path), max_files=1)
    assert "NEW_CONTENT" in text
    assert "OLD_CONTENT" not in text


def test_collect_corpus_text_missing_dir_returns_empty(tmp_path):
    assert collect_corpus_text(str(tmp_path / "nope")) == ""


def test_collect_corpus_text_caps_bytes_per_file(tmp_path):
    big = tmp_path / "big.md"
    big.write_text("x" * 100_000)
    text = collect_corpus_text(str(tmp_path), max_files=1, max_bytes_per_file=500)
    assert len(text) <= 600  # 500 + newlines
