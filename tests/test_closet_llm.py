"""Unit tests for the optional LLM-based closet regeneration.

These tests don't hit the network. They mock urllib to verify:
- LLMConfig correctly reads env vars and CLI overrides
- missing config is reported cleanly
- the OpenAI-compatible request shape is correct
- response parsing handles the standard chat-completions payload
"""

import json
import tempfile
from unittest.mock import patch

from mempalace.closet_llm import (
    LLMConfig,
    _call_llm,
    _parsed_to_closet_lines,
    regenerate_closets,
)


# ── LLMConfig ─────────────────────────────────────────────────────────────


class TestLLMConfig:
    def test_reads_env_vars(self, monkeypatch):
        monkeypatch.setenv("LLM_ENDPOINT", "http://localhost:11434/v1")
        monkeypatch.setenv("LLM_KEY", "sk-abc")
        monkeypatch.setenv("LLM_MODEL", "llama3:8b")
        c = LLMConfig()
        assert c.endpoint == "http://localhost:11434/v1"
        assert c.key == "sk-abc"
        assert c.model == "llama3:8b"

    def test_cli_flags_override_env(self, monkeypatch):
        monkeypatch.setenv("LLM_ENDPOINT", "http://env-endpoint/v1")
        monkeypatch.setenv("LLM_MODEL", "env-model")
        c = LLMConfig(endpoint="http://flag-endpoint/v1", model="flag-model")
        assert c.endpoint == "http://flag-endpoint/v1"
        assert c.model == "flag-model"

    def test_trailing_slash_stripped(self):
        c = LLMConfig(endpoint="http://foo/v1/", model="m")
        assert c.endpoint == "http://foo/v1"

    def test_missing_reports_required(self, monkeypatch):
        monkeypatch.delenv("LLM_ENDPOINT", raising=False)
        monkeypatch.delenv("LLM_KEY", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        c = LLMConfig()
        missing = c.missing()
        assert any("ENDPOINT" in m for m in missing)
        assert any("MODEL" in m for m in missing)
        # key is optional
        assert not any("KEY" in m for m in missing)

    def test_key_is_optional(self, monkeypatch):
        monkeypatch.delenv("LLM_KEY", raising=False)
        c = LLMConfig(endpoint="http://local/v1", model="m")
        assert c.missing() == []


# ── _parsed_to_closet_lines ──────────────────────────────────────────────


class TestParsedToLines:
    def test_topics_become_pointers(self):
        parsed = {"topics": ["authentication", "jwt tokens"], "quotes": [], "summary": ""}
        lines = _parsed_to_closet_lines(parsed, ["d1", "d2"], "Alice;Bob")
        assert len(lines) == 2
        assert "authentication|Alice;Bob|→d1,d2" in lines
        assert "jwt tokens|Alice;Bob|→d1,d2" in lines

    def test_quotes_and_summary_included(self):
        parsed = {
            "topics": ["t1"],
            "quotes": ["[Igor] we ship Friday"],
            "summary": "Release planning discussion",
        }
        lines = _parsed_to_closet_lines(parsed, ["d1"], "")
        joined = "\n".join(lines)
        assert "we ship Friday" in joined
        assert "Release planning discussion" in joined

    def test_caps_topics_at_15(self):
        parsed = {"topics": [f"t{i}" for i in range(20)], "quotes": [], "summary": ""}
        lines = _parsed_to_closet_lines(parsed, ["d1"], "")
        assert len(lines) == 15


# ── _call_llm (HTTP mocked) ──────────────────────────────────────────────


class _FakeResp:
    """Mimics urlopen's context-manager response."""

    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


class TestCallLLM:
    def _make_cfg(self):
        return LLMConfig(endpoint="http://localhost:11434/v1", key="sk-test", model="llama3:8b")

    def test_request_shape_and_parsing(self):
        cfg = self._make_cfg()
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "topics": ["postgres"],
                                        "quotes": ["[Igor] migrate now"],
                                        "summary": "db migration",
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 42, "completion_tokens": 17},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            parsed, usage = _call_llm(cfg, "/tmp/test.md", "w", "r", "content body")

        assert parsed["topics"] == ["postgres"]
        assert usage["prompt_tokens"] == 42
        assert captured["url"] == "http://localhost:11434/v1/chat/completions"
        # Authorization header is stored capitalized-then-lowercase depending on urllib version
        auth_vals = {v for k, v in captured["headers"].items() if k.lower() == "authorization"}
        assert "Bearer sk-test" in auth_vals
        assert captured["body"]["model"] == "llama3:8b"
        assert captured["body"]["messages"][0]["role"] == "user"

    def test_omits_auth_header_when_no_key(self):
        cfg = LLMConfig(endpoint="http://localhost:11434/v1", model="llama3:8b")
        captured_headers = {}

        def fake_urlopen(req, timeout=None):
            captured_headers.update({k.lower(): v for k, v in req.header_items()})
            return _FakeResp(
                {
                    "choices": [{"message": {"content": '{"topics":[],"quotes":[],"summary":""}'}}],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _call_llm(cfg, "/tmp/x", "w", "r", "c")

        assert "authorization" not in captured_headers

    def test_strips_code_fences(self):
        cfg = self._make_cfg()
        fenced = '```json\n{"topics":["t1"],"quotes":[],"summary":""}\n```'

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                {
                    "choices": [{"message": {"content": fenced}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            parsed, _ = _call_llm(cfg, "/tmp/x", "w", "r", "c")
        assert parsed == {"topics": ["t1"], "quotes": [], "summary": ""}

    def test_returns_none_on_invalid_json(self):
        cfg = self._make_cfg()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                {
                    "choices": [{"message": {"content": "not json at all"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            parsed, usage = _call_llm(cfg, "/tmp/x", "w", "r", "c")
        assert parsed is None


# ── regenerate_closets error paths ───────────────────────────────────────


class TestRegenerateClosets:
    def test_missing_config_returns_error(self, monkeypatch):
        monkeypatch.delenv("LLM_ENDPOINT", raising=False)
        monkeypatch.delenv("LLM_MODEL", raising=False)
        with tempfile.TemporaryDirectory() as palace:
            result = regenerate_closets(palace)
            assert result["error"] == "missing-config"
            assert any("ENDPOINT" in m for m in result["missing"])

    def test_regen_purges_regex_closets_and_stamps_normalize_version(self, tmp_path):
        """Regression: before the hardening, regex closets for the same
        source survived alongside fresh LLM closets (the old path used a
        bare ``closets_col.delete(ids=...)`` with a swallowed exception).
        Now we go through ``purge_file_closets`` + ``mine_lock`` + stamp
        ``NORMALIZE_VERSION`` so the next mine's stale-version gate doesn't
        treat the LLM closets as leftovers to rebuild over."""
        from mempalace.palace import (
            NORMALIZE_VERSION,
            get_closets_collection,
            get_collection,
            upsert_closet_lines,
        )

        palace = str(tmp_path / "palace")
        # Seed one drawer and a pre-existing regex closet for the same source.
        source = "/proj/story.md"
        drawers = get_collection(palace, create=True)
        drawers.upsert(
            ids=["drawer_01"],
            documents=["Content about JWT authentication."],
            metadatas=[
                {
                    "wing": "project",
                    "room": "auth",
                    "source_file": source,
                    "entities": "",
                }
            ],
        )
        closets = get_closets_collection(palace)
        upsert_closet_lines(
            closets,
            closet_id_base="closet_old_regex",
            lines=["STALE_REGEX_TOPIC|;|→drawer_01"],
            metadata={
                "wing": "project",
                "room": "auth",
                "source_file": source,
                "generated_by": "regex",
            },
        )

        cfg = LLMConfig(endpoint="http://local/v1", model="llama3:8b")

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "topics": ["jwt auth", "session expiry"],
                                        "quotes": [],
                                        "summary": "auth refactor",
                                    }
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            result = regenerate_closets(palace, cfg=cfg)

        assert result["processed"] == 1 and result["failed"] == 0

        # Every surviving closet for this source must be LLM-generated and
        # must carry the current NORMALIZE_VERSION.
        survivors = closets.get(where={"source_file": source}, include=["documents", "metadatas"])
        assert survivors["ids"], "LLM closets should have been written"
        joined = "\n".join(survivors["documents"])
        assert (
            "STALE_REGEX_TOPIC" not in joined
        ), "pre-existing regex closet was not purged before LLM write"
        assert "jwt auth" in joined
        for meta in survivors["metadatas"]:
            assert meta.get("generated_by", "").startswith("llm:")
            assert meta.get("normalize_version") == NORMALIZE_VERSION

    def test_regen_paginates_drawer_fetch(self, tmp_path):
        """Regression for #1073: drawers_col.get must be paginated at
        batch_size=5000. A single get(limit=total, ...) on a palace with
        more than SQLite's SQLITE_MAX_VARIABLE_NUMBER (32766) drawers
        blows up inside chromadb. Matches the miner.status pattern
        introduced in #851 (see #802, #850, #1073)."""
        from mempalace import closet_llm as closet_llm_mod

        palace = str(tmp_path / "palace")

        # Build a fake collection: 12_000 drawers across 3 source files,
        # enough to force 3 batches of batch_size=5000 (5000 + 5000 + 2000).
        n_drawers = 12_000
        ids = [f"d{i:05d}" for i in range(n_drawers)]
        docs = [f"doc body {i}" for i in range(n_drawers)]
        metas = [
            {
                "wing": "w",
                "room": "r",
                "source_file": f"/src/file_{i % 3}.md",
                "entities": "",
            }
            for i in range(n_drawers)
        ]

        get_calls: list = []

        class FakeDrawersCol:
            def count(self):
                return n_drawers

            def get(self, limit=None, offset=0, include=None, **kwargs):
                get_calls.append({"limit": limit, "offset": offset, "include": include})
                end = min(offset + (limit or n_drawers), n_drawers)
                return {
                    "ids": ids[offset:end],
                    "documents": docs[offset:end],
                    "metadatas": metas[offset:end],
                }

        class FakeClosetsCol:
            """Accept the purge + upsert calls the success path makes."""

            def get(self, *a, **kw):
                return {"ids": [], "documents": [], "metadatas": []}

            def delete(self, *a, **kw):
                return None

            def upsert(self, *a, **kw):
                return None

        fake_drawers = FakeDrawersCol()
        fake_closets = FakeClosetsCol()

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                {
                    "choices": [
                        {"message": {"content": '{"topics":["t1"],"quotes":[],"summary":""}'}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        cfg = LLMConfig(endpoint="http://local/v1", model="m")

        with (
            patch.object(closet_llm_mod, "get_collection", return_value=fake_drawers),
            patch.object(closet_llm_mod, "get_closets_collection", return_value=fake_closets),
            patch.object(closet_llm_mod, "purge_file_closets", return_value=None),
            patch.object(closet_llm_mod, "upsert_closet_lines", return_value=None),
            patch("urllib.request.urlopen", side_effect=fake_urlopen),
        ):
            result = regenerate_closets(palace, cfg=cfg, dry_run=True)

        # Three paginated calls: (limit=5000, offset=0), (5000, 5000), (5000, 10000).
        assert len(get_calls) == 3, f"expected 3 batched fetches, got {len(get_calls)}"
        for call in get_calls:
            assert (
                call["limit"] == 5000
            ), f"batch must be 5000 — got {call['limit']} (would risk SQLITE_MAX_VARIABLE_NUMBER)"
            # include must still request both documents and metadatas
            assert "documents" in call["include"]
            assert "metadatas" in call["include"]
        assert [c["offset"] for c in get_calls] == [0, 5000, 10_000]

        # by_source aggregation must be preserved exactly across batches:
        # 12_000 drawers, 3 source files → 4_000 drawers each.
        # dry_run=True short-circuits LLM calls but still walks by_source.
        assert result.get("processed", 0) == 0  # dry_run
        # Verify no single call tried to pull more than batch_size.
        assert max(c["limit"] for c in get_calls) <= 5000

    def test_regen_by_source_aggregates_across_batches(self, tmp_path):
        """Pagination must not change the by_source grouping — drawers for
        the same source_file split across batches still land in one group."""
        from mempalace import closet_llm as closet_llm_mod

        palace = str(tmp_path / "palace")

        # 7_500 drawers, alternating between two source files → forces
        # splits across the 5000/2500 boundary. Each source ends up with
        # 3_750 drawers after regrouping.
        n_drawers = 7_500
        ids = [f"d{i:05d}" for i in range(n_drawers)]
        docs = [f"body-{i}" for i in range(n_drawers)]
        metas = [
            {
                "wing": "w",
                "room": "r",
                "source_file": f"/src/file_{i % 2}.md",
                "entities": "",
            }
            for i in range(n_drawers)
        ]

        captured_sources: dict = {}

        class FakeDrawersCol:
            def count(self):
                return n_drawers

            def get(self, limit=None, offset=0, include=None, **kwargs):
                end = min(offset + (limit or n_drawers), n_drawers)
                return {
                    "ids": ids[offset:end],
                    "documents": docs[offset:end],
                    "metadatas": metas[offset:end],
                }

        class FakeClosetsCol:
            def get(self, *a, **kw):
                return {"ids": [], "documents": [], "metadatas": []}

            def delete(self, *a, **kw):
                return None

            def upsert(self, *a, **kw):
                return None

        # Hook _call_llm to inspect what regenerate_closets aggregated
        # per source before the HTTP boundary.
        real_call_llm = closet_llm_mod._call_llm

        def spying_call_llm(cfg, source_file, wing, room, content):
            captured_sources[source_file] = content
            return (
                {"topics": ["t"], "quotes": [], "summary": ""},
                {"prompt_tokens": 1, "completion_tokens": 1},
            )

        cfg = LLMConfig(endpoint="http://local/v1", model="m")

        with (
            patch.object(closet_llm_mod, "get_collection", return_value=FakeDrawersCol()),
            patch.object(closet_llm_mod, "get_closets_collection", return_value=FakeClosetsCol()),
            patch.object(closet_llm_mod, "purge_file_closets", return_value=None),
            patch.object(closet_llm_mod, "upsert_closet_lines", return_value=None),
            patch.object(closet_llm_mod, "_call_llm", side_effect=spying_call_llm),
        ):
            regenerate_closets(palace, cfg=cfg)

        # Both sources survived the pagination boundary.
        assert set(captured_sources.keys()) == {"/src/file_0.md", "/src/file_1.md"}
        # Each source accumulated exactly 3_750 drawer bodies, concatenated
        # with the "\n\n" separator the regenerate path uses.
        for source, content in captured_sources.items():
            assert content.count("\n\n") == 3_749, (
                f"{source}: expected 3_750 chunks joined (3_749 separators), "
                f"got {content.count(chr(10) + chr(10)) + 1}"
            )

        # Silence unused-var lint.
        assert real_call_llm is not None

    def test_regen_uses_basename_not_split_slash(self, tmp_path, monkeypatch):
        """Regression: the old closet_id base used ``source.split('/')[-1]``
        which silently degrades on Windows paths (``C:\\proj\\a.md`` →
        the whole string). ``os.path.basename`` handles both separators."""
        from mempalace.palace import get_collection, get_closets_collection

        palace = str(tmp_path / "palace")
        # Use a path whose basename differs between '/' split and
        # os.path.basename only on a platform-aware function, but verify
        # at minimum that IDs encode just the filename, not the full path.
        source = "/deep/nested/project/dir/mydoc.md"
        drawers = get_collection(palace, create=True)
        drawers.upsert(
            ids=["d1"],
            documents=["body"],
            metadatas=[{"wing": "w", "room": "r", "source_file": source, "entities": ""}],
        )

        cfg = LLMConfig(endpoint="http://local/v1", model="m")

        def fake_urlopen(req, timeout=None):
            return _FakeResp(
                {
                    "choices": [
                        {"message": {"content": '{"topics":["t1"],"quotes":[],"summary":""}'}}
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            )

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            regenerate_closets(palace, cfg=cfg)

        closets = get_closets_collection(palace)
        ids = closets.get(where={"source_file": source}).get("ids", [])
        assert ids
        # IDs must not leak the full path (would happen if we used
        # source.split('/')[-1] on Windows, or forgot to strip entirely).
        for cid in ids:
            assert "/" not in cid
            assert "mydoc.md" in cid
