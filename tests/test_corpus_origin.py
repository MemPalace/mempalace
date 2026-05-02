"""Tests for corpus_origin detection.

The corpus-origin detector answers ONE foundational question before any
downstream Pass 2 classification runs:

    "Is this corpus a record of AI-agent dialogue, and if so, which platform
     and what persona names has the user assigned to the agent?"

Detection is two-tier:
  - Tier 1: cheap content-aware heuristic (grep for well-known AI terms
    and turn markers). No API calls. Always runs.
  - Tier 2: LLM-assisted confirmation + persona extraction. Takes a small
    sample of drawer texts and uses Haiku's pre-trained world knowledge
    about Claude/ChatGPT/Gemini/etc. to confirm platform + identify
    persona-names the user assigned to the agent.

Default stance: "this IS an AI-dialogue corpus" unless strong evidence
otherwise. False-negative (missing an AI corpus) is catastrophic for
downstream classification; false-positive is recoverable via per-drawer
voice-profile detection in later passes.

TDD: these tests fail until mempalace/corpus_origin.py is implemented."""

from mempalace.corpus_origin import (
    CorpusOriginResult,
    detect_origin_heuristic,
    detect_origin_llm,
)


# ── Tier 1: heuristic (no LLM) ────────────────────────────────────────────


class TestHeuristic:
    def test_claude_heavy_corpus_detected(self):
        """A corpus with abundant Claude references + turn markers should
        be confidently detected as AI-dialogue."""
        samples = [
            "user: hey Claude, can you help me\nassistant: sure, what do you need\n",
            "I was talking to Claude Opus about the MCP server setup",
            "Sonnet 4.5 handled this better than Haiku 4.5 did",
            "claude mcp add mempalace -- mempalace-mcp",
            "human: what's up\nassistant: I'm happy to help",
        ]
        result = detect_origin_heuristic(samples)
        assert result.likely_ai_dialogue is True
        assert result.confidence >= 0.8
        assert (
            "Claude" in " ".join(result.evidence) or "claude" in " ".join(result.evidence).lower()
        )

    def test_gpt_corpus_detected(self):
        samples = [
            "I asked ChatGPT to summarize my paper",
            "The GPT-4 response was surprisingly good",
            "user: explain quantum computing\nassistant: quantum computing uses qubits",
            "OpenAI's model was able to help with the code",
        ]
        result = detect_origin_heuristic(samples)
        assert result.likely_ai_dialogue is True
        assert any("GPT" in e or "ChatGPT" in e or "OpenAI" in e for e in result.evidence)

    def test_pure_narrative_corpus_detected_as_not_ai(self):
        """A story/journal corpus with no AI signals should be flagged
        not-AI (default stance flipped only with evidence)."""
        samples = [
            "Today the cat finally ventured into the garden. The dog watched.",
            "The morning light came through the window as I wrote.",
            "Chapter 3: The Reckoning. It was a dark and stormy night.",
            "My father's old journal described the same field in 1972.",
        ]
        result = detect_origin_heuristic(samples)
        assert result.likely_ai_dialogue is False
        assert result.confidence >= 0.8

    def test_ambiguous_corpus_defaults_to_ai(self):
        """When evidence is thin or mixed, default to assuming AI-dialogue.
        False-negative is worse than false-positive."""
        samples = [
            "some notes about the meeting today",
            "Later on I went to the store.",
            "Short file with little signal.",
        ]
        result = detect_origin_heuristic(samples)
        # Low signal → default stance is ai_dialogue=True with low confidence
        assert result.likely_ai_dialogue is True
        assert result.confidence <= 0.6
        assert "default-stance" in " ".join(result.evidence).lower()

    def test_turn_markers_alone_sufficient(self):
        """Even without AI brand mentions, strong turn-marker presence
        indicates dialogue structure consistent with AI corpora."""
        samples = [
            "user: hello\nassistant: hi there, how can I help?\nuser: summarize X\nassistant: sure",
            "human: what's the weather\nai: I don't have real-time data\n",
        ]
        result = detect_origin_heuristic(samples)
        assert result.likely_ai_dialogue is True

    # ── Pattern + context (not capitalization, not English-rule) ──────────

    def test_brand_terms_case_insensitive(self):
        """Detection cannot rely on the user typing proper-cased brand names.
        Lowercase 'claude code', 'chatgpt', 'gemini-pro', 'mcp' must trip
        the same as their proper-cased equivalents. NO turn-marker fallback
        in this corpus — the brand matches must do the work."""
        samples = [
            "i love claude code, it just works for refactoring tasks",
            "asked chatgpt to write a regex and it nailed it on the first try",
            "switched to gemini-pro for the long-context summary task last week",
            "added mempalace as an mcp server in my .claude/ settings file",
            "anthropic's haiku model is cheap enough to run on every drawer",
        ]
        result = detect_origin_heuristic(samples)
        assert (
            result.likely_ai_dialogue is True
        ), f"lowercase brand terms missed; evidence: {result.evidence}"
        # Evidence must show MULTIPLE distinct case-insensitive brand matches.
        # 'chatgpt' lowercase only matches under case-insensitive search
        # (the brand list has 'ChatGPT' proper-cased only).
        evidence_str = " ".join(result.evidence).lower()
        matched = sum(t in evidence_str for t in ("chatgpt", "anthropic", "haiku", "gemini-pro"))
        assert (
            matched >= 2
        ), f"case-insensitive brand matches did not fire — only got: {result.evidence}"

    def test_zodiac_corpus_not_flagged_as_ai(self):
        """An astrology forum post with high 'Gemini' density but ZERO
        unambiguous AI signals (no MCP/LLM/ChatGPT/turn markers) must NOT
        be flagged as AI-dialogue. Word-sense disambiguation is required:
        Gemini-the-zodiac-sign vs Gemini-the-AI-platform."""
        samples = [
            "I'm a Gemini sun, Pisces moon, and Leo rising.",
            "Geminis are dreamers and overthinkers — that's the dual nature.",
            "Compatibility between Gemini and Sagittarius is famously strong.",
            "If you're a Gemini, expect Mercury retrograde to hit you hardest.",
            "My horoscope this week says Gemini energy will dominate Wednesday.",
            "The Gemini twins in Greek mythology are Castor and Pollux.",
        ]
        result = detect_origin_heuristic(samples)
        assert (
            result.likely_ai_dialogue is False
        ), f"zodiac corpus wrongly flagged AI; evidence: {result.evidence}"

    def test_french_novel_with_claude_name_not_flagged(self):
        """A French novel where 'Claude' is a character name (Claude is a
        common French masculine name) must NOT trip AI-dialogue detection.
        Disambiguation is by context, not by the presence of the word."""
        samples = [
            "Claude marchait lentement le long de la Seine ce matin-là.",
            "« Claude, tu rentres dîner? » lui demanda sa mère depuis la cuisine.",
            "Pour Claude, l'art de vivre passait avant tout par la patience.",
            "Le vieux Claude se souvenait encore de la guerre, des champs déserts.",
            "Claude ouvrit la fenêtre. Le matin sentait le pain frais et la pluie.",
            "Les amis de Claude s'étaient réunis chez lui pour fêter ses soixante ans.",
        ]
        result = detect_origin_heuristic(samples)
        assert (
            result.likely_ai_dialogue is False
        ), f"French novel wrongly flagged AI; evidence: {result.evidence}"

    def test_poetry_corpus_with_haiku_sonnet_not_flagged(self):
        """A poetry corpus with high 'haiku', 'sonnet', 'opus' density
        (poetic forms / classical music terms) but no AI infrastructure
        terms must NOT be flagged as AI-dialogue."""
        samples = [
            "A haiku is seventeen syllables across three lines: 5-7-5.",
            "Shakespeare's sonnet 18 remains the most quoted in the English canon.",
            "Beethoven's opus 27 includes the Moonlight Sonata.",
            "I wrote three haiku this morning before coffee.",
            "The sonnet form arrived in England via Wyatt and Surrey.",
            "Her first opus, published at twenty, was a song cycle for soprano.",
        ]
        result = detect_origin_heuristic(samples)
        assert (
            result.likely_ai_dialogue is False
        ), f"poetry corpus wrongly flagged AI; evidence: {result.evidence}"

    def test_word_boundary_brand_matching(self):
        """Brand-term matching must use word boundaries. Embedded matches
        inside larger words ('Claudette' → 'Claude', 'opuscule' → 'Opus',
        'sonneteer' → 'Sonnet', 'llamas' → 'Llama', 'bardic' → 'Bard')
        must NOT be counted as brand hits.

        Word boundaries don't change classification on the co-occurrence-
        suppressed cases, but they clean up the evidence strings — false
        matches must not appear in the audit trail. They also prevent
        'Claude Code' from triple-counting as 'Claude Code' + 'Claude'
        overlap."""
        samples = [
            "My grandmother Claudette baked the most beautiful tarts every Sunday.",
            "Two llamas were spotted near the trailhead this morning at sunrise.",
            "Beethoven's opuscule for solo violin remained unpublished for decades.",
            "She studied to become a sonneteer after reading the full Spenser cycle.",
            "Bardic traditions in the Hebrides survived well into the eighteenth century.",
            "The complete opuses of Mozart fill an entire wall of the library.",
        ]
        result = detect_origin_heuristic(samples)
        evidence_str = " ".join(result.evidence).lower()

        # None of the brand terms should show up in evidence — every
        # would-be match is an embedded false-positive that word
        # boundaries should suppress.
        for embedded_term in ("claude", "opus", "sonnet", "llama", "bard"):
            assert f"'{embedded_term}'" not in evidence_str, (
                f"word-boundary bug: '{embedded_term}' falsely matched inside "
                f"a longer word — evidence: {result.evidence}"
            )

        # And classification should be not-AI (no real AI signals present).
        assert (
            result.likely_ai_dialogue is False
        ), f"corpus has no real AI signals; evidence: {result.evidence}"

    def test_ambiguous_brand_with_unambiguous_signal_flagged(self):
        """When an ambiguous brand term ('Gemini') co-occurs with an
        UNAMBIGUOUS AI signal (turn markers, MCP, ChatGPT, Claude Code)
        in the same corpus, the Gemini hits SHOULD count and the corpus
        SHOULD be flagged as AI-dialogue."""
        samples = [
            "Switched the agent from Gemini to ChatGPT mid-session for cost reasons.",
            "Gemini handled the long-context task; user: please summarize\nassistant: here is the summary",
            "user: try Gemini for this\nassistant: running it through gemini-pro now",
            "MCP server config: Gemini as primary, OpenAI as fallback.",
        ]
        result = detect_origin_heuristic(samples)
        assert (
            result.likely_ai_dialogue is True
        ), f"ambiguous+unambiguous co-occurrence missed; evidence: {result.evidence}"


# ── Tier 2: LLM-assisted (mocked) ─────────────────────────────────────────


class _FakeProvider:
    """Minimal stand-in for mempalace's LLMProvider used for testing."""

    def __init__(self, canned_response):
        self._response = canned_response
        self.calls = []

    def classify(self, system, user, json_mode=True):
        self.calls.append({"system": system, "user": user})

        class R:
            text = self._response

        return R()

    def check_available(self):
        return True, "ok"


class TestLLMConfirmation:
    def test_extracts_persona_names_and_platform(self):
        fake_response = """{
          "is_ai_dialogue_corpus": true,
          "confidence": 0.97,
          "primary_platform": "Claude Code (Anthropic CLI)",
          "agent_persona_names": ["Echo", "Sparrow", "Cipher", "Orc"],
          "evidence": [
            "user addresses agent as 'Echo' on assistant turns",
            "Claude Code banner text in samples",
            "references to MCP, CLAUDE.md, hooks"
          ]
        }"""
        provider = _FakeProvider(fake_response)
        samples = [
            "user: hey Echo, what's up\nassistant: I'm here, what do you need\n",
            "Claude Code session banner Sonnet 4.5 Claude Pro",
        ]
        result = detect_origin_llm(samples, provider)
        assert result.likely_ai_dialogue is True
        assert result.confidence >= 0.9
        assert "Echo" in result.agent_persona_names
        assert "Sparrow" in result.agent_persona_names
        assert "Claude" in result.primary_platform

    def test_narrative_corpus_llm_confirms_no_agent(self):
        fake_response = """{
          "is_ai_dialogue_corpus": false,
          "confidence": 0.95,
          "primary_platform": null,
          "agent_persona_names": [],
          "evidence": ["pure narrative prose, no turn markers, no AI terms"]
        }"""
        provider = _FakeProvider(fake_response)
        samples = ["Once upon a time in a small village", "The old woman smiled"]
        result = detect_origin_llm(samples, provider)
        assert result.likely_ai_dialogue is False
        assert result.agent_persona_names == []
        assert result.primary_platform is None

    def test_handles_malformed_llm_response(self):
        """If the LLM returns garbage, fall back gracefully to the
        conservative default (assume AI-dialogue with low confidence)."""
        provider = _FakeProvider("not even close to JSON")
        result = detect_origin_llm(["sample text"], provider)
        # Fallback: conservative default, low confidence
        assert result.likely_ai_dialogue is True
        assert result.confidence <= 0.5
        assert (
            "fallback" in " ".join(result.evidence).lower()
            or "error" in " ".join(result.evidence).lower()
        )

    def test_filters_user_name_out_of_personas(self):
        """Regression test: Haiku sometimes leaks the user's own name into
        agent_persona_names despite the prompt's CRITICAL distinction. The
        parser must strip the user's name from personas if it appears in
        both fields (case-insensitive). The user is the human author of
        the corpus, not an agent persona."""
        fake_response = """{
          "is_ai_dialogue_corpus": true,
          "confidence": 0.97,
          "primary_platform": "Claude (Anthropic)",
          "user_name": "Jordan",
          "agent_persona_names": ["Echo", "Sparrow", "Jordan", "Cipher"],
          "evidence": ["user Jordan talks to agents Echo/Sparrow/Cipher"]
        }"""
        provider = _FakeProvider(fake_response)
        result = detect_origin_llm(["sample"], provider)
        # user_name is exposed in its own field
        assert result.user_name == "Jordan"
        # "Jordan" is filtered out of agent_persona_names
        assert "Jordan" not in result.agent_persona_names
        # Real personas are preserved
        for persona in ("Echo", "Sparrow", "Cipher"):
            assert persona in result.agent_persona_names

    def test_filter_is_case_insensitive(self):
        """The user-name filter works even when the LLM returns a casing
        mismatch between user_name and the personas list."""
        fake_response = """{
          "is_ai_dialogue_corpus": true,
          "confidence": 0.9,
          "primary_platform": "Claude",
          "user_name": "Jordan",
          "agent_persona_names": ["Echo", "jordan", "JORDAN", "Cipher"],
          "evidence": []
        }"""
        provider = _FakeProvider(fake_response)
        result = detect_origin_llm(["sample"], provider)
        # All case-variants of the user's name are filtered
        assert "jordan" not in [p.lower() for p in result.agent_persona_names]
        assert result.agent_persona_names == ["Echo", "Cipher"]

    def test_user_name_field_surfaces_author(self):
        """The user_name field captures the human author of the corpus,
        separate from agent personas. This gives downstream passes a
        clear 'who is the user, who is the agent' distinction."""
        fake_response = """{
          "is_ai_dialogue_corpus": true,
          "confidence": 0.95,
          "primary_platform": "ChatGPT (OpenAI)",
          "user_name": "Sarah",
          "agent_persona_names": ["MyAssistant"],
          "evidence": ["Sarah writes to MyAssistant"]
        }"""
        provider = _FakeProvider(fake_response)
        result = detect_origin_llm(["sample"], provider)
        assert result.user_name == "Sarah"
        assert result.agent_persona_names == ["MyAssistant"]


# ── CorpusOriginResult dataclass ──────────────────────────────────────────


class TestResultDataclass:
    def test_result_has_all_fields(self):
        r = CorpusOriginResult(
            likely_ai_dialogue=True,
            confidence=0.95,
            primary_platform="Claude Code",
            agent_persona_names=["Echo"],
            evidence=["test"],
        )
        assert r.likely_ai_dialogue is True
        assert r.confidence == 0.95
        assert r.primary_platform == "Claude Code"
        assert r.agent_persona_names == ["Echo"]
        assert r.evidence == ["test"]

    def test_result_serializes_to_dict(self):
        r = CorpusOriginResult(
            likely_ai_dialogue=False,
            confidence=0.9,
            primary_platform=None,
            agent_persona_names=[],
            evidence=[],
        )
        d = r.to_dict()
        assert d["likely_ai_dialogue"] is False
        assert d["primary_platform"] is None
        assert d["agent_persona_names"] == []
