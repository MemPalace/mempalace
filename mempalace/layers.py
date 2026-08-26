#!/usr/bin/env python3
"""
layers.py — 4-Layer Memory Stack for mempalace
===================================================

Load only what you need, when you need it.

    Layer 0: Identity       (~100 tokens)   — Always loaded. "Who am I?"
    Layer 1: Essential Story (~500-800)      — Always loaded. Top moments from the palace.
    Layer 2: On-Demand      (~200-500 each)  — Loaded when a topic/wing comes up.
    Layer 3: Deep Search    (unlimited)      — Full ChromaDB semantic search.

Wake-up cost: ~600-900 tokens (L0+L1). Leaves 95%+ of context free.

Reads directly from ChromaDB (mempalace_drawers)
and ~/.mempalace/identity.txt.
"""

import os
import re
import sys
from pathlib import Path
from collections import defaultdict

from .config import MempalaceConfig
from .palace import get_collection as _get_collection
from .searcher import (
    _distance_to_similarity,
    _first_or_empty,
    _metric_for_collection,
    build_where_filter,
)


# ---------------------------------------------------------------------------
# Layer 0 — Identity
# ---------------------------------------------------------------------------


class Layer0:
    """
    ~100 tokens. Always loaded.
    Reads from ~/.mempalace/identity.txt — a plain-text file the user writes.

    Example identity.txt:
        I am Atlas, a personal AI assistant for Alice.
        Traits: warm, direct, remembers everything.
        People: Alice (creator), Bob (Alice's partner).
        Project: A journaling app that helps people process emotions.
    """

    def __init__(self, identity_path: str = None):
        if identity_path is None:
            identity_path = os.path.expanduser("~/.mempalace/identity.txt")
        self.path = identity_path
        self._text = None

    def render(self) -> str:
        """Return the identity text, or a sensible default."""
        if self._text is not None:
            return self._text

        if os.path.exists(self.path):
            with open(self.path, "r") as f:
                self._text = f.read().strip()
        else:
            self._text = (
                "## L0 — IDENTITY\nNo identity configured. Create ~/.mempalace/identity.txt"
            )

        return self._text

    def token_estimate(self) -> int:
        return len(self.render()) // 4


# ---------------------------------------------------------------------------
# Layer 1 — Essential Story (auto-generated from palace)
# ---------------------------------------------------------------------------

#: Words that mark a drawer as reporting an *outcome* — something settled,
#: shipped, broken, or decided — rather than narrating work in progress. A
#: match is a ranking boost, never a requirement: drawers without one still
#: appear, just below the ones with one.
#:
#: This list is English, and so is the boost it drives. A non-English palace
#: still gets every other part of Layer 1 (nothing else here is
#: language-specific) but its drawers never earn this point, so ranking among
#: them falls back to importance and recency. To change that, replace the
#: keywords *and* recompile the regex, which is derived once at import::
#:
#:     layers.L1_OUTCOME_KEYWORDS = (...)
#:     layers._L1_OUTCOME_RE = layers._l1_compile_outcome_re(layers.L1_OUTCOME_KEYWORDS)
#:
#: Reassigning the tuple alone does nothing.
L1_OUTCOME_KEYWORDS = (
    "shipped",
    "shipping",
    "fixed",
    "broke",
    "broken",
    "done",
    "verified",
    "decided",
    "decision",
    "deployed",
    "released",
    "launched",
    "merged",
    "reverted",
    "resolved",
    "root cause",
    "blocked",
    "failed",
    "passed",
    "migrated",
    "renamed",
    "removed",
    "replaced",
    "agreed",
    "chose",
    "switched",
)


def _l1_compile_outcome_re(words) -> "re.Pattern":
    """Build the outcome-keyword matcher. See :data:`L1_OUTCOME_KEYWORDS`."""
    return re.compile(
        r"\b(?:%s)\b" % "|".join(re.escape(word) for word in words),
        re.IGNORECASE,
    )


_L1_OUTCOME_RE = _l1_compile_outcome_re(L1_OUTCOME_KEYWORDS)

#: Structural harness wrappers: literal tags the tooling injects around text
#: the user never typed. These are dropped from L1, but only when one *opens*
#: the drawer, because that is the difference between a drawer that **is**
#: harness output and a drawer that merely *mentions* one (a bug report about
#: ``<system-reminder>`` is story; the reminder itself is not). Matching these
#: anywhere in the body silently deleted real outcomes — see the regression
#: test ``test_l1_salience_keeps_outcomes_that_mention_scaffolding``.
_L1_HARNESS_WRAPPERS = (
    "<system-reminder>",
    "<command-message>",
    "<local-command-caveat>",
    "<task-notification>",
    "[SYSTEM NOTIFICATION",
)

#: Words that read as tool noise rather than story. Unlike the wrappers above
#: these are *never* grounds for exclusion — they appear constantly inside
#: legitimate engineering prose ("the build died with Exit code: 137", "the
#: agent looped on tool_use"). They only forfeit the clean-prose point, so a
#: drawer carrying them ranks below an equally outcome-shaped one that does
#: not. Actual log soup is caught by density and prose ratio, not by these.
_L1_NOISE_MARKERS = (
    "tool_use",
    "tool_result",
    "```",
    "Exit code:",
    "cwd was reset",
    # Word-heavy machine output that clears the prose floor on letter count
    # alone. Demoted rather than excluded, because a human writing "the fix
    # for that Traceback (most recent call last) was a missing guard" is
    # still telling the story.
    "Traceback (most recent call last)",
    "@@ -",
)

#: A drawer that is nothing but a timestamp, date, or separator rule. The
#: letters allowed are the ISO-8601 designators only (``T``, ``Z``).
_L1_TIMESTAMP_ONLY_RE = re.compile(r"^[\s\d:.,/+TZ_-]+$")

#: Sentence boundary followed by the start of a new sentence.
_L1_SENTENCE_START_RE = re.compile(r"[.!?]\s+(?=[A-Z#*\-])")

#: Below this many characters a drawer cannot carry an outcome; above the pipe
#: density it is a table, a diff, or an ASCII rule rather than prose.
_L1_MIN_CHARS = 20
_L1_TABLE_CHARS = "|-=+_"
_L1_MAX_TABLE_DENSITY = 0.18

#: Fraction of a drawer's characters that must be letters or spaces for it to
#: count as prose at all. This is the general form of the old "does it contain
#: a backtick fence" test: JSON, ``ls`` output, env dumps, URL lists, log lines
#: and bare code fences all fail it without any marker being named, while an
#: engineering sentence quoting a path, a version pin or a fence passes.
#:
#: Measured over the corpus in ``test_l1_salience_drops_soup_without_markers``
#: the two populations sit at soup ``0.40-0.82`` against prose ``0.84-0.99``.
#: The floor is set below that gap on purpose: a false drop loses a real
#: memory forever, while a false keep only costs one wake-up line, so the
#: threshold buys margin for prose and leaves the residue to be *demoted* by
#: :data:`_L1_NOISE_MARKERS` rather than excluded.
#:
#: ``str.isalpha`` is Unicode-aware, so Arabic, Hebrew and CJK prose all score
#: as letters. Counting characters rather than tokens is what makes that true:
#: Chinese has no spaces, so a token-based ratio saw one giant "non-word".
_L1_MIN_PROSE_RATIO = 0.78

#: Ranking weights. Reporting an outcome is the point of Layer 1, so it is
#: worth strictly more than the presentation signals — otherwise a drawer that
#: is both an outcome and a little noisy ("the build died with Exit code: 137")
#: would tie with background chatter that merely reads cleanly.
_L1_SCORE_OUTCOME = 2
_L1_SCORE_CLEAN_START = 1
_L1_SCORE_NO_NOISE = 1

#: How many snippets one source file may contribute to a single wake-up. One
#: long transcript should not own the whole story.
L1_MAX_PER_SOURCE = 2

#: How alike two drawers must be, as a word-overlap fraction, before the later
#: one is suppressed as a near-duplicate.
#:
#: This compares whole bodies rather than a leading slice. Matching on the
#: first 80 characters silently collapsed *distinct* drawers that happened to
#: share a templated opening, which mined session summaries routinely do: three
#: summaries opening "Session summary for the mempalace project on ..." and then
#: reporting three different outcomes scored as one drawer and two of the
#: outcomes were lost. Whole-body overlap puts that case near ``0.27`` while
#: genuine restatements of the same drawer stay above ``0.84``.
_L1_DUPLICATE_SIMILARITY = 0.8


def _l1_normalize(text: str) -> str:
    """Collapse whitespace so drawers compare and render on one line."""
    return " ".join(text.split())


def _l1_prose_ratio(body: str) -> float:
    """Fraction of ``body`` that is letters or spaces.

    The measure of whether something was written or dumped. Near ``1.0`` for
    prose in any script; low for JSON, command output and log lines, which are
    mostly punctuation, digits, paths and identifiers.
    """
    if not body:
        return 0.0
    return sum(1 for char in body if char.isalpha() or char.isspace()) / len(body)


def _l1_is_harness_output(body: str) -> bool:
    """True when the drawer *is* a harness injection, not prose mentioning one.

    Anchored at the start, because that is where an injected wrapper opens. A
    drawer that quotes or discusses a wrapper part-way through is a human
    writing about the tooling, and dropping it was the bug this guards.

    Deliberately the only positional rule. Counting repeats anywhere in the
    body was tried and removed: a drawer that names the same wrapper twice
    while explaining it ("``<system-reminder>`` is injected per turn, so a
    second ``<system-reminder>`` means the turn restarted") is prose, and a
    genuine run of concatenated injections is already caught downstream by
    table density and the prose-ratio floor, neither of which needs a marker
    named.
    """
    return body.startswith(_L1_HARNESS_WRAPPERS)


def _l1_salience(text: str) -> int:
    """Score a drawer as an L1 candidate. Negative means "not story, drop it".

    Deterministic and purely lexical — no LLM call, no embedding, no I/O.
    Layer 1 runs inside the wake-up hook's latency budget, so this has to stay
    a few passes over text already in memory.

    Exclusion is deliberately narrow: only things that are *structurally* not
    prose (a harness wrapper opening the drawer, a bare timestamp, table or
    log soup, too few characters to say anything). Merely mentioning tooling
    is not disqualifying — that test dropped real outcomes such as a build
    that died on an exit code or an agent stuck emitting tool calls.

    Returns:
        ``-1`` junk, otherwise ``0``-``4``: points for reading like an outcome
        (weighted highest), starting cleanly at a sentence, and being free of
        tool noise.
    """
    body = _l1_normalize(text)
    if len(body) < _L1_MIN_CHARS:
        return -1
    if _L1_TIMESTAMP_ONLY_RE.match(body):
        return -1
    if _l1_is_harness_output(body):
        return -1
    density = sum(body.count(char) for char in _L1_TABLE_CHARS) / len(body)
    if density > _L1_MAX_TABLE_DENSITY:
        return -1
    if _l1_prose_ratio(body) < _L1_MIN_PROSE_RATIO:
        return -1

    score = 0
    if _L1_OUTCOME_RE.search(body):
        score += _L1_SCORE_OUTCOME
    # A chunk that starts at a sentence or a heading reads as a whole thought;
    # one that starts mid-sentence is a fragment of someone else's. Phrased as
    # "not lowercase" rather than "is uppercase" so that uncased scripts
    # (Arabic, Hebrew, CJK) are not permanently denied the point.
    if not body[0].islower() or body[0] in "#*":
        score += _L1_SCORE_CLEAN_START
    # Clean prose outranks prose carrying tool noise, without excluding it.
    if not any(marker in body for marker in _L1_NOISE_MARKERS):
        score += _L1_SCORE_NO_NOISE
    return score


def _l1_snippet(text: str, max_chars: int = 200) -> str:
    """Compose the snippet shown for one drawer.

    Verbatim: the result is always a contiguous substring of the drawer, never
    a paraphrase. What this chooses is where to start and stop.

    * Chunked drawers frequently open mid-sentence. When the text starts
      lowercase and a sentence boundary is close by, start after that boundary
      instead, so the line does not begin in the middle of someone's thought.
    * Truncation cuts on a word boundary, so the tail is never a half word.
    """
    body = _l1_normalize(text)
    if body[:1].islower():
        match = _L1_SENTENCE_START_RE.search(body[:300])
        if match and len(body) - match.end() >= _L1_MIN_CHARS:
            body = body[match.end() :]
    if len(body) <= max_chars:
        return body
    keep = max_chars - 3
    cut = body.rfind(" ", max_chars // 2, keep)
    return body[: cut if cut > 0 else keep] + "..."


def _l1_word_overlap(left: set, right: set) -> float:
    """Jaccard overlap of two word sets: 1.0 identical, 0.0 nothing in common."""
    union = len(left | right)
    return len(left & right) / union if union else 0.0


def _l1_select(scored: list, max_drawers: int, max_per_source: int = L1_MAX_PER_SOURCE) -> list:
    """Choose the drawers that make up the essential story.

    ``scored`` is the importance/recency-ordered candidate list, each entry
    ``(importance, metadata, document)``. Selection keeps that order inside a
    salience tier and applies three caps: junk is dropped, no source file may
    contribute more than ``max_per_source`` snippets, and near-duplicate
    drawers (mostly the same words) collapse to one.

    Returns an empty list when every candidate scored as junk. The caller
    decides what to do about that; L1 never silently renders nothing.
    """
    ranked = []
    for entry in scored:
        score = _l1_salience(entry[2])
        if score >= 0:
            ranked.append((score, entry))
    # Stable: candidates keep their importance/recency order inside a tier.
    ranked.sort(key=lambda item: item[0], reverse=True)

    selected = []
    per_source: dict = defaultdict(int)
    seen_words: list = []
    for _score, entry in ranked:
        if len(selected) >= max_drawers:
            break
        _imp, meta, doc = entry
        source = (meta or {}).get("source_file") or ""
        # Drawers with no source_file cannot be attributed, so they are not
        # capped: the cap exists to stop one known file dominating.
        if source and per_source[source] >= max_per_source:
            continue
        words = set(_l1_normalize(doc).lower().split())
        if any(_l1_word_overlap(words, prev) >= _L1_DUPLICATE_SIMILARITY for prev in seen_words):
            continue
        # Only ever as long as `max_drawers`, so the scan stays linear.
        seen_words.append(words)
        per_source[source] += 1
        selected.append(entry)
    return selected


class Layer1:
    """
    ~500-800 tokens. Always loaded.
    Auto-generated from the highest-weight / most-recent drawers in the palace.
    Groups by room, picks the top N moments, compresses to a compact summary.
    """

    MAX_DRAWERS = 15  # at most 15 moments in wake-up
    MAX_CHARS = 3200  # hard cap on total L1 text (~800 tokens)
    MAX_SCAN = 2000  # don't scan more than this for L1 generation

    def __init__(self, palace_path: str = None, wing: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.wing = wing

    def generate(self) -> str:
        """Pull top drawers from ChromaDB and format as compact L1 text."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "## L1 — No palace found. Run: mempalace mine <dir>"

        # Fetch all drawers in batches to avoid SQLite variable limit (~999)
        _BATCH = 500
        docs, metas = [], []
        offset = 0
        while True:
            kwargs = {"include": ["documents", "metadatas"], "limit": _BATCH, "offset": offset}
            if self.wing:
                kwargs["where"] = {"wing": self.wing}
            try:
                batch = col.get(**kwargs)
            except Exception:
                break
            batch_docs = batch.get("documents", [])
            batch_metas = batch.get("metadatas", [])
            if not batch_docs:
                break
            docs.extend(batch_docs)
            metas.extend(batch_metas)
            offset += len(batch_docs)
            if len(batch_docs) < _BATCH or len(docs) >= self.MAX_SCAN:
                break

        if not docs:
            return "## L1 — No memories yet."

        # Score each drawer: prefer high importance, then most-recent filing.
        # NOTE: the ingest pipeline (miner, convo_miner, diary, add_drawer)
        # records provenance metadata — wing/room/source/chunk/filed_at — but
        # never an evaluative importance/weight field. So `importance` is
        # absent on virtually every drawer and ties at the default, which used
        # to collapse the sort to insertion order (oldest first). `filed_at`
        # is present on every drawer, so it is the *effective* ordering signal:
        # newest first. This keeps importance as the primary key for the day a
        # scoring pass populates it, while making the "recent filing" half of
        # the promise true today with data we already have.
        scored = []
        for doc, meta in zip(docs, metas):
            meta = meta or {}
            doc = doc or ""
            importance = 3.0
            # Try multiple metadata keys that might carry weight info
            for key in ("importance", "emotional_weight", "weight"):
                val = meta.get(key)
                if val is not None:
                    try:
                        importance = float(val)
                    except (ValueError, TypeError):
                        pass
                    break
            # filed_at is an ISO-8601 string; ISO strings sort lexicographically
            # in chronological order. Coerce to str so a missing/odd value sorts
            # oldest rather than raising during the comparison.
            recency = str(meta.get("filed_at") or "")
            scored.append((importance, recency, meta, doc))

        # Sort by importance desc, then recency (filed_at) desc.
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        candidates = [(imp, meta, doc) for imp, _recency, meta, doc in scored]

        # Then pick the story out of the candidates: drop scaffolding and
        # table soup, prefer outcome-shaped prose, cap how much any one source
        # file contributes, and collapse near-duplicates.
        top = _l1_select(candidates, self.MAX_DRAWERS)
        if not top:
            # Everything scored as junk (a palace of pure tool logs, or drawers
            # too short for the floor). An empty wake-up would be worse than an
            # unfiltered one, so fall back to exactly the pre-filter behavior.
            top = candidates[: self.MAX_DRAWERS]

        # Group by room for readability. Insertion order, not alphabetical:
        # `top` arrives in salience order, so a dict preserves "best room
        # first". Sorting by name instead let MAX_CHARS truncate the story
        # because of where a room sits in the alphabet, which threw away the
        # highest-ranked drawer while keeping chatter from a room called
        # "aaa_*". Selection ranking is worthless if rendering reshuffles it.
        by_room = defaultdict(list)
        for imp, meta, doc in top:
            room = meta.get("room", "general")
            by_room[room].append((imp, meta, doc))

        # Build compact text
        lines = ["## L1 — ESSENTIAL STORY"]

        total_len = 0
        for room, entries in by_room.items():
            room_line = f"\n[{room}]"
            lines.append(room_line)
            total_len += len(room_line)

            for _imp, meta, doc in entries:
                source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""

                snippet = _l1_snippet(doc)

                entry_line = f"  - {snippet}"
                if source:
                    entry_line += f"  ({source})"

                if total_len + len(entry_line) > self.MAX_CHARS:
                    lines.append("  ... (more in L3 search)")
                    return "\n".join(lines)

                lines.append(entry_line)
                total_len += len(entry_line)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 2 — On-Demand (wing/room filtered retrieval)
# ---------------------------------------------------------------------------


class Layer2:
    """
    ~200-500 tokens per retrieval.
    Loaded when a specific topic or wing comes up in conversation.
    Queries ChromaDB with a wing/room filter.
    """

    def __init__(self, palace_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path

    def retrieve(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """Retrieve drawers filtered by wing and/or room."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "No palace found."

        where = build_where_filter(wing, room)

        kwargs = {"include": ["documents", "metadatas"], "limit": n_results}
        if where:
            kwargs["where"] = where

        try:
            results = col.get(**kwargs)
        except Exception as e:
            return f"Retrieval error: {e}"

        docs = results.get("documents", [])
        metas = results.get("metadatas", [])

        if not docs:
            label = f"wing={wing}" if wing else ""
            if room:
                label += f" room={room}" if label else f"room={room}"
            return f"No drawers found for {label}."

        lines = [f"## L2 — ON-DEMAND ({len(docs)} drawers)"]
        for doc, meta in zip(docs[:n_results], metas[:n_results]):
            meta = meta or {}
            doc = doc or ""
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""
            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."
            entry = f"  [{room_name}] {snippet}"
            if source:
                entry += f"  ({source})"
            lines.append(entry)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Layer 3 — Deep Search (full semantic search via ChromaDB)
# ---------------------------------------------------------------------------


class Layer3:
    """
    Unlimited depth. Semantic search against the full palace.
    Reuses searcher.py logic against mempalace_drawers.
    """

    def __init__(self, palace_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Semantic search, returns compact result text."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return "No palace found."

        where = build_where_filter(wing, room)

        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = col.query(**kwargs)
        except Exception as e:
            return f"Search error: {e}"

        docs = _first_or_empty(results, "documents")
        metas = _first_or_empty(results, "metadatas")
        dists = _first_or_empty(results, "distances")

        if not docs:
            return "No results found."

        metric = _metric_for_collection(col)
        lines = [f'## L3 — SEARCH RESULTS for "{query}"']
        for i, (doc, meta, dist) in enumerate(zip(docs, metas, dists), 1):
            meta = meta or {}
            doc = doc or ""
            similarity = round(_distance_to_similarity(dist, metric), 3)
            wing_name = meta.get("wing", "?")
            room_name = meta.get("room", "?")
            source = Path(meta.get("source_file", "")).name if meta.get("source_file") else ""

            snippet = doc.strip().replace("\n", " ")
            if len(snippet) > 300:
                snippet = snippet[:297] + "..."

            lines.append(f"  [{i}] {wing_name}/{room_name} (sim={similarity})")
            lines.append(f"      {snippet}")
            if source:
                lines.append(f"      src: {source}")
            authored = (meta.get("authored_at") or "")[:10]
            if authored:
                lines.append(f"      authored: {authored}")

        return "\n".join(lines)

    def search_raw(
        self, query: str, wing: str = None, room: str = None, n_results: int = 5
    ) -> list:
        """Return raw dicts instead of formatted text."""
        try:
            col = _get_collection(self.palace_path, create=False)
        except Exception:
            return []

        where = build_where_filter(wing, room)

        kwargs = {
            "query_texts": [query],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        try:
            results = col.query(**kwargs)
        except Exception:
            return []

        metric = _metric_for_collection(col)
        hits = []
        for doc, meta, dist in zip(
            _first_or_empty(results, "documents"),
            _first_or_empty(results, "metadatas"),
            _first_or_empty(results, "distances"),
        ):
            # ChromaDB may return None for doc/meta when a drawer's HNSW entry
            # exists but its metadata/document rows haven't been materialized
            # (partial-flush states, mid-delete, schema upgrade boundaries).
            # Degrade gracefully — the hit still appears with real distance;
            # storage fields show their fallback where content is missing.
            meta = meta or {}
            doc = doc or ""
            hits.append(
                {
                    "text": doc,
                    "wing": meta.get("wing", "unknown"),
                    "room": meta.get("room", "unknown"),
                    "source_file": Path(meta.get("source_file", "?")).name,
                    "similarity": round(_distance_to_similarity(dist, metric), 3),
                    "metadata": meta,
                }
            )
        return hits


# ---------------------------------------------------------------------------
# MemoryStack — unified interface
# ---------------------------------------------------------------------------


class MemoryStack:
    """
    The full 4-layer stack. One class, one palace, everything works.

        stack = MemoryStack()
        print(stack.wake_up())                # L0 + L1 (~600-900 tokens)
        print(stack.recall(wing="my_app"))     # L2 on-demand
        print(stack.search("pricing change"))  # L3 deep search
    """

    def __init__(self, palace_path: str = None, identity_path: str = None):
        cfg = MempalaceConfig()
        self.palace_path = palace_path or cfg.palace_path
        self.identity_path = identity_path or os.path.expanduser("~/.mempalace/identity.txt")

        self.l0 = Layer0(self.identity_path)
        self.l1 = Layer1(self.palace_path)
        self.l2 = Layer2(self.palace_path)
        self.l3 = Layer3(self.palace_path)

    def wake_up(self, wing: str = None) -> str:
        """
        Generate wake-up text: L0 (identity) + L1 (essential story).
        Typically ~600-900 tokens. Inject into system prompt or first message.

        Args:
            wing: Optional wing filter for L1 (project-specific wake-up).
        """
        parts = []

        # L0: Identity
        parts.append(self.l0.render())
        parts.append("")

        # L1: Essential Story
        if wing:
            self.l1.wing = wing
        parts.append(self.l1.generate())

        return "\n".join(parts)

    def recall(self, wing: str = None, room: str = None, n_results: int = 10) -> str:
        """On-demand L2 retrieval filtered by wing/room."""
        return self.l2.retrieve(wing=wing, room=room, n_results=n_results)

    def search(self, query: str, wing: str = None, room: str = None, n_results: int = 5) -> str:
        """Deep L3 semantic search."""
        return self.l3.search(query, wing=wing, room=room, n_results=n_results)

    def status(self) -> dict:
        """Status of all layers."""
        result = {
            "palace_path": self.palace_path,
            "L0_identity": {
                "path": self.identity_path,
                "exists": os.path.exists(self.identity_path),
                "tokens": self.l0.token_estimate(),
            },
            "L1_essential": {
                "description": "Auto-generated from top palace drawers",
            },
            "L2_on_demand": {
                "description": "Wing/room filtered retrieval",
            },
            "L3_deep_search": {
                "description": "Full semantic search via ChromaDB",
            },
        }

        # Count drawers
        try:
            col = _get_collection(self.palace_path, create=False)
            count = col.count()
            result["total_drawers"] = count
        except Exception:
            result["total_drawers"] = 0

        return result


# ---------------------------------------------------------------------------
# CLI (standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json

    def usage():
        print("layers.py -- 4-Layer Memory Stack")
        print()
        print("Usage:")
        print("  python layers.py wake-up              Show L0 + L1")
        print("  python layers.py wake-up --wing=NAME  Wake-up for a specific project")
        print("  python layers.py recall --wing=NAME   On-demand L2 retrieval")
        print("  python layers.py search <query>       Deep L3 search")
        print("  python layers.py status               Show layer status")
        sys.exit(0)

    if len(sys.argv) < 2:
        usage()

    cmd = sys.argv[1]

    # Parse flags
    flags = {}
    positional = []
    for arg in sys.argv[2:]:
        if arg.startswith("--") and "=" in arg:
            key, val = arg.split("=", 1)
            flags[key.lstrip("-")] = val
        elif not arg.startswith("--"):
            positional.append(arg)

    palace_path = flags.get("palace")
    stack = MemoryStack(palace_path=palace_path)

    if cmd in ("wake-up", "wakeup"):
        wing = flags.get("wing")
        text = stack.wake_up(wing=wing)
        tokens = len(text) // 4
        print(f"Wake-up text (~{tokens} tokens):")
        print("=" * 50)
        print(text)

    elif cmd == "recall":
        wing = flags.get("wing")
        room = flags.get("room")
        text = stack.recall(wing=wing, room=room)
        print(text)

    elif cmd == "search":
        query = " ".join(positional) if positional else ""
        if not query:
            print("Usage: python layers.py search <query>")
            sys.exit(1)
        wing = flags.get("wing")
        room = flags.get("room")
        text = stack.search(query, wing=wing, room=room)
        print(text)

    elif cmd == "status":
        s = stack.status()
        print(json.dumps(s, indent=2))

    else:
        usage()
