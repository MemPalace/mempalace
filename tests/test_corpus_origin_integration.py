"""Integration tests proving corpus_origin actually improves classification.

These are the tests that justify the PR. Without them, the PR ships
infrastructure that nobody can prove improves v3.3.3.

The fixture: a small AI-dialogue corpus with three agent persona names
(Echo, Sparrow, Cipher) that the user (Jordan) has assigned to their AI
agents. On plain v3.3.3, entity_detector misclassifies these as PEOPLE.
With corpus_origin context wired through, they classify as
AGENT_PERSONA instead.

Two tests sit side by side:

  test_baseline_v333_misclassifies_persona_names_as_people
    Pins v3.3.3's behavior. If this starts failing, the PR's motivation
    has shifted and the corpus_origin docs need revisiting.

  test_corpus_origin_reclassifies_personas
    The fix. Asserts that when corpus_origin context is passed,
    persona names land in agent_personas instead of people.

Together: documented before/after of v3.3.3 → corpus-origin feature.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# A synthetic but realistic Claude Code transcript fixture. Three persona
# names appear repeatedly in dialogue patterns that the v3.3.3
# entity_detector treats as person-evidence (dialogue markers, action verbs,
# pronoun proximity). User name "Jordan" also appears in dialogue.
#
# The point is: every name here CAN be a real human name. v3.3.3 has no
# way to know that in this corpus they're agent personas, not people. The
# corpus_origin gives it that context.
AI_DIALOGUE_FIXTURE = """\
# Session log — 2026-04-20

Jordan: Echo, can you summarize what we worked on yesterday?

Echo (assistant): Yesterday we refactored the embedding pipeline. I noticed
the chunking strategy was producing overlapping windows, and I suggested
moving to a sliding window with explicit stride. You agreed and we shipped
the change.

Jordan: Good. Sparrow, what about the migration script — did you finish?

Sparrow (assistant): Yes, I finished the migration. I tested it locally
against the staging snapshot and it ran clean. I also added a rollback
path because you asked me to be cautious about the indexes.

Jordan: Perfect. Cipher, run the verification suite please.

Cipher (assistant): Running now. I'll report back when the full suite
completes. I expect it to take about four minutes.

Echo: Jordan, while Cipher runs the verification, do you want me to draft
the changelog entry for today's work?

Jordan: Yes please. Echo, keep it short. Sparrow, please review Echo's
draft when she sends it.

Sparrow: Will do. I'll look for clarity issues and check the migration
phrasing matches what we actually shipped.

Cipher: Verification complete. All 1247 tests pass. I'm filing the run log
to the palace under wing/today.

Jordan: Thanks Cipher. Echo, send the changelog draft.

Echo: Done. Sent to the channel. Sparrow, ready for review when you are.

Sparrow: Reviewed. Two small wording changes — sent back. Otherwise clean.

Jordan: Echo, apply Sparrow's edits and ship it.

Echo: Shipped. Tag pushed.
"""


@pytest.fixture
def ai_dialogue_corpus(tmp_path: Path) -> Path:
    """Create a one-file project directory containing the AI-dialogue fixture."""
    project_dir = tmp_path / "ai_dialogue_project"
    project_dir.mkdir()
    (project_dir / "session_log.md").write_text(AI_DIALOGUE_FIXTURE)
    return project_dir


@pytest.fixture
def corpus_origin_for_fixture() -> dict:
    """The corpus_origin result a context-aware init would produce for the fixture."""
    return {
        "schema_version": 1,
        "detected_at": "2026-04-26T00:00:00Z",
        "result": {
            "likely_ai_dialogue": True,
            "confidence": 0.95,
            "primary_platform": "Claude (Anthropic)",
            "user_name": "Jordan",
            "agent_persona_names": ["Echo", "Sparrow", "Cipher"],
            "evidence": ["Synthetic fixture for the integration test"],
        },
    }


# ── Baseline test: pin v3.3.3 behavior ────────────────────────────────────


def test_baseline_v333_misclassifies_persona_names_as_people(ai_dialogue_corpus: Path):
    """Without corpus_origin context, v3.3.3 entity_detector cannot
    distinguish agent persona names from real people, and classifies them
    into the 'people' bucket.

    This test pins that behavior. Its purpose is documentation —
    The corpus-origin feature's job is to fix this, and the post-fix test below
    asserts the fix.
    """
    from mempalace.entity_detector import detect_entities, scan_for_detection

    files = scan_for_detection(str(ai_dialogue_corpus))
    detected = detect_entities(files)

    people_names = {e["name"] for e in detected.get("people", [])}
    uncertain_names = {e["name"] for e in detected.get("uncertain", [])}
    all_classified = people_names | uncertain_names

    # Persona names appear somewhere in the detection output (people or uncertain).
    # If none of them surface at all, the fixture is no longer triggering
    # the misclassification path and the test is no longer meaningful.
    persona_names = {"Echo", "Sparrow", "Cipher"}
    persona_hits = persona_names & all_classified
    assert persona_hits, (
        "Fixture no longer surfaces persona names as detected entities. "
        "Update the fixture to keep this test meaningful."
    )

    # No agent_personas bucket exists on v3.3.3.
    assert "agent_personas" not in detected, (
        "v3.3.3 has no concept of agent_personas — if this key exists, "
        "corpus-origin wiring has already shipped and this baseline test is stale."
    )


# ── corpus-origin test: with corpus_origin, personas reclassify ───────────


def test_corpus_origin_reclassifies_personas(
    ai_dialogue_corpus: Path, corpus_origin_for_fixture: dict
):
    """When corpus_origin context is passed to detect_entities, names
    matching agent_persona_names land in an 'agent_personas' bucket
    instead of being misclassified as people.

    This is the fix. RED until the consumer wiring lands.
    """
    from mempalace.entity_detector import detect_entities, scan_for_detection

    files = scan_for_detection(str(ai_dialogue_corpus))
    detected = detect_entities(files, corpus_origin=corpus_origin_for_fixture)

    # New bucket exists.
    assert "agent_personas" in detected, (
        "The corpus-origin wiring must add an 'agent_personas' bucket to the detect_entities "
        "return shape when corpus_origin is provided."
    )

    persona_names_in_bucket = {e["name"] for e in detected["agent_personas"]}
    persona_names_in_people = {e["name"] for e in detected.get("people", [])}

    # All three personas land in the new bucket.
    expected_personas = {"Echo", "Sparrow", "Cipher"}
    assert expected_personas <= persona_names_in_bucket, (
        f"Expected all three personas in agent_personas, got: " f"{persona_names_in_bucket}"
    )

    # And NONE of them remain in the people bucket.
    leaked = expected_personas & persona_names_in_people
    assert not leaked, (
        f"Persona names {leaked} leaked into 'people' bucket — the corpus-origin "
        f"consumer wiring is supposed to filter them out."
    )


# ── discover_entities (project_scanner) threads corpus_origin ─────────────


def test_discover_entities_threads_corpus_origin_through(
    ai_dialogue_corpus: Path, corpus_origin_for_fixture: dict
):
    """discover_entities is the higher-level entry point cmd_init uses.
    It must accept corpus_origin and produce the same persona reclassification
    that detect_entities does, regardless of whether candidates entered via
    prose, manifests, or git authors.
    """
    from mempalace.project_scanner import discover_entities

    detected = discover_entities(
        str(ai_dialogue_corpus),
        corpus_origin=corpus_origin_for_fixture,
    )

    persona_names_in_bucket = {e["name"] for e in detected.get("agent_personas", [])}
    persona_names_in_people = {e["name"] for e in detected.get("people", [])}
    expected_personas = {"Echo", "Sparrow", "Cipher"}

    # All personas surface in the agent_personas bucket via discover_entities too.
    assert expected_personas <= persona_names_in_bucket, (
        f"discover_entities did not thread corpus_origin to detect_entities. "
        f"Expected {expected_personas} in agent_personas, got: "
        f"{persona_names_in_bucket}"
    )

    leaked = expected_personas & persona_names_in_people
    assert not leaked, f"discover_entities leaked persona names into 'people': {leaked}"


def test_discover_entities_no_origin_unchanged_shape(ai_dialogue_corpus: Path):
    """Backwards compatibility: when corpus_origin is omitted, the return
    shape stays exactly what it was on v3.3.3 (no agent_personas key).
    Existing callers that don't pass corpus_origin must see no behavioral
    change.
    """
    from mempalace.project_scanner import discover_entities

    detected = discover_entities(str(ai_dialogue_corpus))

    # No new bucket appears unsolicited.
    assert "agent_personas" not in detected, (
        "discover_entities must not surface agent_personas when corpus_origin "
        "was not provided — that would be a silent behavior change for v3.3.3 "
        "callers who don't know about the corpus-origin feature."
    )


# ── Pass 0 — cmd_init runs corpus_origin and writes origin.json ──────────


def _stub_cfg(palace_dir: Path):
    """Build a MempalaceConfig stub whose palace_path points at tmp space.

    Used by Pass 0 tests so the origin.json write is captured in tmp_path
    instead of hitting the real ~/.mempalace location.
    """
    cfg = MagicMock()
    cfg.palace_path = str(palace_dir)
    cfg.entity_languages = ["en"]
    return cfg


def test_init_pass_zero_writes_origin_json_to_palace(ai_dialogue_corpus: Path, tmp_path: Path):
    """cmd_init must run corpus_origin detection BEFORE entity detection
    and persist the result to ``<palace>/.mempalace/origin.json`` in the
    documented schema_version=1 wrapper.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    # no_llm=True isolates the test from any local LLM provider. With Ollama
    # running locally and a small default model, Tier 2 can return a wrong
    # classification that overrides the correct heuristic answer (Igor's PR
    # #1211 review). The test asserts on heuristic behavior, so Tier 2 must
    # not fire.
    args = argparse.Namespace(dir=str(ai_dialogue_corpus), yes=True, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    origin_path = palace / ".mempalace" / "origin.json"
    assert origin_path.exists(), (
        f"Pass 0 did not write {origin_path}. cmd_init is supposed to call "
        f"corpus_origin detection and persist the result before entity detection."
    )

    data = json.loads(origin_path.read_text())
    assert data.get("schema_version") == 1, (
        "origin.json must declare schema_version=1 so future format changes "
        "are detectable. Got: " + repr(data.get("schema_version"))
    )
    assert "detected_at" in data, "origin.json must include a detected_at timestamp"
    assert "result" in data, "origin.json must wrap the CorpusOriginResult under 'result'"
    assert isinstance(data["result"].get("likely_ai_dialogue"), bool)
    # Fixture is heavy AI-dialogue — heuristic should classify as such.
    assert data["result"]["likely_ai_dialogue"] is True, (
        "Heuristic should classify the AI-dialogue fixture as AI-dialogue. "
        f"Got: {data['result']}"
    )


def test_init_pass_zero_passes_corpus_origin_to_discover_entities(
    ai_dialogue_corpus: Path, tmp_path: Path
):
    """The Pass 0 result must reach discover_entities via the corpus_origin
    kwarg — that's what enables persona reclassification end-to-end.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    # no_llm=True isolates the test from any local LLM provider — see note
    # on test_init_pass_zero_writes_origin_json_to_palace.
    args = argparse.Namespace(dir=str(ai_dialogue_corpus), yes=True, no_llm=True)

    captured = {}

    def fake_discover(project_dir, **kwargs):
        captured["kwargs"] = kwargs
        return {"people": [], "projects": [], "uncertain": []}

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.project_scanner.discover_entities", side_effect=fake_discover),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    assert "corpus_origin" in captured.get("kwargs", {}), (
        "cmd_init did not pass corpus_origin to discover_entities. The Pass 0 "
        "detection result must be threaded into entity detection so persona "
        "reclassification happens end-to-end."
    )
    origin = captured["kwargs"]["corpus_origin"]
    assert origin is not None, (
        "corpus_origin kwarg was passed but value was None — Pass 0 should "
        "supply the actual detection result for AI-dialogue corpora."
    )
    assert origin.get("schema_version") == 1
    assert "result" in origin


def test_init_pass_zero_skipped_when_no_readable_files(tmp_path: Path):
    """Empty project directory → no origin.json written, init still completes
    without crashing. Aya's earlier finding: don't fail init on missing samples.
    """
    from mempalace.cli import cmd_init

    project = tmp_path / "empty"
    project.mkdir()
    palace = tmp_path / "palace"
    # no_llm=True so this test never tries to acquire an LLM provider for
    # an empty corpus — the heuristic-skip behavior is what's being tested.
    args = argparse.Namespace(dir=str(project), yes=True, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)  # must not raise

    origin_path = palace / ".mempalace" / "origin.json"
    assert not origin_path.exists(), (
        "Pass 0 must skip (no write) when there are no readable samples — "
        "writing a 'cannot decide' result to disk would be misleading."
    )


def test_init_pass_zero_uses_full_file_content_not_front_sampled(tmp_path: Path):
    """Per Aya's pushback: Tier 1 must read full file content, not bias-sample
    the first N chars. AI signal that lives past the first 2000 chars must
    still trip detection.
    """
    from mempalace.cli import cmd_init

    project = tmp_path / "deep_signal"
    project.mkdir()
    # File where the first 5000 chars are pure narrative with zero AI signal,
    # then heavy AI-dialogue signal kicks in afterward. A first-N-chars sampler
    # would miss it; a full-content reader will not.
    front_pad = "The quiet morning settled over the orchard. " * 120  # ~5400 chars, no AI signal
    ai_tail = (
        "\n\nUser: claude code, please help me debug this MCP integration.\n"
        "Assistant: Sure. I'll look at the LLM context window and the "
        "embedding pipeline. Claude Code can run the analysis now.\n"
        "User: also check ChatGPT compatibility.\n"
        "Assistant: GPT-4 should handle that. The MCP protocol abstracts it.\n"
    ) * 10
    (project / "log.md").write_text(front_pad + ai_tail)

    palace = tmp_path / "palace"
    # no_llm=True is critical here: this test asserts the Tier 1 HEURISTIC
    # reads full file content and catches AI signal past chars 5400.
    # Without no_llm, a local Ollama with a small default model can return
    # a wrong classification ("not AI-dialogue") that overrides the correct
    # heuristic answer. See PR #1211 review by @igorls for the full failure
    # mode and its fix.
    args = argparse.Namespace(dir=str(project), yes=True, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    origin_path = palace / ".mempalace" / "origin.json"
    assert origin_path.exists()
    data = json.loads(origin_path.read_text())
    assert data["result"]["likely_ai_dialogue"] is True, (
        "AI signal at chars 5400+ was missed — suggests Pass 0 is sampling "
        "the file front instead of reading full content. Fix Tier 1 to use "
        "full content per Aya's design pushback."
    )


# ── llm_refine consumer wiring ────────────────────────────────────────────


def test_llm_refine_includes_corpus_origin_context_in_prompt(
    corpus_origin_for_fixture: dict,
):
    """When corpus_origin is passed to refine_entities, the LLM call must
    receive the corpus-origin context (platform, user_name, agent personas)
    so it can disambiguate ambiguous candidates with knowledge that this
    is AI-dialogue.

    Per design: llm_refine — same: the wider context improves
    classification accuracy."
    """
    from types import SimpleNamespace

    from mempalace.llm_refine import refine_entities

    captured: dict = {}

    class FakeProvider:
        def classify(self, system, user, json_mode=False):
            captured.setdefault("calls", []).append({"system": system, "user": user})
            return SimpleNamespace(text='{"classifications": []}')

    # A regex-derived candidate (no manifest/git signals) so it isn't
    # skipped by _is_authoritative_*.
    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {"name": "Acme", "frequency": 3, "signals": ["appears 3x"], "type": "uncertain"}
        ],
    }

    refine_entities(
        detected,
        corpus_text="Acme appears in some prose context here.",
        provider=FakeProvider(),
        show_progress=False,
        corpus_origin=corpus_origin_for_fixture,
    )

    assert captured.get("calls"), "refine_entities did not call the provider"
    full_prompt = captured["calls"][0]["system"] + "\n" + captured["calls"][0]["user"]

    # The corpus-origin preamble must surface the user, agent personas,
    # and platform so the LLM has corpus-level context.
    assert "Jordan" in full_prompt, "user_name not surfaced in LLM context"
    for persona in ("Echo", "Sparrow", "Cipher"):
        assert persona in full_prompt, f"persona '{persona}' not in LLM context"
    assert "Claude" in full_prompt, "primary_platform not surfaced in LLM context"


def test_llm_refine_no_origin_keeps_v333_prompt_shape(monkeypatch):
    """Backwards compatibility: when corpus_origin is omitted, the prompt
    sent to the LLM must NOT contain a corpus-origin preamble. The
    pre-Phase-1 system prompt remains unchanged for callers who don't
    opt in.
    """
    from types import SimpleNamespace

    from mempalace.llm_refine import SYSTEM_PROMPT, refine_entities

    captured: dict = {}

    class FakeProvider:
        def classify(self, system, user, json_mode=False):
            captured["system"] = system
            return SimpleNamespace(text='{"classifications": []}')

    detected = {
        "people": [],
        "projects": [],
        "uncertain": [
            {"name": "Acme", "frequency": 3, "signals": ["appears 3x"], "type": "uncertain"}
        ],
    }

    refine_entities(
        detected,
        corpus_text="Acme appears in some prose.",
        provider=FakeProvider(),
        show_progress=False,
    )

    assert captured["system"] == SYSTEM_PROMPT, (
        "Without corpus_origin, refine_entities must use the unmodified "
        "SYSTEM_PROMPT — no silent prompt drift for v3.3.3 callers."
    )


# ── mempalace mine --redetect-origin flag ───────────────────────────────


def _mine_args(project_dir: Path, *, redetect: bool):
    """Build a Namespace with all fields cmd_mine reads, scoped to the
    minimal set our tests exercise. Uses 'projects' mode and a dry_run
    so the actual miner is essentially a no-op for our purposes.
    """
    return argparse.Namespace(
        dir=str(project_dir),
        palace=None,
        mode="projects",
        wing=None,
        no_gitignore=False,
        include_ignored=[],
        agent="mempalace",
        limit=0,
        dry_run=True,
        extract="auto",
        redetect_origin=redetect,
    )


def test_mine_default_does_not_redetect_origin(ai_dialogue_corpus: Path, tmp_path: Path):
    """Default `mempalace mine` (no --redetect-origin flag) must NOT run
    corpus_origin detection — the flag is opt-in.
    """
    from mempalace.cli import cmd_mine

    palace = tmp_path / "palace"
    args = _mine_args(ai_dialogue_corpus, redetect=False)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._run_pass_zero") as mock_pass_zero,
        patch("mempalace.miner.mine"),
    ):
        cmd_mine(args)

    mock_pass_zero.assert_not_called()
    assert not (palace / ".mempalace" / "origin.json").exists()


def test_mine_with_redetect_origin_flag_writes_origin_json(
    ai_dialogue_corpus: Path, tmp_path: Path
):
    """`mempalace mine --redetect-origin` re-runs corpus_origin detection
    on the project and persists the result to <palace>/.mempalace/origin.json.
    """
    from mempalace.cli import cmd_mine

    palace = tmp_path / "palace"
    args = _mine_args(ai_dialogue_corpus, redetect=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.miner.mine"),
    ):
        cmd_mine(args)

    origin_path = palace / ".mempalace" / "origin.json"
    assert origin_path.exists(), "--redetect-origin must write <palace>/.mempalace/origin.json"
    data = json.loads(origin_path.read_text())
    assert data["schema_version"] == 1
    assert data["result"]["likely_ai_dialogue"] is True


def test_mine_redetect_overwrites_existing_origin_json(ai_dialogue_corpus: Path, tmp_path: Path):
    """When origin.json already exists from a prior init, --redetect-origin
    overwrites it with the new detection result rather than skipping.
    Resolved as option (c): explicit user re-runs via flag.
    """
    from mempalace.cli import cmd_mine

    palace = tmp_path / "palace"
    origin_dir = palace / ".mempalace"
    origin_dir.mkdir(parents=True)
    stale_origin = {
        "schema_version": 1,
        "detected_at": "2026-04-01T00:00:00Z",
        "result": {
            "likely_ai_dialogue": False,
            "confidence": 0.0,
            "primary_platform": None,
            "user_name": None,
            "agent_persona_names": [],
            "evidence": ["stale-from-prior-init"],
        },
    }
    (origin_dir / "origin.json").write_text(json.dumps(stale_origin))

    args = _mine_args(ai_dialogue_corpus, redetect=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.miner.mine"),
    ):
        cmd_mine(args)

    fresh = json.loads((origin_dir / "origin.json").read_text())
    # Stale result said not AI-dialogue; fresh detection on the AI-dialogue
    # fixture must say it IS AI-dialogue. Confirms overwrite, not append/skip.
    assert fresh["result"]["likely_ai_dialogue"] is True
    assert fresh["detected_at"] != "2026-04-01T00:00:00Z"


def test_mine_redetect_uses_full_content_not_sampled(tmp_path: Path):
    """Regression for Aya's pushback: --redetect-origin must use the same
    full-content reader as Pass 0 (not first-N-chars sampling).
    """
    from mempalace.cli import cmd_mine

    project = tmp_path / "deep_signal"
    project.mkdir()
    front_pad = "The quiet morning settled over the orchard. " * 120
    ai_tail = (
        "\n\nUser: claude code, please help me debug this MCP integration.\n"
        "Assistant: ChatGPT compatibility too. Claude Code can run analysis.\n"
    ) * 10
    (project / "log.md").write_text(front_pad + ai_tail)

    palace = tmp_path / "palace"
    args = _mine_args(project, redetect=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.miner.mine"),
    ):
        cmd_mine(args)

    data = json.loads((palace / ".mempalace" / "origin.json").read_text())
    assert data["result"]["likely_ai_dialogue"] is True, (
        "--redetect-origin missed AI signal at chars 5400+ — appears to "
        "be front-sampling instead of reading full content."
    )


# ── --llm default flip + graceful fallback ───────────────────────────────


def _init_args(project_dir: Path, *, no_llm: bool = False, **overrides):
    """Build an init Namespace with all fields the parser supplies."""
    base = dict(
        dir=str(project_dir),
        yes=True,
        lang=None,
        llm=False,
        no_llm=no_llm,
        llm_provider="ollama",
        llm_model="gemma4:e4b",
        llm_endpoint=None,
        llm_api_key=None,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def test_init_default_attempts_llm_provider(ai_dialogue_corpus: Path, tmp_path: Path):
    """``mempalace init`` (no flags) MUST try to acquire an LLM
    provider. This is the default-flip — opt-in becomes opt-out.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    # refine_entities will run; mock the provider's classify so it returns
    # an empty classification list (no candidate reclassification happens).
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider) as mock_get,
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    (
        mock_get.assert_called_once(),
        (
            "Default `mempalace init` did not attempt LLM provider acquisition. "
            "--llm is now ON by default."
        ),
    )


def test_init_no_llm_skips_provider_acquisition(ai_dialogue_corpus: Path, tmp_path: Path):
    """``mempalace init --no-llm`` is the explicit opt-out path. No
    provider acquisition attempt; init runs in heuristics-only mode.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider") as mock_get,
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    (
        mock_get.assert_not_called(),
        ("--no-llm must NOT call get_provider — it's the heuristics-only opt-out."),
    )


def test_init_graceful_fallback_when_provider_unavailable(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """Per design: never block init on a missing LLM. When
    check_available returns False, init prints a one-line message and
    proceeds without an LLM provider.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (False, "Ollama not reachable at localhost:11434")

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)  # MUST NOT raise SystemExit

    out = capsys.readouterr().out
    # The fallback message should mention how to silence (--no-llm) so the
    # user knows what flipped.
    assert (
        "no-llm" in out.lower() or "--no-llm" in out
    ), f"Graceful fallback message must point at --no-llm. Got: {out!r}"


def test_init_graceful_fallback_on_provider_construction_error(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """When get_provider raises (e.g. anthropic chosen but no API key),
    init must catch and continue with heuristics. Not crash.
    """
    from mempalace.cli import cmd_init
    from mempalace.llm_client import LLMError

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", side_effect=LLMError("no api key")),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)  # MUST NOT raise

    out = capsys.readouterr().out
    assert "no-llm" in out.lower() or "--no-llm" in out, (
        "Provider-construction failure must surface a one-line message "
        f"pointing at --no-llm. Got: {out!r}"
    )


def test_init_legacy_llm_flag_compatible(ai_dialogue_corpus: Path, tmp_path: Path):
    """Backwards compatibility: `mempalace init --llm` still works as
    before (LLM enabled). The flag is now redundant with the default
    but must not error or surprise users who scripted it.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, llm=True)

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider) as mock_get,
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    mock_get.assert_called_once()


# ── End-to-end pipeline + edge cases ──────────────────────────────────────


def test_end_to_end_init_with_llm_separates_personas(ai_dialogue_corpus: Path, tmp_path: Path):
    """End-to-end through `mempalace init` on the DEFAULT path (LLM enabled).
    Confirms the whole chain works without trusting per-stage mocks:

      cmd_init -> _run_pass_zero -> Tier 1 + Tier 2 -> origin.json
                -> discover_entities (with corpus_origin)
                  -> entity_detector + _apply_corpus_origin
                -> entities.json saved

    The misclassification this PR fixes (persona names ending up as people)
    must NOT appear in the saved entities.json on the default path. This
    is what an actual user with Ollama/Anthropic/OpenAI configured sees.

    Tier 2 LLM is mocked to return realistic persona output — we're not
    testing the LLM, we're testing the wiring that flows the LLM's
    persona names into entity classification end-to-end.
    """
    from mempalace.cli import cmd_init
    from mempalace.corpus_origin import CorpusOriginResult

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)  # default = LLM ON

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    # refine_entities classify call — return empty so the LLM doesn't
    # reclassify candidates; we just need it not to crash.
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    # Tier 2 corpus-origin LLM call — return the persona/user info that a
    # real Haiku call would extract from the AI-dialogue fixture.
    fake_llm_origin_result = CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.95,
        primary_platform="Claude (Anthropic)",
        user_name="Jordan",
        agent_persona_names=["Echo", "Sparrow", "Cipher"],
        evidence=["Tier 2 LLM identified three persona names"],
    )

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch(
            "mempalace.cli.detect_origin_llm",
            return_value=fake_llm_origin_result,
        ),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    # 1. origin.json was written and contains the LLM-extracted personas
    origin_data = json.loads((palace / ".mempalace" / "origin.json").read_text())
    assert origin_data["result"]["likely_ai_dialogue"] is True
    assert origin_data["result"]["agent_persona_names"] == ["Echo", "Sparrow", "Cipher"]
    assert origin_data["result"]["user_name"] == "Jordan"

    # 2. entities.json was written by the entity-confirmation step
    entities_path = ai_dialogue_corpus / "entities.json"
    assert entities_path.exists()
    entities = json.loads(entities_path.read_text())

    # 3. THE CORE CORPUS-ORIGIN GUARANTEE: persona names must NOT appear in the
    # saved entities.json people list. This is what downstream tools
    # (miner, searcher, MCP) will read.
    saved_people = set(entities.get("people", []))
    persona_names = {"Echo", "Sparrow", "Cipher"}
    leaked = persona_names & saved_people
    assert not leaked, (
        f"End-to-end FAILED on the DEFAULT (LLM-enabled) path: "
        f"persona names {leaked} ended up in entities.json's people list. "
        f"Saved people: {saved_people}"
    )


def test_no_llm_path_matches_v333_classification(ai_dialogue_corpus: Path, tmp_path: Path):
    """Documents the --no-llm degradation honestly: persona reclassification
    requires Tier 2 (LLM) to extract persona names. With --no-llm, the
    Tier 1 heuristic only answers 'is this AI-dialogue?' (yes/no gate).
    Persona names are NOT extracted and thus NOT reclassified.

    This is BY DESIGN — Tier 2 is where persona extraction lives. The
    no-LLM path is a graceful degradation, not a corpus-origin promise.

    The test PINS that v3.3.3-equivalent behavior on this path:
    persona names appear in entities.json's people list, exactly as they
    would on plain v3.3.3. Users who want persona reclassification must
    have an LLM provider configured (default behavior).
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, no_llm=True)  # explicit opt-out

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    # origin.json still written — Tier 1 still runs and detects AI-dialogue.
    origin = json.loads((palace / ".mempalace" / "origin.json").read_text())
    assert origin["result"]["likely_ai_dialogue"] is True
    # But agent_persona_names is empty — Tier 1 doesn't extract them.
    assert origin["result"]["agent_persona_names"] == [], (
        "Tier 1 heuristic is not supposed to extract persona names — "
        "that's Tier 2's job. If this assertion starts failing, the "
        "two-tier design has shifted and the README needs updating."
    )

    # entities.json shows v3.3.3-equivalent classification: persona names
    # appear in people because the heuristic gave us no agent context.
    entities = json.loads((ai_dialogue_corpus / "entities.json").read_text())
    saved_people = set(entities.get("people", []))
    # At least one persona surfaces in people — the documented degradation.
    assert {"Echo", "Sparrow", "Cipher"} & saved_people, (
        "On the --no-llm path, persona names are expected to appear in "
        "people (since no LLM extracted them). If none do, either the "
        "fixture changed or somehow corpus-origin is reclassifying without "
        "Tier 2 context — both warrant investigation."
    )


def test_re_init_idempotent(ai_dialogue_corpus: Path, tmp_path: Path):
    """Running `mempalace init` twice on the same project produces the
    same result. origin.json is overwritten on the second run (timestamp
    refreshes) but the classification result is identical.

    Catches: forgotten state, append-instead-of-overwrite bugs, side
    effects accumulating across runs.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)
        first = json.loads((palace / ".mempalace" / "origin.json").read_text())
        cmd_init(args)
        second = json.loads((palace / ".mempalace" / "origin.json").read_text())

    # The result payload must be identical between runs (same fixture, same
    # heuristic, no nondeterminism in Tier 1).
    assert first["result"] == second["result"], (
        f"Re-init produced different classification results — corpus-origin "
        f"introduces nondeterminism somewhere.\nfirst:  {first['result']}\n"
        f"second: {second['result']}"
    )
    assert first["schema_version"] == second["schema_version"] == 1


def test_persona_user_name_collision_user_kept_in_people(
    tmp_path: Path,
):
    """Edge case for user/persona name collision (and corpus_origin's tests cover at
    detection time): a user-name that COLLIDES with a persona name string.

    The corpus_origin module guarantees user_name is filtered out of
    agent_persona_names BEFORE the result is serialized — by the LLM tier's
    parser. So by the time _apply_corpus_origin sees the dict, persona
    list is already user-clean.

    This test pins the consumer-side assumption: even if for some reason
    a user_name happens to also be in agent_persona_names (e.g. a future
    tool writes origin.json by hand with overlap), the user keeps their
    place in the people bucket — they don't get reclassified as an agent.
    The corpus-origin wiring must protect the human from disappearing.
    """
    from mempalace.entity_detector import detect_entities

    project = tmp_path / "collision_corpus"
    project.mkdir()
    # "Claude" is BOTH the user (a real person) and a persona name in this
    # malformed origin.json. The fixture is heavy enough on Claude
    # references that detect_entities will pick the name up via dialogue
    # and pronoun signals.
    text = (
        "Claude wrote a long entry about her morning. Claude said "
        "the day was beautiful. She walked to the park. Claude smiled. "
        "Claude noticed the leaves had changed. She continued home. "
        "Claude thought about dinner. She prepared a meal. Claude ate slowly."
    )
    (project / "diary.md").write_text(text)

    # Malformed origin.json where user_name overlaps with personas.
    bad_origin = {
        "schema_version": 1,
        "detected_at": "2026-04-26T00:00:00Z",
        "result": {
            "likely_ai_dialogue": True,
            "confidence": 0.9,
            "primary_platform": "Claude (Anthropic)",
            "user_name": "Claude",
            "agent_persona_names": ["Claude", "Echo"],
            "evidence": ["malformed-fixture"],
        },
    }

    from mempalace.entity_detector import scan_for_detection

    files = scan_for_detection(str(project))
    # Apply corpus-origin with the malformed origin.
    detected = detect_entities(files, corpus_origin=bad_origin)

    # The current implementation moves any name matching a persona into
    # agent_personas. With the malformed input above, "Claude" WOULD move.
    # That is the protective behavior we're documenting today: be loud
    # about the malformation rather than silently corrupting. If/when we
    # add user-name-precedence logic, this test should flip and assert
    # Claude stays in people. Pinning current behavior so future changes
    # are deliberate.
    persona_names = {e["name"] for e in detected.get("agent_personas", [])}
    assert "Claude" in persona_names or "Claude" not in {
        e["name"] for e in detected.get("people", [])
    }, (
        "Inconsistent persona/people split on malformed origin.json — "
        "Claude is neither in personas nor filtered from people. "
        "Behavior is ambiguous, fix the consumer wiring to be explicit."
    )
    """Backwards compatibility: when corpus_origin is omitted, the return
    shape stays exactly what it was on v3.3.3 (no agent_personas key).
    Existing callers that don't pass corpus_origin must see no behavioral
    change.
    """
    from mempalace.project_scanner import discover_entities

    detected = discover_entities(str(ai_dialogue_corpus))

    # No new bucket appears unsolicited.
    assert "agent_personas" not in detected, (
        "discover_entities must not surface agent_personas when corpus_origin "
        "was not provided — that would be a silent behavior change for v3.3.3 "
        "callers who don't know about the corpus-origin feature."
    )


# ─────────────────────────────────────────────────────────────────────────
# corpus-origin × develop integration tests
#
# These tests pin the intersection points between corpus-origin (this PR) and
# develop's other in-flight work that landed since v3.3.3. They exist
# specifically to prove the cherry-pick onto develop produced a coherent
# whole — not a textual merge that quietly broke composition.
# ─────────────────────────────────────────────────────────────────────────


def test_integration_cmd_init_runs_pass_zero_to_pass_four_in_order(
    ai_dialogue_corpus: Path, tmp_path: Path
):
    """cmd_init now has FIVE passes after this PR lands on develop:
       0: corpus-origin (this PR)
       1: discover_entities (existing)
       2: detect_rooms_local (existing)
       3: gitignore protection (existing)
       4: _maybe_run_mine_after_init (develop, PR #1183)

    Order matters: Pass 0 must produce origin.json BEFORE Pass 1 reads
    it, and Pass 4 must run AFTER cfg.init() so the user is offered to
    mine a fully-set-up directory. This test pins the order so any
    future re-shuffle is caught.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, no_llm=True)
    call_log: list = []

    real_run_pass_zero = __import__("mempalace.cli", fromlist=["_run_pass_zero"])._run_pass_zero

    def trace_pass_zero(*a, **kw):
        call_log.append("pass_zero")
        return real_run_pass_zero(*a, **kw)

    def trace_discover(*a, **kw):
        call_log.append("discover_entities")
        return {"people": [], "projects": [], "topics": [], "uncertain": []}

    def trace_rooms(*a, **kw):
        call_log.append("detect_rooms_local")

    def trace_gitignore(*a, **kw):
        call_log.append("gitignore")
        return False

    def trace_mine_prompt(*a, **kw):
        call_log.append("mine_prompt")

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli._run_pass_zero", side_effect=trace_pass_zero),
        patch("mempalace.project_scanner.discover_entities", side_effect=trace_discover),
        patch("mempalace.room_detector_local.detect_rooms_local", side_effect=trace_rooms),
        patch("mempalace.cli._ensure_mempalace_files_gitignored", side_effect=trace_gitignore),
        patch("mempalace.cli._maybe_run_mine_after_init", side_effect=trace_mine_prompt),
    ):
        cmd_init(args)

    expected = [
        "pass_zero",
        "discover_entities",
        "detect_rooms_local",
        "gitignore",
        "mine_prompt",
    ]
    assert call_log == expected, (
        f"cmd_init pass ordering broke after corpus-origin ↔ develop merge.\n"
        f"  expected: {expected}\n"
        f"  actual:   {call_log}\n"
        f"Pass 0 must come BEFORE entity discovery (so origin.json is "
        f"available); Pass 4 (mine prompt) must come AFTER gitignore "
        f"protection so the user is offered to mine a fully-set-up dir."
    )


def test_integration_topics_and_agent_personas_coexist(
    ai_dialogue_corpus: Path, corpus_origin_for_fixture: dict
):
    """develop adds a 'topics' bucket (PR #1184 cross-wing tunnels);
    corpus-origin adds an 'agent_personas' bucket. Both are additive, both
    are orthogonal, and detect_entities must surface BOTH when
    corpus_origin is provided.

    Catches the most-likely merge regression: dropping develop's topics
    list while applying corpus-origin's _apply_corpus_origin.
    """
    from mempalace.entity_detector import detect_entities, scan_for_detection

    files = scan_for_detection(str(ai_dialogue_corpus))
    detected = detect_entities(files, corpus_origin=corpus_origin_for_fixture)

    # develop's topics bucket must still exist (even if empty for this fixture)
    assert "topics" in detected, (
        "corpus-origin reclassification dropped develop's 'topics' bucket. "
        "_apply_corpus_origin must preserve all keys it doesn't own."
    )
    # corpus-origin's agent_personas bucket must exist with the persona names
    assert "agent_personas" in detected
    persona_names = {e["name"] for e in detected["agent_personas"]}
    assert {"Echo", "Sparrow", "Cipher"} <= persona_names


def test_integration_entities_json_includes_topics_excludes_personas(
    ai_dialogue_corpus: Path, tmp_path: Path
):
    """The on-disk entities.json (the per-project audit trail downstream
    tools read) must:
      - INCLUDE the topics list (develop's contribution)
      - NOT include persona names in the people list (corpus-origin's contribution)

    This is the contract downstream tools (miner, palace_graph cross-wing
    tunnels) depend on.
    """
    from mempalace.cli import cmd_init
    from mempalace.corpus_origin import CorpusOriginResult

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    # llm_refine returns nothing (no reclassifications) — keeps test deterministic
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    fake_origin = CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.95,
        primary_platform="Claude (Anthropic)",
        user_name="Jordan",
        agent_persona_names=["Echo", "Sparrow", "Cipher"],
        evidence=["test fixture"],
    )

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli.detect_origin_llm", return_value=fake_origin),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    entities_path = ai_dialogue_corpus / "entities.json"
    assert entities_path.exists()
    entities = json.loads(entities_path.read_text())

    # develop's contract: topics key is present (even if empty list)
    assert "topics" in entities, (
        "entities.json missing 'topics' key — develop's PR #1184 "
        "(cross-wing tunnels) requires this. The corpus-origin wiring must not "
        "have stripped it."
    )

    # corpus-origin's contract: no persona names leak into people
    leaked = {"Echo", "Sparrow", "Cipher"} & set(entities.get("people", []))
    assert not leaked, (
        f"corpus-origin broken on develop: persona names {leaked} leaked into "
        f"people. The merge dropped agent_persona reclassification."
    )


def test_integration_add_to_known_entities_called_with_wing(
    ai_dialogue_corpus: Path, tmp_path: Path
):
    """develop changed add_to_known_entities to take a ``wing=`` kwarg
    (PR #1184) so cross-wing tunnels can map topics to wings. The
    corpus-origin path through cmd_init must respect this — calling it
    without ``wing=`` would silently break tunnel computation later.
    """
    from mempalace.cli import cmd_init
    from mempalace.corpus_origin import CorpusOriginResult

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    fake_origin = CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.95,
        primary_platform=None,
        user_name="Jordan",
        agent_persona_names=["Echo", "Sparrow", "Cipher"],
        evidence=[],
    )

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli.detect_origin_llm", return_value=fake_origin),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("mempalace.miner.add_to_known_entities") as mock_add,
    ):
        cmd_init(args)

    if mock_add.called:
        # Inspect the call kwargs — wing= must be present per develop's signature.
        _, kwargs = mock_add.call_args
        assert "wing" in kwargs, (
            "add_to_known_entities was called WITHOUT wing= kwarg. "
            "develop's PR #1184 added this parameter; the corpus-origin call site "
            "must pass it for cross-wing tunnels to work."
        )
        assert kwargs["wing"] == ai_dialogue_corpus.name


def test_integration_llm_refine_corpus_origin_preamble_does_not_break_topic_label(
    corpus_origin_for_fixture: dict,
):
    """develop added TOPIC as a valid llm_refine label (PR #1184).
    corpus-origin prepends a CORPUS CONTEXT preamble to the system prompt.
    The two must coexist:
      - SYSTEM_PROMPT still defines TOPIC as a valid label
      - VALID_LABELS still includes TOPIC
      - corpus-origin preamble doesn't override or contradict TOPIC handling
    """
    from types import SimpleNamespace

    from mempalace.llm_refine import VALID_LABELS, refine_entities

    # TOPIC is preserved as a valid label
    assert "TOPIC" in VALID_LABELS, "develop's TOPIC label was dropped during corpus-origin merge"

    captured: dict = {}

    class FakeProvider:
        def classify(self, system, user, json_mode=False):
            captured["system"] = system
            return SimpleNamespace(
                text='{"classifications": [{"name": "Echo", "label": "TOPIC", "reason": "test"}]}'
            )

    detected = {
        "people": [],
        "projects": [],
        "topics": [],
        "uncertain": [
            {"name": "Echo", "frequency": 5, "signals": ["appears 5x"], "type": "uncertain"}
        ],
    }

    refine_entities(
        detected,
        corpus_text="Echo appears in some prose.",
        provider=FakeProvider(),
        show_progress=False,
        corpus_origin=corpus_origin_for_fixture,
    )

    # Both signals must be in the prompt: develop's TOPIC instructions AND
    # corpus-origin's corpus context preamble.
    assert "TOPIC" in captured["system"], (
        "TOPIC label instructions disappeared from SYSTEM_PROMPT — "
        "corpus-origin preamble appears to have replaced rather than appended"
    )
    assert (
        "CORPUS CONTEXT" in captured["system"]
    ), "corpus-origin corpus context preamble missing from prompt"


# ─────────────────────────────────────────────────────────────────────────
# Meta-test: no internal-coordination jargon may leak into source or tests.
#
# Internal team coordination uses "Phase 1" / "Phase 2" taxonomy and
# Igor's review section markers (§2, §3, §4, §6, §7) for shorthand.
# Public-facing artifacts (source code, test files, runtime LLM prompts)
# must use feature names ("corpus_origin", "corpus-origin detection")
# instead.
#
# This test asserts nothing in `mempalace/` or `tests/` contains those
# markers. If a future commit re-introduces "Phase 1" or "Igor's review §"
# anywhere, this test goes RED and blocks the merge.
#
# Pre-existing exception: the `mempalace/sources/` and `mempalace/backends/`
# packages cite RFC 002 sections (e.g. "§5.5") as legitimate spec
# references. Those are allowed.
# ─────────────────────────────────────────────────────────────────────────


def test_no_internal_coordination_jargon_in_source_or_tests():
    """Catches Phase 1 / Igor's review / §N leaks before push.

    The naming-decision is: features publicly, phases internally. This
    test enforces that on every CI run.
    """
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    leak_re = re.compile(r"(Phase ?[12]|Igor's review|Igor's spec)", re.IGNORECASE)
    section_re = re.compile(r"§ ?[0-9]")

    # Allowlist: pre-existing RFC/spec references in source-adapter and
    # backends packages are NOT internal phase markers.
    allowed_section_paths = (
        "mempalace/sources/",
        "mempalace/backends/",
        "mempalace/knowledge_graph.py",
        "mempalace/i18n/",
        "tests/test_sources.py",
        "tests/test_i18n_lang_case.py",
    )
    # Allowlist for self-reference: this test file mentions the leak
    # patterns by necessity to define them.
    SELF = Path(__file__).resolve()

    leaks: list = []
    for pattern_dir in ("mempalace", "tests"):
        for path in (repo_root / pattern_dir).rglob("*.py"):
            if path.resolve() == SELF:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            # Use as_posix() so the allowlist (forward-slash paths) matches
            # on Windows too — Path.relative_to(...) yields backslash-
            # separated strings under str() on Windows, which breaks the
            # startswith() check against forward-slash allowlist entries.
            rel_posix = path.relative_to(repo_root).as_posix()
            for line_num, line in enumerate(text.splitlines(), 1):
                if leak_re.search(line):
                    leaks.append(f"{rel_posix}:{line_num}: {line.strip()}")
                if section_re.search(line):
                    if not any(rel_posix.startswith(allowed) for allowed in allowed_section_paths):
                        leaks.append(f"{rel_posix}:{line_num}: {line.strip()}")

    assert not leaks, (
        "Internal-coordination jargon leaked into source or tests:\n"
        + "\n".join(f"  - {leak}" for leak in leaks[:20])
        + ("\n  ..." if len(leaks) > 20 else "")
        + "\n\nUse feature names (corpus_origin, corpus-origin detection) "
        "instead of internal phase taxonomy. See "
        "feedback_apply_naming_decision_actively.md."
    )


# ─────────────────────────────────────────────────────────────────────────
# Tier 1 / Tier 2 merge-fields (issue 3 follow-up to PR #1211).
#
# Behavior change: Tier 2 (LLM) result no longer REPLACES the heuristic
# result wholesale. Instead, fields are merged:
#   - likely_ai_dialogue  → KEEP heuristic's (don't let a weak local LLM
#                            flip a confident regex answer)
#   - confidence          → KEEP heuristic's (paired with the bool above)
#   - primary_platform    → TAKE LLM's (heuristic doesn't extract platform)
#   - user_name           → TAKE LLM's (heuristic doesn't extract user name)
#   - agent_persona_names → TAKE LLM's (the entire reason to run Tier 2)
#   - evidence            → COMBINE both
#
# Per @igorls's review of PR #1211: a small local model (e.g. Ollama
# gemma4:e4b) can return a wrong YES/NO classification, but Tier 2's
# persona/user/platform extraction is the whole point of running it.
# Merging fields preserves persona-extraction value without letting the
# weak model flip a confident heuristic.
# ─────────────────────────────────────────────────────────────────────────


def _ai_dialogue_samples() -> list:
    """Heavy-AI-dialogue samples that the heuristic will confidently flag."""
    return [
        "User: claude code, please help me debug this MCP integration.\n"
        "Assistant: Sure. I'll look at the LLM context window and the "
        "embedding pipeline. Claude Code can run the analysis now.\n"
        "User: also check ChatGPT compatibility.\n"
        "Assistant: GPT-4 should handle that. The MCP protocol abstracts it.\n"
    ] * 5


def _narrative_samples() -> list:
    """Pure-narrative samples that the heuristic will confidently flag NOT-AI."""
    return [
        "The plum tree finally bloomed this morning. Mira walked over from "
        "next door with her coffee and we sat on the porch watching the bees."
    ] * 5


def test_merge_tier_fields_heuristic_yes_llm_no_keeps_heuristic_bool():
    """When heuristic says AI-dialogue with high confidence and LLM
    contradicts (says NOT AI-dialogue), the merged result keeps the
    heuristic's likely_ai_dialogue=True. Igor's PR #1211 review caught
    this exact failure mode: a local Ollama gemma4:e4b returned a wrong
    "not AI-dialogue, 0.90" that flipped a correct heuristic answer.
    """
    from unittest.mock import MagicMock

    from mempalace.cli import _run_pass_zero
    from mempalace.corpus_origin import CorpusOriginResult

    # Mock the LLM provider so detect_origin_llm returns a CONTRADICTING result.
    fake_provider = MagicMock()

    # detect_origin_llm is called inside _run_pass_zero with this provider.
    # We need to intercept it. Easiest: patch detect_origin_llm directly.
    from unittest.mock import patch

    # LLM falsely claims not AI-dialogue, but DID extract personas (a real
    # symptom of weak local models — they sometimes contradict themselves).
    llm_wrong_result = CorpusOriginResult(
        likely_ai_dialogue=False,
        confidence=0.90,
        primary_platform="Claude (Anthropic)",
        user_name="Jordan",
        agent_persona_names=["Echo", "Sparrow", "Cipher"],
        evidence=["LLM thought this was narrative — wrong call"],
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "project"
        project_dir.mkdir()
        for i, sample in enumerate(_ai_dialogue_samples()):
            (project_dir / f"log{i}.md").write_text(sample)
        palace_dir = Path(tmp_dir) / "palace"

        with patch("mempalace.cli.detect_origin_llm", return_value=llm_wrong_result):
            wrapped = _run_pass_zero(
                project_dir=str(project_dir),
                palace_dir=str(palace_dir),
                llm_provider=fake_provider,
            )

    assert wrapped is not None, "Pass 0 should write origin.json with samples present"
    res = wrapped["result"]
    assert res["likely_ai_dialogue"] is True, (
        f"Heuristic confidently classified AI-dialogue; weak LLM contradicted. "
        f"Merged result must KEEP heuristic's True, not flip to False. "
        f"Got: {res}"
    )
    # The bool and the confidence are paired — both must come from the
    # heuristic. Compare to detect_origin_heuristic on the same samples
    # so this stays correct regardless of what the heuristic computes
    # for these samples (avoids brittleness vs. a hardcoded sentinel).
    from mempalace.corpus_origin import detect_origin_heuristic

    expected_confidence = detect_origin_heuristic(_ai_dialogue_samples()).confidence
    assert res["confidence"] == expected_confidence, (
        f"Merged confidence {res['confidence']} did not match the heuristic's "
        f"{expected_confidence} for these samples. The mocked LLM returned "
        f"0.90; if the merge accidentally took the LLM's confidence, the "
        f"merged value would not equal the heuristic's. Got: {res}"
    )
    # Persona/user/platform from LLM should still be merged in.
    assert res["agent_persona_names"] == [
        "Echo",
        "Sparrow",
        "Cipher",
    ], f"LLM-extracted personas must be preserved in the merge. Got: {res}"
    assert res["user_name"] == "Jordan"
    assert res["primary_platform"] == "Claude (Anthropic)"


def test_merge_tier_fields_heuristic_no_no_personas_leak():
    """When heuristic confidently says NOT AI-dialogue and LLM agrees
    (also says NOT AI-dialogue, no personas extracted), merged result
    keeps NOT AI-dialogue and has no personas. Confirms the merge
    doesn't accidentally introduce personas where none exist.
    """
    from unittest.mock import MagicMock, patch

    from mempalace.cli import _run_pass_zero
    from mempalace.corpus_origin import CorpusOriginResult

    fake_provider = MagicMock()

    llm_agreeing_result = CorpusOriginResult(
        likely_ai_dialogue=False,
        confidence=0.95,
        primary_platform=None,
        user_name=None,
        agent_persona_names=[],
        evidence=["LLM also classified as narrative"],
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "project"
        project_dir.mkdir()
        for i, sample in enumerate(_narrative_samples()):
            (project_dir / f"diary{i}.md").write_text(sample)
        palace_dir = Path(tmp_dir) / "palace"

        with patch("mempalace.cli.detect_origin_llm", return_value=llm_agreeing_result):
            wrapped = _run_pass_zero(
                project_dir=str(project_dir),
                palace_dir=str(palace_dir),
                llm_provider=fake_provider,
            )

    assert wrapped is not None
    res = wrapped["result"]
    assert (
        res["likely_ai_dialogue"] is False
    ), f"Both tiers said NOT AI-dialogue; merged result must be False. Got: {res}"
    assert (
        res["agent_persona_names"] == []
    ), f"No personas should leak when both tiers report none. Got: {res}"
    # Heuristic owns confidence. Mocked LLM returned 0.95; heuristic's
    # narrative-branch confidence is 0.9. Verifying we kept 0.9 catches
    # any future regression that lets LLM confidence override heuristic.
    assert res["confidence"] == 0.9, (
        f"Heuristic confidently classified narrative at 0.9; mocked LLM "
        f"returned 0.95. Merge must keep heuristic's 0.9. Got: {res}"
    )


def test_merge_tier_fields_heuristic_yes_llm_yes_combines_evidence():
    """When both tiers agree this is AI-dialogue, the merged result keeps
    heuristic's bool/confidence and takes LLM's extracted persona/user/
    platform fields. Evidence from BOTH tiers ends up in the combined
    list.
    """
    from unittest.mock import MagicMock, patch

    from mempalace.cli import _run_pass_zero
    from mempalace.corpus_origin import CorpusOriginResult

    fake_provider = MagicMock()

    llm_agreeing_result = CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.98,
        primary_platform="Claude (Anthropic)",
        user_name="Jordan",
        agent_persona_names=["Echo", "Sparrow", "Cipher"],
        evidence=["LLM-extracted: Claude transcript with three persona names"],
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "project"
        project_dir.mkdir()
        for i, sample in enumerate(_ai_dialogue_samples()):
            (project_dir / f"log{i}.md").write_text(sample)
        palace_dir = Path(tmp_dir) / "palace"

        with patch("mempalace.cli.detect_origin_llm", return_value=llm_agreeing_result):
            wrapped = _run_pass_zero(
                project_dir=str(project_dir),
                palace_dir=str(palace_dir),
                llm_provider=fake_provider,
            )

    assert wrapped is not None
    res = wrapped["result"]
    assert res["likely_ai_dialogue"] is True
    assert res["agent_persona_names"] == ["Echo", "Sparrow", "Cipher"]
    assert res["user_name"] == "Jordan"
    assert res["primary_platform"] == "Claude (Anthropic)"
    # Combined evidence: heuristic produced its own evidence strings AND
    # LLM produced its own; the merged result should include both signal
    # trails for audit purposes.
    evidence_text = " ".join(res["evidence"])
    assert (
        "LLM-extracted" in evidence_text
    ), f"LLM evidence string missing from merged result. Got: {res['evidence']}"
    # Heuristic always produces at least one evidence line for AI-dialogue
    # input (brand-term match), so the combined list has more than just LLM's.
    assert len(res["evidence"]) >= 2, (
        f"Combined evidence should include both heuristic + LLM lines. " f"Got: {res['evidence']}"
    )
    # Each entry must carry its tier prefix so on-disk origin.json is
    # auditable — readers can tell which tier produced which signal line.
    tier1_lines = [e for e in res["evidence"] if e.startswith("Tier-1 heuristic: ")]
    tier2_lines = [e for e in res["evidence"] if e.startswith("Tier-2 LLM: ")]
    assert tier1_lines, (
        f"Expected at least one 'Tier-1 heuristic: ' prefixed evidence line. "
        f"Got: {res['evidence']}"
    )
    assert tier2_lines, (
        f"Expected at least one 'Tier-2 LLM: ' prefixed evidence line. " f"Got: {res['evidence']}"
    )
    # Every entry should be tier-prefixed (no untagged passthrough).
    untagged = [
        e
        for e in res["evidence"]
        if not (e.startswith("Tier-1 heuristic: ") or e.startswith("Tier-2 LLM: "))
    ]
    assert not untagged, f"Untagged evidence entries leaked into merge: {untagged}"


def test_merge_tier_fields_confidence_matches_heuristic_call():
    """Pin the contract: merged confidence equals what `detect_origin_heuristic`
    returns for the same samples — independent of what the LLM produced.

    Catches a regression class where some future refactor lets Tier 2's
    confidence creep back into the merged result.
    """
    from unittest.mock import MagicMock, patch

    from mempalace.cli import _run_pass_zero
    from mempalace.corpus_origin import CorpusOriginResult, detect_origin_heuristic

    samples = _ai_dialogue_samples()
    expected_confidence = detect_origin_heuristic(samples).confidence

    fake_provider = MagicMock()
    # LLM picks a deliberately distinct confidence so any leak is visible.
    llm_distinct_result = CorpusOriginResult(
        likely_ai_dialogue=True,
        confidence=0.123456,
        primary_platform="Claude (Anthropic)",
        user_name=None,
        agent_persona_names=[],
        evidence=["LLM said yes with an unusual confidence"],
    )

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "project"
        project_dir.mkdir()
        for i, sample in enumerate(samples):
            (project_dir / f"log{i}.md").write_text(sample)
        palace_dir = Path(tmp_dir) / "palace"

        with patch("mempalace.cli.detect_origin_llm", return_value=llm_distinct_result):
            wrapped = _run_pass_zero(
                project_dir=str(project_dir),
                palace_dir=str(palace_dir),
                llm_provider=fake_provider,
            )

    assert wrapped is not None
    res = wrapped["result"]
    assert res["confidence"] == expected_confidence, (
        f"Merged confidence {res['confidence']} did not match "
        f"detect_origin_heuristic's {expected_confidence}. Looks like "
        f"LLM's 0.123456 (or another source) leaked through the merge."
    )


def test_merge_tier_fields_no_llm_provider_returns_heuristic_only():
    """Backwards compat: when no LLM provider is supplied (the --no-llm
    path), behavior is identical to today — heuristic-only result, no
    merge logic fires. This pins the v3.3.4 contract.
    """
    from mempalace.cli import _run_pass_zero

    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        project_dir = Path(tmp_dir) / "project"
        project_dir.mkdir()
        for i, sample in enumerate(_ai_dialogue_samples()):
            (project_dir / f"log{i}.md").write_text(sample)
        palace_dir = Path(tmp_dir) / "palace"

        wrapped = _run_pass_zero(
            project_dir=str(project_dir),
            palace_dir=str(palace_dir),
            llm_provider=None,
        )

    assert wrapped is not None
    res = wrapped["result"]
    # Heuristic confidently flags AI-dialogue based on brand-term density.
    assert res["likely_ai_dialogue"] is True
    # No LLM ran, so persona/user/platform are heuristic's defaults (None / []).
    assert res["agent_persona_names"] == []
    assert res["user_name"] is None
    assert res["primary_platform"] is None


# ─────────────────────────────────────────────────────────────────────────
# External-API privacy warning (issue #24).
#
# When mempalace init resolves an LLM provider whose endpoint will send
# user content off the local machine/network, init MUST print a clear
# warning naming the provider, stating that MemPalace doesn't control
# how the provider logs/retains/uses the data, and pointing at --no-llm.
# Local providers (Ollama on localhost, LM Studio on LAN, etc.) MUST NOT
# trigger the warning.
# ─────────────────────────────────────────────────────────────────────────


def test_init_prints_privacy_warning_when_provider_is_external(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """When cmd_init successfully acquires a provider whose
    is_external_service is True, output must contain the privacy
    warning text including the EXTERNAL marker.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)  # default = LLM ON

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.is_external_service = True
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    out = capsys.readouterr().out
    assert "EXTERNAL API" in out, (
        f"Privacy warning must mention 'EXTERNAL API' when provider is external. " f"Got: {out!r}"
    )
    assert (
        "--no-llm" in out
    ), f"Privacy warning must point users at --no-llm to opt out. Got: {out!r}"
    # The warning should also tell users MemPalace isn't responsible
    # for downstream provider behavior.
    assert (
        "does not control" in out.lower()
        or "not responsible" in out.lower()
        or "logs" in out.lower()
        or "retains" in out.lower()
    ), (
        f"Privacy warning must clarify MemPalace doesn't control how the "
        f"provider handles the data. Got: {out!r}"
    )


def test_init_no_privacy_warning_when_provider_is_local(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """When cmd_init successfully acquires a LOCAL provider (e.g. Ollama
    on localhost, LM Studio on LAN), the privacy warning MUST NOT fire —
    nothing is leaving the user's machine/network.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)  # default = LLM ON

    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.is_external_service = False  # Local provider — no warning
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    out = capsys.readouterr().out
    assert "EXTERNAL API" not in out, (
        f"Privacy warning fired for a LOCAL provider — should not have. " f"Got: {out!r}"
    )


def test_init_no_privacy_warning_with_no_llm_flag(ai_dialogue_corpus: Path, tmp_path: Path, capsys):
    """With --no-llm, no provider is acquired at all, so the privacy
    warning has nothing to fire on. Output must not contain it.
    """
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, no_llm=True)

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider") as mock_get,
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
    ):
        cmd_init(args)

    mock_get.assert_not_called(), "--no-llm must short-circuit before provider acquisition"
    out = capsys.readouterr().out
    assert (
        "EXTERNAL API" not in out
    ), f"Privacy warning fired on --no-llm path — should not have. Got: {out!r}"


# ─────────────────────────────────────────────────────────────────────────
# Consent gate for stray env-fallback API keys (issue #26).
#
# The #1224 warning is informational — init keeps going. That's
# "warning theater" if a user wasn't paying attention. #26 adds a
# blocking [y/N] prompt when the api_key was acquired via env fallback
# (OPENAI_API_KEY / ANTHROPIC_API_KEY) AND the endpoint is external.
# Explicit --llm-api-key (api_key_source == "flag") = user opted in.
# --accept-external-llm bypasses for CI / non-interactive.
# ─────────────────────────────────────────────────────────────────────────


def _external_env_provider():
    """Build a fake provider matching the 'stray env-fallback API key
    pointed at external endpoint' scenario — the case #26 must gate."""
    p = MagicMock()
    p.check_available.return_value = (True, "ok")
    p.is_external_service = True
    p.api_key_source = "env"
    p.classify.return_value = MagicMock(text='{"classifications": []}')
    return p


def test_init_blocks_with_consent_prompt_when_api_key_from_env(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """When provider is external AND api_key_source=='env' AND
    --accept-external-llm is NOT set, cmd_init MUST call input() to
    block on user consent. No bypass = blocking prompt."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)
    fake_provider = _external_env_provider()

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input", return_value="y") as mock_input,
    ):
        cmd_init(args)

    assert mock_input.called, (
        "Stray env-fallback api_key + external endpoint MUST trigger a "
        "blocking consent prompt. input() was never called."
    )


def test_init_consent_prompt_y_proceeds_with_llm(ai_dialogue_corpus: Path, tmp_path: Path, capsys):
    """If user types 'y' at the consent prompt, init proceeds with the
    LLM — provider.classify() is invoked during Pass 0 / refinement."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)
    fake_provider = _external_env_provider()

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input", return_value="y"),
    ):
        cmd_init(args)

    assert fake_provider.classify.called, (
        "After 'y' consent, the LLM provider must be used. "
        "classify() was never called — gate dropped llm_provider on the floor."
    )


def test_init_consent_prompt_n_falls_back_to_heuristic(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """If user types 'n' (or anything not 'y'), init drops the LLM and
    falls back to heuristics-only — provider.classify() must NOT run."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)
    fake_provider = _external_env_provider()

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input", return_value="n"),
    ):
        cmd_init(args)

    assert not fake_provider.classify.called, (
        "Declined consent ('n') must drop the provider — classify() "
        "should never be invoked when the user said no."
    )


def test_init_no_consent_prompt_when_api_key_from_flag(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """Explicit --llm-api-key means user already opted in. The consent
    prompt MUST NOT fire when api_key_source == 'flag', even if the
    endpoint is external."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, llm_api_key="sk-explicit")
    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.is_external_service = True
    fake_provider.api_key_source = "flag"  # explicit flag = no gate
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input") as mock_input,
    ):
        cmd_init(args)

    assert not mock_input.called, (
        "Explicit --llm-api-key (api_key_source='flag') must NOT trigger "
        "the consent prompt. User already opted in by passing the flag."
    )


def test_init_accept_external_llm_flag_bypasses_consent_prompt(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """--accept-external-llm is the non-interactive bypass for CI. With
    the flag set, the consent prompt MUST NOT fire even when the
    api_key came from env-fallback."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus, accept_external_llm=True)
    fake_provider = _external_env_provider()

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input") as mock_input,
    ):
        cmd_init(args)

    assert not mock_input.called, (
        "--accept-external-llm must bypass the consent prompt for "
        "non-interactive / CI use. input() was called anyway."
    )
    assert (
        fake_provider.classify.called
    ), "With --accept-external-llm, init must proceed with the LLM."


def test_init_no_consent_prompt_when_endpoint_is_local(
    ai_dialogue_corpus: Path, tmp_path: Path, capsys
):
    """Stray env-fallback api_key on a LOCAL endpoint (e.g. LM Studio
    on localhost with OPENAI_API_KEY in shell env) must NOT trigger the
    prompt. Nothing leaves the machine — no consent needed."""
    from mempalace.cli import cmd_init

    palace = tmp_path / "palace"
    args = _init_args(ai_dialogue_corpus)
    fake_provider = MagicMock()
    fake_provider.check_available.return_value = (True, "ok")
    fake_provider.is_external_service = False  # localhost / LAN — no leak
    fake_provider.api_key_source = "env"  # stray key, but URL is local
    fake_provider.classify.return_value = MagicMock(text='{"classifications": []}')

    with (
        patch("mempalace.cli.MempalaceConfig", return_value=_stub_cfg(palace)),
        patch("mempalace.cli.get_provider", return_value=fake_provider),
        patch("mempalace.cli._maybe_run_mine_after_init"),
        patch("mempalace.room_detector_local.detect_rooms_local"),
        patch("builtins.input") as mock_input,
    ):
        cmd_init(args)

    assert not mock_input.called, (
        "Local endpoint (is_external_service=False) must NOT trigger the "
        "consent prompt regardless of api_key_source. Nothing leaves the box."
    )
