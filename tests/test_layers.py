"""Tests for mempalace.layers — Layer0, Layer1, Layer2, Layer3, MemoryStack."""

import os
from unittest.mock import MagicMock, patch

from mempalace.layers import (
    L1_MAX_PER_SOURCE,
    Layer0,
    Layer1,
    Layer2,
    Layer3,
    MemoryStack,
    _l1_prose_ratio,
    _l1_salience,
    _l1_select,
    _l1_snippet,
)


# ── Layer0 — with identity file ─────────────────────────────────────────


def test_layer0_reads_identity_file(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas, a personal AI assistant for Alice.")
    layer = Layer0(identity_path=str(identity_file))
    text = layer.render()
    assert "Atlas" in text
    assert "Alice" in text


def test_layer0_caches_text(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("Hello world")
    layer = Layer0(identity_path=str(identity_file))
    first = layer.render()
    identity_file.write_text("Changed content")
    second = layer.render()
    assert first == second
    assert second == "Hello world"


def test_layer0_missing_file_returns_default(tmp_path):
    missing = str(tmp_path / "nonexistent.txt")
    layer = Layer0(identity_path=missing)
    text = layer.render()
    assert "No identity configured" in text
    assert "identity.txt" in text


def test_layer0_token_estimate(tmp_path):
    identity_file = tmp_path / "identity.txt"
    content = "A" * 400
    identity_file.write_text(content)
    layer = Layer0(identity_path=str(identity_file))
    estimate = layer.token_estimate()
    assert estimate == 100


def test_layer0_token_estimate_empty(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("")
    layer = Layer0(identity_path=str(identity_file))
    assert layer.token_estimate() == 0


def test_layer0_strips_whitespace(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("  Hello world  \n\n")
    layer = Layer0(identity_path=str(identity_file))
    text = layer.render()
    assert text == "Hello world"


def test_layer0_default_path():
    layer = Layer0()
    expected = os.path.expanduser("~/.mempalace/identity.txt")
    assert layer.path == expected


# ── Layer1 — mocked chromadb ────────────────────────────────────────────


def _mock_chromadb_for_layer(docs, metas, monkeypatch=None):
    """Return a mock collection whose get() returns docs/metas."""
    mock_col = MagicMock()
    # First batch returns data, second batch returns empty (end of pagination)
    mock_col.get.side_effect = [
        {"documents": docs, "metadatas": metas},
        {"documents": [], "metadatas": []},
    ]
    return mock_col


def test_layer1_no_palace():
    """Layer1 returns helpful message when no palace exists."""
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer1(palace_path="/nonexistent/palace")
    result = layer.generate()
    assert "No palace found" in result or "No memories" in result


def test_layer1_generates_essential_story():
    docs = [
        "Important memory about project decisions",
        "Key architectural choice for the backend",
    ]
    metas = [
        {"room": "decisions", "source_file": "meeting.txt", "importance": 5},
        {"room": "architecture", "source_file": "design.txt", "importance": 4},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    assert "project decisions" in result


def test_layer1_empty_palace():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "No memories" in result


def test_layer1_with_wing_filter():
    docs = ["Memory about project X"]
    metas = [{"room": "general", "source_file": "x.txt", "importance": 3}]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake", wing="project_x")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    # Verify wing filter was passed
    call_kwargs = mock_col.get.call_args_list[0][1]
    assert call_kwargs.get("where") == {"wing": "project_x"}


def test_layer1_truncates_long_snippets():
    docs = ["A" * 300]
    metas = [{"room": "general", "source_file": "long.txt"}]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "..." in result


def test_layer1_respects_max_chars():
    """L1 stops adding entries once MAX_CHARS is reached."""
    docs = [f"Memory number {i} with substantial content padding here" for i in range(30)]
    metas = [{"room": "general", "source_file": f"f{i}.txt", "importance": 5} for i in range(30)]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        layer.MAX_CHARS = 200  # Very low cap to trigger truncation
        result = layer.generate()

    assert "more in L3 search" in result


def test_layer1_importance_from_various_keys():
    """Layer1 tries importance, emotional_weight, weight keys."""
    docs = ["mem1", "mem2", "mem3"]
    metas = [
        {"room": "r", "emotional_weight": 5},
        {"room": "r", "weight": 1},
        {"room": "r"},  # no weight key, defaults to 3
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result


def test_layer1_breaks_importance_ties_by_filed_at_recency():
    """Equal-importance drawers surface newest-first instead of insertion order."""
    docs = ["oldest memory", "newest memory", "middle memory"]
    metas = [
        {"room": "moments", "importance": 3, "filed_at": "2026-01-01T00:00:00Z"},
        {"room": "moments", "importance": 3, "filed_at": "2026-03-01T00:00:00Z"},
        {"room": "moments", "importance": 3, "filed_at": "2026-02-01T00:00:00Z"},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    assert result.index("newest memory") < result.index("middle memory")
    assert result.index("middle memory") < result.index("oldest memory")


# ── Layer1 — salience scoring and snippet composition ──────────────────


def test_l1_salience_drops_tool_scaffolding():
    """A drawer that *is* a harness injection: the wrapper opens the text."""
    junk = [
        "<system-reminder>Remember to run the tests before you claim done.</system-reminder>",
        "<task-notification> background agent finished its run and reported back</task-notification>",
        "[SYSTEM NOTIFICATION - NOT USER INPUT] the background task has completed",
        "<command-message>dira</command-message> running the project slash command now",
        "<local-command-caveat>Caveat: the messages below were generated while running commands",
    ]
    for text in junk:
        assert _l1_salience(text) < 0, text

    # A code fence is no longer grounds for exclusion — engineering prose
    # quotes code constantly. A fence made mostly of English words reads as
    # prose by shape, so it survives, but demoted below ordinary prose.
    fence = "```python\nprint('hello world from the example snippet')\n```"
    assert (
        0
        <= _l1_salience(fence)
        < _l1_salience("We talked through the pricing model for the hosted tier.")
    )


def test_l1_salience_keeps_outcomes_that_mention_scaffolding():
    """Regression: mentioning tooling is not the same as being tooling.

    Every drawer here reports a real outcome and every one was silently
    dropped when scaffolding markers were matched as substrings anywhere in
    the body. Reported on #2169.
    """
    outcomes = [
        # A root cause, quoting the query that reproduced it.
        "Root cause of the deadlock: two workers took the advisory locks in "
        "opposite order. Reproduced with:\n```sql\nSELECT pg_advisory_lock(1);\n```\n"
        "Fixed by sorting the lock ids before acquiring.",
        # The migration that finally worked, quoting the command.
        "The migration that finally worked was 0043: we dropped the partial index "
        "first, then backfilled in batches of 5000. Verified on the full corpus.\n"
        "```\nalembic upgrade head\n```",
        # A retry fix that names the tool-call type it was looping on.
        "Fixed the retry storm by capping backoff at 30s and adding jitter. "
        "The old loop retried instantly on `tool_use` errors and hammered the API.",
        # A build failure identified by its exit code.
        "The nightly build died with Exit code: 137, which is the OOM killer, not a "
        "test failure. We raised the container memory limit to 4G and it passed.",
        # An agent stuck emitting tool calls.
        "The agent was looping: it emitted the same tool_use block forever because the "
        "result was never appended. Fixed by threading the tool_result back in.",
        # Prose *about* a harness wrapper, which is exactly what a bug report is.
        "The wake-up output was full of <system-reminder> blocks, so we taught L1 to "
        "drop them and shipped the fix.",
    ]
    for text in outcomes:
        assert _l1_salience(text) >= 0, text


def test_l1_salience_ranks_outcomes_above_chatter_even_when_noisy():
    """The noise demotion must not cancel out the outcome boost."""
    noisy_outcome = _l1_salience(
        "The nightly build died with Exit code: 137, the OOM killer. "
        "We raised the limit and it passed."
    )
    clean_chatter = _l1_salience(
        "We talked through the pricing model for the hosted tier this morning."
    )
    assert noisy_outcome > clean_chatter


def test_l1_salience_demotes_fenced_code_below_clean_outcomes():
    """Relaxing the fence ban must not let code dumps dominate the wake-up."""
    fenced = _l1_salience("Here is the config we use:\n```yaml\nkey: value\n```\nnothing else.")
    outcome = _l1_salience("We deployed the new retrieval backend and verified recall.")
    assert outcome > fenced


def test_l1_salience_keeps_prose_naming_a_wrapper_twice():
    """Discussing the same wrapper twice is writing about tooling, not tooling.

    An earlier revision of this filter also dropped any body that repeated a
    wrapper, on the theory that a repeat means concatenated injections. That
    rule was unanchored, so it re-created the exact defect the anchoring fixed:
    an engineer explaining why a reminder fired twice writes the tag twice.
    Concatenated soup is caught by table density and the prose-ratio floor
    instead, which need no marker named.
    """
    text = (
        "The wake-up hook injects a system-reminder block on every turn, so seeing "
        "a second system-reminder block in one transcript means the turn restarted "
        "rather than that the harness misbehaved, and we fixed the loop."
    )
    assert _l1_salience(text) > 0

    literal = (
        "Writing up why the turn restarted. The harness emits one "
        "<system-reminder> per turn, so a second <system-reminder> in the same "
        "transcript is the restart, not a bug in our code, and the fix landed."
    )
    assert _l1_salience(literal) > 0


def test_l1_salience_drops_soup_without_markers():
    """Raw machine output is caught by shape, not by naming every marker.

    None of these contain a scaffolding marker, so the old substring ban let
    all of them through. They must be dropped outright or ranked below prose.
    """
    soup = [
        # JSON blob
        '{"id":"drw_8123","wing":"proj","room":"2026-08-06","meta":{"importance":3,'
        '"filed_at":"2026-08-06T09:00:00Z","source_file":"/x/y/z.jsonl"},"n":417}',
        # ls -l output
        "-rw-r--r--  1 atk staff   4096 Aug  6 09:00 config.yaml\n"
        "-rw-r--r--  1 atk staff  81920 Aug  6 09:01 palace.sqlite3",
        # log lines
        "2026-08-06 09:00:01,123 INFO  chroma.segment 417 ids\n"
        "2026-08-06 09:00:02,455 WARN  chroma.hnsw ef=64 m=16",
        # env dump
        "PATH=/usr/bin:/bin HOME=/Users/atk SHELL=/bin/zsh TERM=xterm-256color",
        # url list
        "https://example.com/a?x=1&y=2 https://example.com/b?x=3&y=4",
        # pytest tail
        "tests/test_layers.py::test_l1_salience PASSED [ 42%]\n"
        "tests/test_layers.py::test_x PASSED [ 43%]",
    ]
    for text in soup:
        assert _l1_salience(text) < 0, text

    # Word-heavy machine output clears the prose floor on letter count alone,
    # so it is demoted instead: never above ordinary prose.
    plain_prose = _l1_salience("We talked through the pricing model for the hosted tier.")
    for text in (
        'Traceback (most recent call last):\n  File "/app/run.py", line 88, in main\n'
        "    x = y[0]\nIndexError: list index out of range",
        "@@ -17,6 +17,7 @@\n+import re\n import sys\n-from pathlib import Path",
    ):
        assert _l1_salience(text) < plain_prose, text


def test_l1_salience_scores_non_latin_prose_like_latin():
    """Arabic, Hebrew and CJK prose must not be structurally penalized.

    Two ways they were: a token-based prose ratio saw space-free Chinese as a
    single non-word, and the clean-start point was awarded on ``isupper()``,
    which is False for every character of an uncased script.
    """
    latin = _l1_salience("We talked through the pricing model for the hosted tier.")
    non_latin = [
        "تم نشر النسخة الجديدة من المنصة اليوم وتم التحقق من عمل البحث بشكل كامل.",
        "אנחנו שחררנו את הגרסה החדשה היום ובדקנו שהחיפוש עובד כמו שצריך.",
        "我们今天部署了新的检索后端并验证了全部召回结果没有问题。",
        "本日、新しい検索バックエンドをデプロイし、リコール結果をすべて確認しました。",
    ]
    for text in non_latin:
        assert _l1_salience(text) == latin, text


def test_l1_prose_ratio_separates_writing_from_dumps():
    assert _l1_prose_ratio("We shipped the fix and verified it.") > 0.9
    assert _l1_prose_ratio("我们今天部署了新的检索后端。") > 0.9
    assert _l1_prose_ratio('{"a":1,"b":[2,3],"c":{"d":4}}') < 0.5
    assert _l1_prose_ratio("") == 0.0


def test_l1_select_keeps_the_reviewers_eight_drawer_repro():
    """End to end on #2169's repro: five outcomes, three prose, nothing lost.

    The everything-junk fallback deliberately does not fire here (the three
    prose drawers survive), so before the fix these five were simply gone.
    """
    outcomes = [
        ("deadlock", "Root cause of the deadlock was lock ordering. See ```SELECT 1;``` above."),
        ("migration", "The migration that finally worked was 0043, verified on the full corpus."),
        ("retry", "Fixed the retry storm by capping backoff; it looped on `tool_use` errors."),
        (
            "build",
            "The nightly build died with Exit code: 137, which is the OOM killer, not a "
            "test failure. We raised the container memory limit to 4G and it passed.",
        ),
        ("agent", "The agent looped emitting the same tool_use block. Fixed by threading it back."),
    ]
    prose = [
        ("chat1", "We talked through the pricing model for the hosted tier this morning."),
        ("chat2", "Background reading on how the original recall path was designed."),
        ("chat3", "Notes from the sync about which client onboards first next quarter."),
    ]
    candidates = [
        (3.0, {"source_file": f"{name}.md", "room": "r"}, text) for name, text in outcomes + prose
    ]
    selected = _l1_select(candidates, max_drawers=10)
    kept = {meta["source_file"] for _imp, meta, _doc in selected}

    for name, _text in outcomes:
        assert f"{name}.md" in kept, f"{name} was dropped"
    # And they lead: every outcome outranks every background-prose drawer.
    order = [m["source_file"] for _i, m, _d in selected]
    assert max(order.index(f"{n}.md") for n, _t in outcomes) < min(
        order.index(f"{n}.md") for n, _t in prose
    )


def test_l1_salience_drops_date_only_and_too_short():
    assert _l1_salience("2026-08-06T14:22:31Z 2026-08-06T14:22:35Z 2026-08-06") < 0
    assert _l1_salience("------------------------------------------") < 0
    assert _l1_salience("ok thanks") < 0


def test_l1_salience_drops_table_soup():
    table = "| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |\n| 7 | 8 | 9 |"
    assert _l1_salience(table) < 0


def test_l1_salience_boosts_outcome_language():
    outcome = "We shipped the pgvector backend and verified recall on the full corpus."
    plain = "The weather in Riyadh today is warm and the office is quiet again."
    assert _l1_salience(outcome) > _l1_salience(plain)
    assert _l1_salience(plain) >= 0


def test_l1_salience_ranks_fragment_below_whole_thought():
    fragment = "and then the migration finished without any of the drama we expected"
    whole = "Then the migration finished without any of the drama we expected."
    assert _l1_salience(whole) > _l1_salience(fragment)
    assert _l1_salience(fragment) >= 0


def test_l1_salience_keeps_prose_containing_iso_designator_letters():
    """The date-only guard must not eat ordinary words (regression guard)."""
    assert _l1_salience("There were zero regressions in the nightly run today.") >= 0


def test_l1_snippet_starts_at_a_sentence_boundary():
    text = (
        "ing the retry loop, which was the wrong layer entirely. "
        "The root cause was the connection pool exhausting under load."
    )
    snippet = _l1_snippet(text)
    assert snippet.startswith("The root cause was")


def test_l1_snippet_keeps_lowercase_start_when_no_boundary_is_near():
    text = "the pool exhausted under load and nothing else was wrong with the service"
    assert _l1_snippet(text) == text


def test_l1_snippet_cuts_on_a_word_boundary():
    text = "Alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu " * 6
    snippet = _l1_snippet(text)
    assert snippet.endswith("...")
    assert len(snippet) <= 200
    # No half words: the character before the ellipsis ends a whole token.
    assert not snippet[:-3].endswith(" ")
    assert text.startswith(snippet[:-3])
    assert text[len(snippet) - 3] == " "


def test_l1_snippet_collapses_newlines_and_leaves_short_text_alone():
    assert _l1_snippet("first line\n\nsecond   line") == "first line second line"


def test_l1_select_caps_snippets_per_source_file():
    candidates = [
        (3.0, {"source_file": "big.md", "room": "r"}, f"We shipped change number {i} today.")
        for i in range(5)
    ]
    candidates.append((3.0, {"source_file": "other.md", "room": "r"}, "We fixed the flaky test."))
    selected = _l1_select(candidates, max_drawers=10)
    sources = [meta["source_file"] for _imp, meta, _doc in selected]
    assert sources.count("big.md") == L1_MAX_PER_SOURCE
    assert "other.md" in sources


def test_l1_select_does_not_cap_unattributed_drawers():
    candidates = [
        (3.0, {"room": "r"}, f"We shipped change number {i} today, all verified.") for i in range(5)
    ]
    assert len(_l1_select(candidates, max_drawers=10)) == 5


def test_l1_select_suppresses_near_duplicates():
    shared = "The checkpoint hook was rewritten to skip the harness boilerplate that leaked into every summary"
    candidates = [
        (3.0, {"source_file": "a.md", "room": "r"}, shared + " and it shipped."),
        (3.0, {"source_file": "b.md", "room": "r"}, shared + " and it shipped, twice."),
        (3.0, {"source_file": "c.md", "room": "r"}, "A different outcome entirely: we reverted."),
    ]
    selected = _l1_select(candidates, max_drawers=10)
    assert len(selected) == 2
    assert selected[-1][1]["source_file"] == "c.md"


def test_l1_select_keeps_distinct_summaries_sharing_a_templated_lead():
    """Regression: a shared opening is not a duplicate.

    Mined session summaries routinely open with the same templated sentence.
    Keying near-duplicate detection on the first 80 characters collapsed three
    different outcomes into one and lost two of them silently.
    """
    lead = (
        "Session summary for the mempalace project on the sixth of August 2026, "
        "covering the work that"
    )
    bodies = [
        " we shipped the pgvector migration after backfilling in batches of five "
        "thousand rows, verified recall against the full corpus and merged it.",
        " we reverted the checkpoint hook rewrite because it skipped genuine user "
        "messages that quoted a harness wrapper, and reworked the match.",
        " we merged the Arabic RTL fixes into the release branch and confirmed the "
        "dashboard renders correctly on the tablet layout.",
    ]
    candidates = [
        (3.0, {"source_file": f"s{i}.md", "room": "r"}, lead + body)
        for i, body in enumerate(bodies)
    ]
    assert len(_l1_select(candidates, max_drawers=10)) == 3


def test_l1_select_prefers_outcomes_over_neutral_prose():
    candidates = [
        (3.0, {"source_file": "a.md", "room": "r"}, "Some background reading about the domain."),
        (3.0, {"source_file": "b.md", "room": "r"}, "We deployed the fix and verified it."),
    ]
    selected = _l1_select(candidates, max_drawers=2)
    assert selected[0][1]["source_file"] == "b.md"


def test_l1_select_returns_empty_when_everything_is_junk():
    candidates = [(3.0, {"source_file": "a.md", "room": "r"}, "<system-reminder>x</...>")]
    assert _l1_select(candidates, max_drawers=5) == []


def test_layer1_leads_with_the_outcome_not_the_scaffolding():
    """End to end: junk is out, the outcome line leads, one file cannot own L1."""
    docs = [
        "<system-reminder>Do not forget to run the full test suite before shipping.</system-reminder>",
        "| col | col |\n|-----|-----|\n| 1   | 2   |\n|-----|-----|\n| 3   | 4   |",
        "2026-08-06T09:00:00Z 2026-08-06T09:00:01Z 2026-08-06T09:00:02Z",
        "Background notes on how the retry loop was originally written years ago.",
        "We shipped the recency fetch and verified wake-up on the full palace.",
        "We shipped the recency fetch and verified wake-up on the full palace, again.",
        "Third entry from the same transcript, which also mentions we deployed it.",
    ]
    metas = [
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:00:00Z"},
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:01:00Z"},
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:02:00Z"},
        {"room": "session", "source_file": "notes.md", "filed_at": "2026-08-06T09:03:00Z"},
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:04:00Z"},
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:05:00Z"},
        {"room": "session", "source_file": "t.md", "filed_at": "2026-08-06T09:06:00Z"},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    assert "system-reminder" not in result
    assert "| col |" not in result
    assert "2026-08-06T09:00:01Z" not in result
    assert "We shipped the recency fetch" in result
    # Near-duplicate of the same sentence appears once, and t.md is capped.
    assert result.count("We shipped the recency fetch") == 1
    assert result.count("(t.md)") <= L1_MAX_PER_SOURCE
    assert "Background notes" in result


def test_layer1_char_cap_keeps_the_outcome_not_the_alphabet():
    """Regression: rendering must not undo the salience ranking.

    Rooms used to render in alphabetical order, so when MAX_CHARS truncated,
    what survived depended on the room's name. A high-salience outcome in a
    room called "zzz_*" was cut while background chatter in "aaa_*" rendered.
    """
    docs = [
        "Background chatter about the office coffee machine and nothing important.",
        "We shipped the pgvector backend, verified recall and merged the release.",
    ]
    metas = [
        {"room": "aaa_trivia", "source_file": "a.md", "filed_at": "2026-08-06T09:00:00Z"},
        {"room": "zzz_outcomes", "source_file": "b.md", "filed_at": "2026-08-06T09:01:00Z"},
    ]
    assert _l1_salience(docs[1]) > _l1_salience(docs[0])
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        layer.MAX_CHARS = 120
        result = layer.generate()

    assert "shipped the pgvector" in result
    assert "coffee machine" not in result


def test_layer1_falls_back_when_every_drawer_is_junk():
    docs = ["<system-reminder>alpha</system-reminder>", "<system-reminder>beta</system-reminder>"]
    metas = [{"room": "r", "source_file": "a.md"}, {"room": "r", "source_file": "b.md"}]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    assert "ESSENTIAL STORY" in result
    assert "alpha" in result


def test_layer1_snippets_stay_verbatim_substrings():
    """L1 never paraphrases: every rendered snippet is text from the drawer."""
    doc = (
        "We decided to keep the exact scan as the recall path and verified the "
        "numbers against the previous release before merging anything at all."
    )
    mock_col = _mock_chromadb_for_layer([doc], [{"room": "r", "source_file": "a.md"}])

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        result = Layer1(palace_path="/fake").generate()

    rendered = result.split("  - ")[1].split("  (")[0]
    assert rendered.rstrip(".") in doc


def test_layer1_batch_exception_breaks():
    """If col.get raises on a batch, loop breaks gracefully."""
    mock_col = MagicMock()
    mock_col.get.side_effect = [
        {"documents": ["doc1"], "metadatas": [{"room": "r"}]},
        RuntimeError("batch error"),
    ]
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert "ESSENTIAL STORY" in result


# ── Layer2 — mocked chromadb ────────────────────────────────────────────


def test_layer2_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer2(palace_path="/nonexistent/palace")
    result = layer.retrieve(wing="test")
    assert "No palace found" in result


def test_layer2_retrieve_with_wing():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Some memory about the project"],
        "metadatas": [{"room": "backend", "source_file": "notes.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="project")

    assert "ON-DEMAND" in result
    assert "memory about the project" in result


def test_layer2_retrieve_with_room():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Backend architecture notes"],
        "metadatas": [{"room": "architecture", "source_file": "arch.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(room="architecture")

    assert "ON-DEMAND" in result


def test_layer2_retrieve_wing_and_room():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["Filtered result"],
        "metadatas": [{"room": "backend", "source_file": "x.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="proj", room="backend")

    assert "ON-DEMAND" in result
    call_kwargs = mock_col.get.call_args[1]
    assert "$and" in call_kwargs.get("where", {})


def test_layer2_retrieve_empty():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="missing")

    assert "No drawers found" in result


def test_layer2_retrieve_no_filter():
    mock_col = MagicMock()
    mock_col.get.return_value = {"documents": [], "metadatas": []}
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        layer.retrieve()

    # No where filter should be passed
    call_kwargs = mock_col.get.call_args[1]
    assert "where" not in call_kwargs


def test_layer2_retrieve_error():
    mock_col = MagicMock()
    mock_col.get.side_effect = RuntimeError("db error")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="test")

    assert "Retrieval error" in result


def test_layer2_truncates_long_snippets():
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["B" * 400],
        "metadatas": [{"room": "r", "source_file": "s.txt"}],
    }
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        result = layer.retrieve(wing="test")

    assert "..." in result


# ── Layer3 — mocked chromadb ────────────────────────────────────────────


def _mock_query_results(docs, metas, dists):
    return {
        "documents": [docs],
        "metadatas": [metas],
        "distances": [dists],
    }


def test_layer3_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer3(palace_path="/nonexistent/palace")
    result = layer.search("test query")
    assert "No palace found" in result


def test_layer3_search_raw_no_palace():
    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent/palace"
        layer = Layer3(palace_path="/nonexistent/palace")
    result = layer.search_raw("test query")
    assert result == []


def test_layer3_search_with_results():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["Found this important memory"],
        [{"wing": "project", "room": "backend", "source_file": "notes.txt"}],
        [0.2],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("important")

    assert "SEARCH RESULTS" in result
    assert "important memory" in result
    assert "sim=0.8" in result


def test_layer3_search_no_results():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results([], [], [])
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("nothing")

    assert "No results found" in result


def test_layer3_search_with_wing_filter():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "proj", "room": "r"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", wing="proj")

    call_kwargs = mock_col.query.call_args[1]
    assert call_kwargs["where"] == {"wing": "proj"}


def test_layer3_search_with_room_filter():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "w", "room": "backend"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", room="backend")

    call_kwargs = mock_col.query.call_args[1]
    assert call_kwargs["where"] == {"room": "backend"}


def test_layer3_search_with_wing_and_room():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["result"],
        [{"wing": "proj", "room": "backend"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search("q", wing="proj", room="backend")

    call_kwargs = mock_col.query.call_args[1]
    assert "$and" in call_kwargs["where"]


def test_layer3_search_error():
    mock_col = MagicMock()
    mock_col.query.side_effect = RuntimeError("search failed")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("q")

    assert "Search error" in result


def test_layer3_search_truncates_long_docs():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["C" * 400],
        [{"wing": "w", "room": "r", "source_file": "s.txt"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search("q")

    assert "..." in result


def test_layer3_search_raw_returns_dicts():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["doc text"],
        [{"wing": "proj", "room": "backend", "source_file": "f.txt"}],
        [0.3],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        hits = layer.search_raw("q")

    assert len(hits) == 1
    assert hits[0]["text"] == "doc text"
    assert hits[0]["wing"] == "proj"
    assert hits[0]["similarity"] == 0.7
    assert "metadata" in hits[0]


def test_layer3_search_raw_with_filters():
    mock_col = MagicMock()
    mock_col.query.return_value = _mock_query_results(
        ["doc"],
        [{"wing": "w", "room": "r"}],
        [0.1],
    )
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        layer.search_raw("q", wing="w", room="r")

    call_kwargs = mock_col.query.call_args[1]
    assert "$and" in call_kwargs["where"]


def test_layer3_search_raw_error():
    mock_col = MagicMock()
    mock_col.query.side_effect = RuntimeError("fail")
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer3(palace_path="/fake")
        result = layer.search_raw("q")

    assert result == []


# ── MemoryStack ─────────────────────────────────────────────────────────


def test_memory_stack_wake_up(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.wake_up()

    assert "Atlas" in result
    # L1 will say no palace found
    assert "No palace" in result or "No memories" in result


def test_memory_stack_wake_up_with_wing(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.wake_up(wing="my_project")

    assert stack.l1.wing == "my_project"
    assert "Atlas" in result


def test_memory_stack_recall(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.recall(wing="test")

    assert "No palace found" in result


def test_memory_stack_search(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.search("test query")

    assert "No palace found" in result


def test_memory_stack_status(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    with patch("mempalace.layers.MempalaceConfig") as mock_cfg:
        mock_cfg.return_value.palace_path = "/nonexistent"
        stack = MemoryStack(
            palace_path="/nonexistent",
            identity_path=str(identity_file),
        )
        result = stack.status()

    assert result["palace_path"] == "/nonexistent"
    assert result["total_drawers"] == 0
    assert "L0_identity" in result
    assert "L1_essential" in result
    assert "L2_on_demand" in result
    assert "L3_deep_search" in result


def test_memory_stack_status_with_palace(tmp_path):
    identity_file = tmp_path / "identity.txt"
    identity_file.write_text("I am Atlas.")

    mock_col = MagicMock()
    mock_col.count.return_value = 42
    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        stack = MemoryStack(
            palace_path="/fake",
            identity_path=str(identity_file),
        )
        result = stack.status()

    assert result["total_drawers"] == 42
    assert result["L0_identity"]["exists"] is True


# ── Layer1 / Layer2 None-metadata guards ───────────────────────────────
#
# Chroma 1.5.x can return ``None`` inside the ``metadatas`` / ``documents``
# lists for partially-flushed rows. The Layer1.generate() and
# Layer2.retrieve() loops previously called ``meta.get(...)`` without
# coercing, raising ``AttributeError: 'NoneType' object has no attribute
# 'get'`` and blowing up the whole wake-up render. These tests guard that
# the loops tolerate the None entries and render the rest of the result.


def test_layer1_handles_none_metadata():
    """Layer1.generate tolerates None entries in the metadatas list."""
    docs = ["important memory", "another memory"]
    metas = [{"room": "decisions", "source_file": "a.txt"}, None]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        # Should not raise AttributeError on the None entry.
        result = layer.generate()

    assert "ESSENTIAL STORY" in result
    assert "important memory" in result


def test_layer1_handles_none_document():
    """Layer1.generate tolerates None entries in the documents list."""
    docs = ["first doc", None]
    metas = [
        {"room": "r", "source_file": "a.txt"},
        {"room": "r", "source_file": "b.txt"},
    ]
    mock_col = _mock_chromadb_for_layer(docs, metas)

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer1(palace_path="/fake")
        result = layer.generate()

    assert result  # Render succeeded despite the None document.


def test_layer2_handles_none_metadata():
    """Layer2.retrieve tolerates None entries in the metadatas list."""
    mock_col = MagicMock()
    mock_col.get.return_value = {
        "documents": ["first doc", "second doc"],
        "metadatas": [{"room": "r", "source_file": "a.txt"}, None],
    }

    with (
        patch("mempalace.layers.MempalaceConfig") as mock_cfg,
        patch("mempalace.layers._get_collection", return_value=mock_col),
    ):
        mock_cfg.return_value.palace_path = "/fake"
        layer = Layer2(palace_path="/fake")
        # Should not raise AttributeError on the None entry.
        result = layer.retrieve()

    assert "L2 — ON-DEMAND" in result
