# Source Metadata and Search Scopes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stamp deterministic source/tier metadata on new drawers and let callers search multiple wings while excluding cold history by default.

**Architecture:** Centralize scalar metadata construction in a small module shared by all miners, extend room config with optional source-kind overrides, and extend both vector and SQLite search paths with identical filters. Missing legacy tier metadata is treated as hot.

**Tech Stack:** Python, YAML, Chroma metadata filters, SQLite FTS, pytest.

## Global Constraints

- Drawer content remains exact verbatim source content.
- Metadata values must be Chroma-compatible scalars.
- Existing singular `wing` input remains compatible; `wing` and `wings` are mutually exclusive.
- Missing `memory_tier` is treated as hot.
- Cold history is never deleted by search filtering.

---

### Task 1: Canonical source metadata builder

**Files:**
- Create: `mempalace/source_metadata.py`
- Modify: `mempalace/miner.py`
- Modify: `mempalace/convo_miner.py`
- Modify: `mempalace/format_miner.py`
- Test: `tests/test_source_metadata.py`

**Interfaces:**
- Produces: `SourceContext` dataclass.
- Produces: `build_source_metadata(context, content, chunk_index) -> dict[str, str]`.
- Produces fields: `source_kind`, `memory_tier`, `source_root`, `source_identity`, `source_revision`, `source_sha256`, `content_sha256`, `source_canonicality` when values are available.

- [ ] **Step 1: Write failing deterministic hash/identity tests**

```python
def test_build_source_metadata_is_scalar_and_deterministic(tmp_path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('x')\n")
    ctx = SourceContext(root=str(tmp_path), source_file=str(source), source_kind="code", memory_tier="hot")
    meta = build_source_metadata(ctx, "print('x')\n", 0)
    assert meta["source_identity"].endswith(":src/app.py")
    assert meta["content_sha256"] == hashlib.sha256(b"print('x')\n").hexdigest()
    assert all(isinstance(value, (str, int, float, bool)) for value in meta.values())
```

- [ ] **Step 2: Run tests and verify missing module failure**

Run: `.venv/bin/pytest tests/test_source_metadata.py -q`

Expected: FAIL importing `mempalace.source_metadata`.

- [ ] **Step 3: Implement builder and merge it into existing drawer metadata**

```python
@dataclass(frozen=True)
class SourceContext:
    root: str
    source_file: str
    source_kind: str
    memory_tier: str = "hot"
    source_revision: str | None = None
    source_canonicality: str = "canonical"

def build_source_metadata(context: SourceContext, content: str, chunk_index: int) -> dict[str, str]:
    relative = os.path.relpath(os.path.realpath(context.source_file), os.path.realpath(context.root))
    return {
        "source_kind": context.source_kind,
        "memory_tier": context.memory_tier,
        "source_root": os.path.realpath(context.root),
        "source_identity": f"{context.source_kind}:{relative}",
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "source_canonicality": context.source_canonicality,
    }
```

- [ ] **Step 4: Run miner suites**

Run: `.venv/bin/pytest tests/test_source_metadata.py tests/test_miner.py tests/test_convo_miner.py tests/test_format_miner.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mempalace/source_metadata.py mempalace/miner.py mempalace/convo_miner.py mempalace/format_miner.py tests/
git commit -m "feat: stamp canonical source metadata on drawers"
```

### Task 2: Configuration validation and room overrides

**Files:**
- Modify: `mempalace/miner.py:483-535`
- Modify: `mempalace/room_detector_local.py:282-305`
- Test: `tests/test_miner.py`
- Test: `tests/test_room_detector_local.py`

**Interfaces:**
- Consumes: Task 1 `SourceContext`.
- Produces validated config keys `source_kind`, `reject_linked_worktrees`, and per-room `source_kind`.

- [ ] **Step 1: Write invalid-enum and documentation-room tests**

```python
def test_load_config_rejects_unknown_source_kind(tmp_path):
    (tmp_path / "mempalace.yaml").write_text("wing: x\nsource_kind: mystery\nrooms: []\n")
    with pytest.raises(ValueError, match="source_kind"):
        load_config(str(tmp_path))

def test_room_source_kind_overrides_project_default(tmp_path):
    config = {"source_kind": "code", "rooms": [{"name": "docs", "source_kind": "documentation"}]}
    assert source_kind_for_room(config, "docs") == "documentation"
```

- [ ] **Step 2: Run and confirm failure**

Run: `.venv/bin/pytest tests/test_miner.py tests/test_room_detector_local.py -k 'source_kind' -q`

Expected: FAIL because config is not validated or routed.

- [ ] **Step 3: Implement strict additive validation**

```python
SOURCE_KINDS = frozenset({"curated", "code", "documentation", "session", "worktree-artifact"})

def source_kind_for_room(config: dict, room_name: str) -> str:
    default = config.get("source_kind", "code")
    room = next((item for item in config.get("rooms", []) if item.get("name") == room_name), {})
    value = room.get("source_kind", default)
    if value not in SOURCE_KINDS:
        raise ValueError(f"invalid source_kind: {value!r}")
    return value
```

- [ ] **Step 4: Run config/miner tests and commit**

Run: `.venv/bin/pytest tests/test_miner.py tests/test_room_detector_local.py -q`

Expected: PASS.

```bash
git add mempalace/miner.py mempalace/room_detector_local.py tests/
git commit -m "feat: configure source kinds by memory room"
```

### Task 3: Multi-wing and tier filters in both search paths

**Files:**
- Modify: `mempalace/searcher.py:240-735`
- Modify: `mempalace/searcher.py:1049-1310`
- Test: `tests/test_searcher.py`

**Interfaces:**
- Produces: `build_where_filter(wing=None, wings=None, room=None, source_file=None, source_kinds=None, include_cold=False)`.
- Produces matching optional parameters on `search_memories()` and `_bm25_only_via_sqlite()`.

- [ ] **Step 1: Write vector-filter and SQLite fallback parity tests**

```python
def test_build_where_filter_combines_wings_source_kind_and_hot_default():
    where = build_where_filter(wings=["se", "se-code"], source_kinds=["code", "curated"])
    assert {"wing": {"$in": ["se", "se-code"]}} in where["$and"]
    assert {"source_kind": {"$in": ["code", "curated"]}} in where["$and"]

def test_legacy_missing_tier_is_hot_in_sqlite_fallback(palace_path, seeded_collection):
    result = _bm25_only_via_sqlite("legacy", str(palace_path), include_cold=False)
    assert any(hit["text"] == "legacy" for hit in result["results"])
```

- [ ] **Step 2: Run focused tests and verify signature/filter failures**

Run: `.venv/bin/pytest tests/test_searcher.py -k 'wings or tier or source_kind' -q`

Expected: FAIL.

- [ ] **Step 3: Implement common filter semantics**

```python
def _validate_scope(wing, wings):
    if wing and wings:
        raise ValueError("wing and wings are mutually exclusive")
    return [wing] if wing else list(wings or [])

def _tier_visible(meta: dict, include_cold: bool) -> bool:
    return include_cold or meta.get("memory_tier", "hot") != "cold"
```

Use Chroma `$in` for multiple wings/source kinds. Apply missing-as-hot logic in vector candidate post-filtering where a `missing OR hot` Chroma expression is unavailable, and in SQLite metadata grouping before BM25 ranking. Fetch enough candidates to fill the requested result count after filtering.

- [ ] **Step 4: Run search tests and commit**

Run: `.venv/bin/pytest tests/test_searcher.py -q`

Expected: PASS.

```bash
git add mempalace/searcher.py tests/test_searcher.py
git commit -m "feat: filter memory search by wings and tiers"
```

### Task 4: MCP search contract and verification

**Files:**
- Modify: `mempalace/mcp_server.py:2011-2075`
- Modify: `mempalace/mcp_server.py` search tool schema
- Test: `tests/test_mcp_server.py`
- Test: `tests/test_searcher.py`

**Interfaces:**
- Adds MCP arguments `wings: list[str]`, `source_kinds: list[str]`, `include_cold: bool`.

- [ ] **Step 1: Write failing MCP argument and compatibility tests**

```python
def test_tool_search_forwards_multiwing_hot_scope(monkeypatch):
    seen = {}
    monkeypatch.setattr(mcp, "search_memories", lambda *a, **kw: seen.update(kw) or {"results": []})
    mcp.tool_search("why OCR", wings=["se", "se-code"], source_kinds=["curated", "code"])
    assert seen["wings"] == ["se", "se-code"]
    assert seen["include_cold"] is False
```

- [ ] **Step 2: Run test and verify failure**

Run: `.venv/bin/pytest tests/test_mcp_server.py -k 'multiwing_hot_scope' -q`

Expected: FAIL on unsupported arguments.

- [ ] **Step 3: Add schema, sanitization, mutual-exclusion error, and forwarding**

Sanitize every wing/source kind, keep existing `wing` callers unchanged, and return a structured error if both singular and plural arguments are supplied.

- [ ] **Step 4: Run tests, lint, and commit**

Run: `.venv/bin/pytest tests/test_mcp_server.py tests/test_searcher.py -q && .venv/bin/ruff check mempalace tests`

Expected: PASS/exit 0.

```bash
git add mempalace/mcp_server.py mempalace/searcher.py tests/
git commit -m "feat: expose scoped memory retrieval over MCP"
```
