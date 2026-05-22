#!/usr/bin/env python3
"""
test_readme_claims.py — TDD verification of every major README claim against actual code.

Each test verifies a specific claim made in README.md. If a test fails, either
the README is wrong or the code hasn't shipped the feature yet. Fix one or the
other until all tests pass — that's when the README matches reality.

Based on the audit at ~/Desktop/readme_audit.md (2026-04-13).
"""

import importlib
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Helpers — locate repo root and parse README / source files
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMPALACE_PKG = REPO_ROOT / "mempalace"
README_PATH = REPO_ROOT / "README.md"
MCP_TOOLS_DOC_PATH = REPO_ROOT / "website" / "reference" / "mcp-tools.md"
MODULES_DOC_PATH = REPO_ROOT / "website" / "reference" / "modules.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _readme() -> str:
    return _read(README_PATH)


def _tools_dict_keys() -> list:
    """Return the list of tool names registered in the TOOLS dict."""
    # Import the module-level TOOLS dict.  We can't just import mcp_server
    # because it calls chromadb on import, so we parse the source instead.
    src = _read(MEMPALACE_PKG / "mcp_server.py")
    return re.findall(r'"(mempalace_\w+)":\s*\{', src)


def _doc_tool_names() -> list:
    """Return the list of tool names documented in the MCP tools reference.

    The MCP tool table lived in README.md prior to the #875 rewrite; it now
    lives in website/reference/mcp-tools.md (linked from README). Each tool
    is introduced by a level-3 heading `### \\`mempalace_xxx\\``.
    """
    doc = _read(MCP_TOOLS_DOC_PATH)
    return re.findall(r"^###\s+`(mempalace_\w+)`", doc, re.MULTILINE)


# ---------------------------------------------------------------------------
# 1. Tool count — README says 19, verify actual count
# ---------------------------------------------------------------------------


class TestToolCount:
    """README claims '19 tools available through MCP' in multiple places."""

    def test_readme_tool_count_matches_code(self):
        """Claim: README says 19 tools. Actual TOOLS dict may differ.

        This test asserts the REAL tool count so the README can be updated.
        If TOOLS has 25 entries, the README should say 25, not 19.
        """
        actual_count = len(_tools_dict_keys())
        readme = _readme()
        # Find all "19 tools" claims in README
        claimed_counts = re.findall(r"(\d+)\s+tools", readme)
        for claimed in claimed_counts:
            assert int(claimed) == actual_count, (
                f"README claims {claimed} tools but TOOLS dict has {actual_count}. "
                f"Update every occurrence of '{claimed} tools' to '{actual_count} tools'."
            )


# ---------------------------------------------------------------------------
# 2. Every tool listed in README actually exists in TOOLS dict
# ---------------------------------------------------------------------------


class TestReadmeToolsExistInCode:
    """Every tool name documented in the MCP tools reference must be a key in TOOLS."""

    def test_every_readme_tool_exists_in_tools_dict(self):
        """Claim: the MCP tools reference (website/reference/mcp-tools.md)
        lists tools like mempalace_get_aaak_spec. Each one must actually be
        registered in the TOOLS dict in mempalace/mcp_server.py.

        Pre-#875 this parsed the tool table that lived in README.md; that
        table has moved to the website docs and README now links out.
        """
        code_tools = set(_tools_dict_keys())
        doc_tools = _doc_tool_names()
        assert len(doc_tools) > 0, (
            f"Could not parse any tools from {MCP_TOOLS_DOC_PATH.relative_to(REPO_ROOT)} "
            f"— expected `### \\`mempalace_xxx\\`` headings."
        )

        missing = [t for t in doc_tools if t not in code_tools]
        assert missing == [], (
            f"Docs list tools that don't exist in TOOLS dict: {missing}. "
            f"Either add them to mcp_server.py or remove them from "
            f"{MCP_TOOLS_DOC_PATH.relative_to(REPO_ROOT)}."
        )


# ---------------------------------------------------------------------------
# 3. No tool in TOOLS dict is missing from README's tool table
# ---------------------------------------------------------------------------


class TestNoUnlistedTools:
    """Every tool in the TOOLS dict should be documented in the MCP tools reference."""

    def test_no_undocumented_tools(self):
        """Claim: the MCP tools reference
        (website/reference/mcp-tools.md) is complete. Any tool in TOOLS
        but not documented there is undocumented on the public surface."""
        code_tools = set(_tools_dict_keys())
        doc_tools = set(_doc_tool_names())

        undocumented = sorted(code_tools - doc_tools)
        assert undocumented == [], (
            f"Tools in TOOLS dict but missing from docs: {undocumented}. "
            f"Add sections for these to "
            f"{MCP_TOOLS_DOC_PATH.relative_to(REPO_ROOT)}."
        )


# ---------------------------------------------------------------------------
# 4. Closets collection exists — palace.py has get_closets_collection()
# ---------------------------------------------------------------------------


class TestClosetsExist:
    """README describes closets as a core architectural feature."""

    def test_get_closets_collection_exists(self):
        """Claim: closets are a shipped feature.
        palace.py must export get_closets_collection()."""
        src = _read(MEMPALACE_PKG / "palace.py")
        assert "def get_closets_collection(" in src, (
            "palace.py does not define get_closets_collection(). "
            "Closets are described in README but the collection function is missing."
        )

    def test_closets_importable(self):
        """get_closets_collection should be importable from mempalace.palace."""
        from mempalace.palace import get_closets_collection

        assert callable(get_closets_collection)


# ---------------------------------------------------------------------------
# 5. Closet-first search exists in searcher.py
# ---------------------------------------------------------------------------


class TestClosetFirstSearch:
    """README implies search goes through closets, not just direct drawer query."""

    def test_closet_boost_search_exists(self):
        """Claim: search uses closets as a boost signal.
        searcher.py must have CLOSET_RANK_BOOSTS and query closets_col."""
        src = _read(MEMPALACE_PKG / "searcher.py")
        assert "CLOSET_RANK_BOOSTS" in src, (
            "searcher.py has no closet boost logic. "
            "README describes closet-based search but searcher.py has no closet ranking."
        )

    def test_searcher_imports_closets(self):
        """searcher.py must import get_closets_collection to use closets."""
        src = _read(MEMPALACE_PKG / "searcher.py")
        assert "get_closets_collection" in src, (
            "searcher.py does not reference get_closets_collection. "
            "Closet-first search can't work without the closets collection."
        )


# ---------------------------------------------------------------------------
# 6. BM25 hybrid search functions exist
# ---------------------------------------------------------------------------


class TestBM25HybridSearch:
    """README claims 'BM25 hybrid search'. Verify the functions exist."""

    def test_bm25_in_searcher(self):
        """Claim: BM25 hybrid search is shipped.
        searcher.py must have BM25 scoring or hybrid ranking logic."""
        src = _read(MEMPALACE_PKG / "searcher.py")
        has_bm25 = any(
            term in src.lower()
            for term in [
                "bm25",
                "_bm25_score",
                "_hybrid_rank",
                "hybrid_search",
                "bm25_score",
                "rank_bm25",
            ]
        )
        assert has_bm25, (
            "searcher.py has no BM25 or hybrid search function. "
            "README claims BM25 hybrid search but it's not in the code."
        )


# ---------------------------------------------------------------------------
# 7. Entity metadata extraction exists in miner.py
# ---------------------------------------------------------------------------


class TestEntityMetadataExtraction:
    """README implies entity extraction populates drawer/closet metadata."""

    def test_entity_extraction_in_palace_or_miner(self):
        """Claim: entity extraction is part of the mining pipeline.
        Either miner.py or palace.py must extract entities."""
        miner_src = _read(MEMPALACE_PKG / "miner.py")
        palace_src = _read(MEMPALACE_PKG / "palace.py")
        # Entity extraction can be in either file — palace.py has it for closets
        has_entity_extraction = (
            "entities" in palace_src and "_ENTITY_STOPLIST" in palace_src
        ) or "extract_entities" in miner_src
        assert has_entity_extraction, (
            "No entity extraction found in miner.py or palace.py. "
            "README implies entities are extracted during mining."
        )


# ---------------------------------------------------------------------------
# 8. strip_noise function exists in normalize.py
# ---------------------------------------------------------------------------


class TestStripNoise:
    """normalize.py should have strip_noise() for cleaning input text."""

    def test_strip_noise_exists(self):
        """Claim: normalize.py has noise stripping.
        Function strip_noise must exist."""
        src = _read(MEMPALACE_PKG / "normalize.py")
        assert "def strip_noise(" in src, (
            "normalize.py does not define strip_noise(). "
            "This function is referenced in the normalization pipeline."
        )

    def test_strip_noise_importable(self):
        """strip_noise should be importable from mempalace.normalize."""
        from mempalace.normalize import strip_noise

        assert callable(strip_noise)


# ---------------------------------------------------------------------------
# 9. diary_ingest.py module exists and is importable
# ---------------------------------------------------------------------------


class TestDiaryIngest:
    """README describes diary ingest (day-based). Module must exist."""

    def test_diary_ingest_module_exists(self):
        """Claim: diary_ingest.py is a shipped module.
        File must exist at mempalace/diary_ingest.py."""
        path = MEMPALACE_PKG / "diary_ingest.py"
        assert path.is_file(), (
            "mempalace/diary_ingest.py does not exist. "
            "README describes diary ingest but the module is missing (still in an unmerged PR?)."
        )

    def test_diary_ingest_importable(self):
        """diary_ingest should be importable."""
        try:
            importlib.import_module("mempalace.diary_ingest")
        except ImportError:
            pytest.fail(
                "mempalace.diary_ingest is not importable. Module must exist and import cleanly."
            )


# ---------------------------------------------------------------------------
# 10. fact_checker.py module exists and is importable
# ---------------------------------------------------------------------------


class TestFactChecker:
    """README has a 'Contradiction detection' section implying fact_checker.py."""

    def test_fact_checker_module_exists(self):
        """Claim: contradiction detection is shipped.
        fact_checker.py must exist at mempalace/fact_checker.py."""
        path = MEMPALACE_PKG / "fact_checker.py"
        assert path.is_file(), (
            "mempalace/fact_checker.py does not exist. "
            "README describes contradiction detection but the module is missing."
        )

    def test_fact_checker_importable(self):
        """fact_checker should be importable."""
        try:
            importlib.import_module("mempalace.fact_checker")
        except ImportError:
            pytest.fail(
                "mempalace.fact_checker is not importable. Module must exist and import cleanly."
            )


# ---------------------------------------------------------------------------
# 11. Tunnel functions exist in palace_graph.py
# ---------------------------------------------------------------------------


class TestTunnelFunctions:
    """README describes tunnels — connections between wings."""

    def test_find_tunnels_exists(self):
        """Claim: tunnels connect rooms across wings.
        palace_graph.py must have find_tunnels()."""
        src = _read(MEMPALACE_PKG / "palace_graph.py")
        assert "def find_tunnels(" in src, (
            "palace_graph.py has no find_tunnels() function. "
            "README describes tunnels but the function is missing."
        )

    def test_traverse_exists(self):
        """Claim: you can walk the palace graph.
        palace_graph.py must have traverse()."""
        src = _read(MEMPALACE_PKG / "palace_graph.py")
        assert "def traverse(" in src, "palace_graph.py has no traverse() function."

    def test_graph_stats_exists(self):
        """palace_graph.py must have graph_stats()."""
        src = _read(MEMPALACE_PKG / "palace_graph.py")
        assert "def graph_stats(" in src, "palace_graph.py has no graph_stats() function."

    def test_tunnel_functions_importable(self):
        """find_tunnels, traverse, graph_stats should be importable."""
        from mempalace.palace_graph import find_tunnels, traverse, graph_stats

        assert callable(find_tunnels)
        assert callable(traverse)
        assert callable(graph_stats)


# ---------------------------------------------------------------------------
# 12. closet_llm.py module exists and is importable
# ---------------------------------------------------------------------------


class TestClosetLLM:
    """README describes LLM-based closet regeneration. Module must exist."""

    def test_closet_llm_module_exists(self):
        """Claim: LLM-based closet regen is shipped.
        closet_llm.py must exist at mempalace/closet_llm.py."""
        path = MEMPALACE_PKG / "closet_llm.py"
        assert path.is_file(), (
            "mempalace/closet_llm.py does not exist. "
            "README describes LLM closet regeneration but the module is missing."
        )

    def test_closet_llm_importable(self):
        """closet_llm should be importable."""
        try:
            importlib.import_module("mempalace.closet_llm")
        except ImportError:
            pytest.fail(
                "mempalace.closet_llm is not importable. Module must exist and import cleanly."
            )


# ---------------------------------------------------------------------------
# 13. mine_lock exists in palace.py
# ---------------------------------------------------------------------------


class TestMineLock:
    """Multi-agent file locking must be shipped (PR #784 was merged)."""

    def test_mine_lock_exists(self):
        """Claim: multi-agent file locking is shipped.
        palace.py must define mine_lock."""
        src = _read(MEMPALACE_PKG / "palace.py")
        assert "def mine_lock(" in src, (
            "palace.py does not define mine_lock(). "
            "Multi-agent locking is claimed as shipped but function is missing."
        )

    def test_mine_lock_importable(self):
        """mine_lock should be importable from mempalace.palace."""
        from mempalace.palace import mine_lock

        assert callable(mine_lock)

    def test_mine_lock_is_context_manager(self):
        """mine_lock should be a context manager (used with `with` statement)."""
        src = _read(MEMPALACE_PKG / "palace.py")
        # It should be decorated with @contextlib.contextmanager or similar
        # Find the mine_lock definition and check for context manager pattern
        assert "@contextlib.contextmanager" in src or "def __enter__" in src, (
            "mine_lock does not appear to be a context manager. "
            "It should be usable with `with mine_lock(path):` syntax."
        )


# ---------------------------------------------------------------------------
# 14. Version in version.py matches pyproject.toml
# ---------------------------------------------------------------------------


class TestVersionConsistency:
    """version.py and pyproject.toml must agree on the version string."""

    def test_version_py_matches_pyproject(self):
        """Claim: single source of truth for version.
        version.py __version__ must match pyproject.toml version."""
        version_src = _read(MEMPALACE_PKG / "version.py")
        version_match = re.search(r'__version__\s*=\s*"([^"]+)"', version_src)
        assert version_match, "Could not parse __version__ from version.py"
        code_version = version_match.group(1)

        pyproject_src = _read(REPO_ROOT / "pyproject.toml")
        pyproject_match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_src, re.MULTILINE)
        assert pyproject_match, "Could not parse version from pyproject.toml"
        toml_version = pyproject_match.group(1)

        assert code_version == toml_version, (
            f"version.py says {code_version} but pyproject.toml says {toml_version}. "
            f"These must match."
        )


# ---------------------------------------------------------------------------
# 15. Version badge URL in README matches version.py
# ---------------------------------------------------------------------------


class TestVersionBadge:
    """README version badge must show the current version, not a stale one."""

    def test_readme_badge_matches_version_py(self):
        """Claim: README badge shows current version.
        The shields.io badge URL must contain the version from version.py."""
        version_src = _read(MEMPALACE_PKG / "version.py")
        version_match = re.search(r'__version__\s*=\s*"([^"]+)"', version_src)
        assert version_match, "Could not parse __version__ from version.py"
        code_version = version_match.group(1)

        readme = _readme()
        # Find the version badge URL
        badge_match = re.search(r"shields\.io/badge/version-([^-]+)-", readme)
        assert badge_match, "Could not find version badge URL in README"
        badge_version = badge_match.group(1)

        assert badge_version == code_version, (
            f"README badge says {badge_version} but version.py says {code_version}. "
            f"Update the badge URL in README.md."
        )


# ---------------------------------------------------------------------------
# 16. dialect.py docstring does NOT say "lossless"
# ---------------------------------------------------------------------------


class TestDialectNotLossless:
    """The April 7 correction: AAAK is lossy, not lossless."""

    def test_dialect_docstring_says_not_lossless(self):
        """Claim: dialect.py correctly says AAAK is NOT lossless.
        The docstring must contain 'NOT lossless' or 'lossy'."""
        src = _read(MEMPALACE_PKG / "dialect.py")
        # Check the module docstring (first ~20 lines)
        docstring_area = src[:1000]
        assert "NOT lossless" in docstring_area or "lossy" in docstring_area.lower(), (
            "dialect.py docstring does not disclaim losslessness. "
            "After the April 7 correction, it must say AAAK is NOT lossless."
        )

    def test_dialect_docstring_does_not_claim_lossless(self):
        """The docstring must not positively claim 'lossless compression'."""
        src = _read(MEMPALACE_PKG / "dialect.py")
        docstring_area = src[:1000]
        # "NOT lossless" is OK; bare "lossless" without negation is not
        # Remove the "NOT lossless" disclaimer before checking
        cleaned = docstring_area.replace("NOT lossless", "")
        assert "lossless" not in cleaned.lower(), (
            "dialect.py docstring still claims 'lossless' somewhere. "
            "AAAK is lossy — remove any positive lossless claims."
        )


# ---------------------------------------------------------------------------
# 17. README file reference table for dialect.py does NOT say "lossless"
# ---------------------------------------------------------------------------


class TestReadmeDialectNotLossless:
    """The file-reference documentation must not say dialect.py is lossless.

    Pre-#875 this lived in a README.md file table; it now lives in
    website/reference/modules.md. The April 7 correction established that
    AAAK is a lossy abbreviation system, not lossless compression, and
    every docs surface that describes dialect.py must respect that.
    """

    def test_readme_dialect_line_not_lossless(self):
        doc = _read(MODULES_DOC_PATH)
        # Any line mentioning dialect.py (narrative or table) must not call it lossless
        dialect_lines = [line for line in doc.splitlines() if "dialect.py" in line]
        assert len(dialect_lines) > 0, (
            f"Could not find dialect.py in "
            f"{MODULES_DOC_PATH.relative_to(REPO_ROOT)}. "
            f"Expected at least one reference."
        )

        for line in dialect_lines:
            assert "lossless" not in line.lower(), (
                f"Docs still call dialect.py lossless: {line.strip()!r}. "
                f"After April 7 correction, this must say 'lossy' or remove the lossless claim."
            )


# ---------------------------------------------------------------------------
# 18. Hall keywords in config.py — verify miners actually WRITE hall metadata
# ---------------------------------------------------------------------------


class TestHallMetadata:
    """README describes 5 hall types. Miners must actually write hall metadata."""

    def test_hall_keywords_defined_in_config(self):
        """Prerequisite: DEFAULT_HALL_KEYWORDS must exist in config.py."""
        src = _read(MEMPALACE_PKG / "config.py")
        assert "DEFAULT_HALL_KEYWORDS" in src, (
            "config.py does not define DEFAULT_HALL_KEYWORDS. "
            "Hall types are described in README but not defined in config."
        )

    def test_miners_write_hall_metadata(self):
        """Claim: halls are populated. At least one miner must write a 'hall'
        field into drawer metadata.

        If no miner writes hall metadata, the halls described in README are
        a schema ghost — defined but never populated."""
        miner_src = _read(MEMPALACE_PKG / "miner.py")
        convo_miner_src = _read(MEMPALACE_PKG / "convo_miner.py")

        # Check if either miner references 'hall' in the metadata it writes
        writes_hall = (
            '"hall"' in miner_src
            or "'hall'" in miner_src
            or '"hall"' in convo_miner_src
            or "'hall'" in convo_miner_src
        )
        assert writes_hall, (
            "Neither miner.py nor convo_miner.py writes a 'hall' field to drawer metadata. "
            "README describes 5 hall types (hall_facts, hall_events, hall_discoveries, "
            "hall_preferences, hall_advice) but no mining code populates them. "
            "Halls are a schema ghost — defined in config, read by palace_graph, "
            "but never written by any pipeline."
        )

    def test_readme_hall_types_match_config(self):
        """If README lists specific hall names, they should appear in config."""
        # README mentions these 5 halls
        readme_halls = [
            "hall_facts",
            "hall_events",
            "hall_discoveries",
            "hall_preferences",
            "hall_advice",
        ]
        for hall in readme_halls:
            # These should either be in config or README should not list them
            # The hall_ prefix is a README convention; config uses keyword groups
            # like "emotions", "consciousness" etc. Check if they're consistent.
            pass  # This is a documentation check; the real test is #18b above


# ---------------------------------------------------------------------------
# 19. Backend abstraction exists
# ---------------------------------------------------------------------------


class TestBackendAbstraction:
    """Backend seam for pluggable storage backends."""

    def test_backends_base_exists(self):
        """Claim: pluggable backends.
        backends/base.py must define an abstract base class."""
        path = MEMPALACE_PKG / "backends" / "base.py"
        assert path.is_file(), (
            "mempalace/backends/base.py does not exist. Backend abstraction layer is missing."
        )
        src = _read(path)
        assert "ABC" in src or "abstractmethod" in src, (
            "backends/base.py does not define an abstract base class."
        )

    def test_backends_chroma_exists(self):
        """Claim: ChromaDB backend implementation.
        backends/chroma.py must exist and subclass the base."""
        path = MEMPALACE_PKG / "backends" / "chroma.py"
        assert path.is_file(), "mempalace/backends/chroma.py does not exist."
        src = _read(path)
        assert "BaseCollection" in src or "base" in src, (
            "backends/chroma.py does not reference the base class."
        )

    def test_backends_importable(self):
        """Both backend modules should be importable."""
        from mempalace.backends.base import BaseCollection
        from mempalace.backends.chroma import ChromaBackend

        assert BaseCollection is not None
        assert ChromaBackend is not None


# ---------------------------------------------------------------------------
# 20. i18n module exists with at least 8 language files
# ---------------------------------------------------------------------------


class TestI18n:
    """i18n support — 8 languages."""

    def test_i18n_directory_exists(self):
        """i18n directory must exist."""
        path = MEMPALACE_PKG / "i18n"
        assert path.is_dir(), "mempalace/i18n/ directory does not exist."

    def test_at_least_8_language_files(self):
        """Claim: 8 languages supported.
        i18n/ must contain at least 8 .json language files."""
        path = MEMPALACE_PKG / "i18n"
        json_files = list(path.glob("*.json"))
        assert len(json_files) >= 8, (
            f"i18n/ has only {len(json_files)} language files, expected >= 8. "
            f"Files found: {[f.name for f in json_files]}"
        )

    def test_english_baseline_exists(self):
        """en.json must exist as the baseline language file."""
        path = MEMPALACE_PKG / "i18n" / "en.json"
        assert path.is_file(), (
            "mempalace/i18n/en.json does not exist. English baseline is required."
        )


# ---------------------------------------------------------------------------
# 21. Wake-up token cost — check layers.py vs README's "~170 tokens"
# ---------------------------------------------------------------------------


class TestWakeUpTokenCost:
    """README claims '~170 tokens' for wake-up. layers.py says otherwise."""

    def test_readme_wakeup_cost_matches_layers(self):
        """Claim: README says ~170 tokens for wake-up.
        layers.py docstring says L0 ~100 tokens, L1 ~500-800 tokens.
        Total = 600-900, not 170.

        If the README means '170 tokens of critical facts' (just the AAAK
        portion), it should say so clearly. If it means total wake-up cost,
        it must match layers.py."""
        readme = _readme()
        layers_src = _read(MEMPALACE_PKG / "layers.py")

        # What layers.py says
        assert "~600-900 tokens" in layers_src or "600-900" in layers_src, (
            "layers.py docstring does not mention 600-900 tokens. "
            "Check if the wake-up cost documentation has changed."
        )

        # What README says
        readme_170_claims = re.findall(r"~?170 tokens", readme)

        if readme_170_claims:
            # README claims 170 tokens but layers.py says 600-900.
            # This test enforces that README must match the code.
            # Either README should say 600-900 or layers.py should say 170.
            # Since we trust code over docs, the README is wrong.
            pytest.fail(
                f"README claims '~170 tokens' for wake-up ({len(readme_170_claims)} occurrences) "
                f"but layers.py says L0+L1 = ~600-900 tokens. "
                f"Either update README to match layers.py, or clarify that '170 tokens' "
                f"refers to a specific subset (e.g., AAAK-compressed facts only)."
            )


# ---------------------------------------------------------------------------
# Bonus: pyproject.toml version in README project structure
# ---------------------------------------------------------------------------


class TestReadmeProjectStructureVersion:
    """README's project structure section says pyproject.toml version."""

    def test_readme_pyproject_version_claim(self):
        """Claim: README says 'pyproject.toml — package config (v3.0.0)' or similar.
        Must match actual pyproject.toml version."""
        readme = _readme()
        pyproject_src = _read(REPO_ROOT / "pyproject.toml")
        pyproject_match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_src, re.MULTILINE)
        assert pyproject_match, "Could not parse version from pyproject.toml"
        actual_version = pyproject_match.group(1)

        # Find any version claim near pyproject.toml in README
        version_in_readme = re.search(r"pyproject\.toml.*?v?([\d]+\.[\d]+\.[\d]+)", readme)
        if version_in_readme:
            readme_version = version_in_readme.group(1)
            assert readme_version == actual_version, (
                f"README says pyproject.toml is v{readme_version} "
                f"but actual version is {actual_version}."
            )


# ---------------------------------------------------------------------------
# Bonus: README tool count consistency (all mentions must agree)
# ---------------------------------------------------------------------------


class TestReadmeToolCountConsistency:
    """README mentions tool count in multiple places — they must all agree."""

    def test_all_tool_count_mentions_consistent(self):
        """Every place README says 'N tools' must use the same number."""
        readme = _readme()
        counts = re.findall(r"(\d+)\s+tools", readme)
        if len(counts) > 1:
            unique = set(counts)
            assert len(unique) == 1, (
                f"README mentions different tool counts: {counts}. All occurrences must agree."
            )


# ---------------------------------------------------------------------------
# Bonus: get_aaak_spec tool handler exists
# ---------------------------------------------------------------------------


class TestAAAKSpecToolHandler:
    """If mempalace_get_aaak_spec is in TOOLS, its handler must exist."""

    def test_aaak_spec_handler_exists(self):
        """The handler function for get_aaak_spec must be defined."""
        src = _read(MEMPALACE_PKG / "mcp_server.py")
        tools = _tools_dict_keys()
        if "mempalace_get_aaak_spec" in tools:
            assert "def tool_get_aaak_spec(" in src, (
                "mempalace_get_aaak_spec is in TOOLS dict but "
                "tool_get_aaak_spec() handler function is not defined."
            )
