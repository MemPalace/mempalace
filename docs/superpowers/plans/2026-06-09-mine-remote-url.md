# mine --remote-url Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `--remote-url` and `--remote-token` flags to `mempalace mine` so drawers are POSTed to a remote HTTP mempalace server instead of written to local ChromaDB.

**Architecture:** A new `RemoteCollection` class implements the existing `BaseCollection` interface. `mine()` and `mine_convos()` receive an optional `remote_url`/`remote_token`; when set, they use `RemoteCollection` instead of calling `get_collection(palace_path)`. All existing mine logic is unchanged. A local state file (`~/.mempalace/remote_state/{hash}.json`) tracks which source files have been fully sent so re-runs skip them without querying the server.

**Tech Stack:** Python stdlib only (`urllib.request`, `json`, `hashlib`). Tests use `unittest.mock`. No new dependencies.

---

### Task 1: `RemoteCollection` — core class

**Files:**
- Create: `mempalace/backends/remote.py`
- Test: `tests/test_remote_collection.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_remote_collection.py`:

```python
"""Tests for RemoteCollection."""
import json
import os
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest


def _make_response(body: dict, status: int = 200):
    """Build a mock urllib response."""
    raw = json.dumps(body).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _http_error(code: int):
    return urllib.error.HTTPError(url="http://x", code=code, msg="", hdrs=None, fp=None)


@pytest.fixture()
def col(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.backends.remote import RemoteCollection
    return RemoteCollection(url="http://palace.test", token="tok")


def test_upsert_posts_add_drawer(col):
    resp = _make_response({"result": {"success": True, "drawer_id": "x"}})
    with patch("urllib.request.urlopen", return_value=resp) as mock_open:
        col.upsert(
            documents=["hello world"],
            ids=["drawer_foo_bar_abc123"],
            metadatas=[{"wing": "foo", "room": "bar", "source_file": "/f.jsonl", "added_by": "mine"}],
        )
    mock_open.assert_called_once()
    payload = json.loads(mock_open.call_args[0][0].data)
    assert payload["params"]["name"] == "mempalace_add_drawer"
    assert payload["params"]["arguments"]["wing"] == "foo"
    assert payload["params"]["arguments"]["content"] == "hello world"


def test_upsert_registry_skips_http_marks_done(col):
    with patch("urllib.request.urlopen") as mock_open:
        col.upsert(
            documents=["[registry] /f.jsonl"],
            ids=["_reg_abc"],
            metadatas=[{"wing": "w", "room": "_registry", "source_file": "/f.jsonl",
                        "added_by": "mine", "ingest_mode": "registry"}],
        )
    mock_open.assert_not_called()
    assert "/f.jsonl" in col._state["source_files"]


def test_already_exists_is_not_an_error(col):
    resp = _make_response({"result": {"success": True, "reason": "already_exists"}})
    with patch("urllib.request.urlopen", return_value=resp):
        col.upsert(
            documents=["hello"],
            ids=["d1"],
            metadatas=[{"wing": "w", "room": "r", "source_file": "/f.jsonl", "added_by": "mine"}],
        )
    # Should not raise


def test_get_returns_empty_when_file_not_in_state(col):
    result = col.get(where={"source_file": "/unknown.jsonl"})
    assert result["ids"] == []


def test_get_returns_hit_when_file_in_state(col):
    col.mark_file_done("/known.jsonl")
    result = col.get(where={"source_file": "/known.jsonl"})
    assert result["ids"] == ["/known.jsonl"]


def test_mark_file_done_persists_across_instances(col, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    col.mark_file_done("/f.jsonl")

    from mempalace.backends.remote import RemoteCollection
    col2 = RemoteCollection(url="http://palace.test", token="tok")
    assert "/f.jsonl" in col2._state["source_files"]


def test_mark_file_done_idempotent(col):
    col.mark_file_done("/f.jsonl")
    col.mark_file_done("/f.jsonl")
    assert col._state["source_files"].count("/f.jsonl") == 1


def test_401_raises_with_clear_message(col):
    with patch("urllib.request.urlopen", side_effect=_http_error(401)):
        with pytest.raises(RuntimeError, match="401"):
            col.upsert(
                documents=["x"],
                ids=["d1"],
                metadatas=[{"wing": "w", "room": "r", "source_file": "/f", "added_by": "mine"}],
            )


def test_connection_error_retries_once_then_raises(col):
    err = urllib.error.URLError("connection refused")
    with patch("urllib.request.urlopen", side_effect=err):
        with patch("time.sleep") as mock_sleep:
            with pytest.raises(urllib.error.URLError):
                col.upsert(
                    documents=["x"],
                    ids=["d1"],
                    metadatas=[{"wing": "w", "room": "r", "source_file": "/f", "added_by": "mine"}],
                )
        mock_sleep.assert_called_once_with(2)


def test_delete_is_noop(col):
    # Should not raise, should not call HTTP
    with patch("urllib.request.urlopen") as mock_open:
        col.delete(where={"source_file": "/f"})
    mock_open.assert_not_called()


def test_query_raises(col):
    with pytest.raises(NotImplementedError):
        col.query(query_texts=["x"], n_results=5)


def test_count_raises(col):
    with pytest.raises(NotImplementedError):
        col.count()
```

- [ ] **Step 2: Run tests — confirm they all fail**

```bash
cd /home/gavin/my_other_repos/mempalace
python -m pytest tests/test_remote_collection.py -v 2>&1 | head -30
```

Expected: `ModuleNotFoundError: No module named 'mempalace.backends.remote'`

- [ ] **Step 3: Implement `RemoteCollection`**

Create `mempalace/backends/remote.py`:

```python
"""RemoteCollection — sends drawers to a remote mempalace HTTP server."""

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import BaseCollection

logger = logging.getLogger(__name__)

_STATE_DIR = Path.home() / ".mempalace" / "remote_state"


class RemoteCollection(BaseCollection):
    """BaseCollection implementation that POSTs drawers to a remote HTTP MCP server."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url.rstrip("/")
        self._token = token
        state_key = hashlib.sha256(url.encode()).hexdigest()[:16]
        self._state_path = _STATE_DIR / f"{state_key}.json"
        self._state = self._load_state()

    # ── State file ──────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data.get("source_files"), list):
                return data
        except Exception:
            pass
        return {"url": self._url, "source_files": []}

    def _save_state(self) -> None:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._state_path)

    def mark_file_done(self, source_file: str) -> None:
        """Record source_file as fully sent. Idempotent."""
        if source_file not in self._state["source_files"]:
            self._state["source_files"].append(source_file)
            self._save_state()

    # ── HTTP ─────────────────────────────────────────────────────────────

    def _post(self, tool_name: str, arguments: Dict[str, Any]) -> dict:
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "id": 1,
                "params": {"name": tool_name, "arguments": arguments},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self._url}/mcp",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        for attempt in range(2):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise RuntimeError(
                        "Remote palace returned 401 — check --remote-token or MEMPALACE_TOKEN"
                    ) from e
                raise
            except (urllib.error.URLError, OSError):
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise
        raise RuntimeError("unreachable")  # pragma: no cover

    # ── BaseCollection interface ─────────────────────────────────────────

    def upsert(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if metadatas is None:
            metadatas = [{}] * len(documents)
        for doc, _id, meta in zip(documents, ids, metadatas):
            if meta.get("ingest_mode") == "registry":
                source_file = meta.get("source_file", "")
                if source_file:
                    self.mark_file_done(source_file)
                continue
            result = self._post(
                "mempalace_add_drawer",
                {
                    "wing": meta.get("wing", "general"),
                    "room": meta.get("room", "general"),
                    "content": doc,
                    "source_file": meta.get("source_file", ""),
                    "added_by": meta.get("added_by", "mine"),
                },
            )
            inner = result.get("result", result)
            if inner.get("reason") == "already_exists":
                logger.debug("drawer already_exists: %s", _id)

    def add(
        self,
        *,
        documents: List[str],
        ids: List[str],
        metadatas: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self.upsert(documents=documents, ids=ids, metadatas=metadatas)

    def get(self, *, where: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Dict[str, Any]:
        source_file = (where or {}).get("source_file")
        if source_file and source_file in self._state["source_files"]:
            return {"ids": [source_file], "documents": [], "metadatas": []}
        return {"ids": [], "documents": [], "metadatas": []}

    def delete(self, **kwargs: Any) -> None:
        pass

    def query(self, **kwargs: Any) -> Dict[str, Any]:
        raise NotImplementedError("RemoteCollection does not support query()")

    def count(self) -> int:
        raise NotImplementedError("RemoteCollection does not support count()")
```

- [ ] **Step 4: Run tests — confirm they pass**

```bash
python -m pytest tests/test_remote_collection.py -v
```

Expected: all 11 tests pass.

- [ ] **Step 5: Commit**

```bash
git add mempalace/backends/remote.py tests/test_remote_collection.py
git commit -m "feat(backends): add RemoteCollection for HTTP mine target"
```

---

### Task 2: Export `RemoteCollection` and add palace factory

**Files:**
- Modify: `mempalace/backends/__init__.py`
- Modify: `mempalace/palace.py`
- Test: `tests/test_remote_collection.py` (extend)

- [ ] **Step 1: Write failing test for the factory**

Append to `tests/test_remote_collection.py`:

```python
def test_get_remote_collection_returns_remote_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.palace import get_remote_collection
    from mempalace.backends.remote import RemoteCollection
    col = get_remote_collection("http://palace.test", "tok")
    assert isinstance(col, RemoteCollection)


def test_get_remote_collection_strips_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.palace import get_remote_collection
    col = get_remote_collection("http://palace.test/", "tok")
    assert col._url == "http://palace.test"
```

- [ ] **Step 2: Run — confirm failure**

```bash
python -m pytest tests/test_remote_collection.py::test_get_remote_collection_returns_remote_instance -v
```

Expected: `ImportError: cannot import name 'get_remote_collection'`

- [ ] **Step 3: Update `backends/__init__.py`**

```python
"""Storage backend implementations for MemPalace."""

from .base import BaseCollection
from .chroma import ChromaBackend, ChromaCollection
from .remote import RemoteCollection

__all__ = ["BaseCollection", "ChromaBackend", "ChromaCollection", "RemoteCollection"]
```

- [ ] **Step 4: Add factory to `palace.py`**

Add after the existing `get_collection` function (after line 50):

```python
def get_remote_collection(url: str, token: str) -> "RemoteCollection":
    """Get a collection that writes to a remote HTTP mempalace server."""
    from .backends.remote import RemoteCollection
    return RemoteCollection(url=url, token=token)
```

- [ ] **Step 5: Run — confirm tests pass**

```bash
python -m pytest tests/test_remote_collection.py -v
```

Expected: all 13 tests pass.

- [ ] **Step 6: Commit**

```bash
git add mempalace/backends/__init__.py mempalace/palace.py tests/test_remote_collection.py
git commit -m "feat(palace): add get_remote_collection factory"
```

---

### Task 3: Wire `convo_miner.py`

**Files:**
- Modify: `mempalace/convo_miner.py`
- Test: `tests/test_convo_miner_unit.py` (extend)

- [ ] **Step 1: Read the existing convo_miner unit tests to understand patterns**

```bash
head -60 tests/test_convo_miner_unit.py
```

- [ ] **Step 2: Write failing tests**

Append to `tests/test_convo_miner_unit.py`:

```python
def test_mine_convos_uses_remote_collection_when_remote_url_set(tmp_path, monkeypatch):
    """When remote_url is passed, mine_convos uses RemoteCollection, not get_collection."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.convo_miner import mine_convos
    from mempalace.backends.remote import RemoteCollection

    collected = []

    class CapturingRemote(RemoteCollection):
        def upsert(self, *, documents, ids, metadatas=None):
            collected.extend(documents)
        def get(self, *, where=None, **kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text(
        '{"type":"message","message":{"role":"user","content":"hello world"}}\n'
        '{"type":"message","message":{"role":"assistant","content":"hi there"}}\n'
    )

    with patch("mempalace.convo_miner.get_remote_collection", return_value=CapturingRemote("http://x", "t")):
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
            remote_token="t",
        )

    assert len(collected) > 0


def test_mine_convos_remote_token_from_env(tmp_path, monkeypatch):
    """MEMPALACE_TOKEN env var is used when remote_token is not passed."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("MEMPALACE_TOKEN", "env-token")
    from mempalace.convo_miner import mine_convos
    from mempalace.backends.remote import RemoteCollection

    captured_token = []

    with patch("mempalace.convo_miner.get_remote_collection") as mock_factory:
        mock_col = MagicMock(spec=RemoteCollection)
        mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}
        mock_factory.return_value = mock_col
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
    mock_factory.assert_called_once_with("http://x", "env-token")


def test_mine_convos_raises_when_remote_url_but_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_TOKEN", raising=False)
    from mempalace.convo_miner import mine_convos

    with pytest.raises(RuntimeError, match="remote-url requires a token"):
        mine_convos(
            convo_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
```

- [ ] **Step 3: Run — confirm failure**

```bash
python -m pytest tests/test_convo_miner_unit.py -k "remote" -v
```

Expected: `TypeError: mine_convos() got an unexpected keyword argument 'remote_url'`

- [ ] **Step 4: Modify `convo_miner.py`**

Add import at top of file (after existing imports):

```python
import os
```

*(already present — skip if so)*

Change the `mine_convos` signature and collection setup. Find the current signature:

```python
def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
):
```

Replace with:

```python
def mine_convos(
    convo_dir: str,
    palace_path: str,
    wing: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    extract_mode: str = "exchange",
    remote_url: str = None,
    remote_token: str = None,
):
```

Find the import line at the top of `convo_miner.py`:

```python
from .palace import SKIP_DIRS, get_collection, file_already_mined
```

Replace with:

```python
from .palace import SKIP_DIRS, get_collection, get_remote_collection, file_already_mined
```

Find the collection setup inside `mine_convos` (currently around line 310):

```python
    collection = get_collection(palace_path) if not dry_run else None
```

Replace with:

```python
    if not dry_run:
        if remote_url:
            token = remote_token or os.environ.get("MEMPALACE_TOKEN", "")
            if not token:
                raise RuntimeError(
                    "remote-url requires a token — pass --remote-token or set MEMPALACE_TOKEN"
                )
            collection = get_remote_collection(remote_url, token)
        else:
            collection = get_collection(palace_path)
    else:
        collection = None
```

Find where `palace_path` is printed (around line 305):

```python
    print(f"  Palace:  {palace_path}")
```

Replace with:

```python
    print(f"  Palace:  {remote_url or palace_path}")
```

After the chunk-filing loop for each file (find the line that prints the progress line):

```python
        print(f"  ✓ [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers_added}")
```

Add after it:

```python
        if hasattr(collection, "mark_file_done"):
            collection.mark_file_done(source_file)
```

- [ ] **Step 5: Run — confirm tests pass**

```bash
python -m pytest tests/test_convo_miner_unit.py -v
```

Expected: all tests pass including the 3 new remote ones.

- [ ] **Step 6: Commit**

```bash
git add mempalace/convo_miner.py tests/test_convo_miner_unit.py
git commit -m "feat(convo_miner): add remote_url/remote_token support"
```

---

### Task 4: Wire `miner.py`

**Files:**
- Modify: `mempalace/miner.py`
- Test: `tests/test_miner.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_miner.py`:

```python
def test_mine_uses_remote_collection_when_remote_url_set(tmp_path, monkeypatch):
    """When remote_url is passed, mine uses RemoteCollection."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace.miner import mine
    from unittest.mock import patch, MagicMock

    mock_col = MagicMock()
    mock_col.get.return_value = {"ids": [], "documents": [], "metadatas": []}

    (tmp_path / "hello.txt").write_text("this is some content for testing the miner remote path")

    with patch("mempalace.miner.get_remote_collection", return_value=mock_col):
        mine(
            project_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
            remote_token="tok",
        )

    assert mock_col.upsert.called or mock_col.get.called


def test_mine_raises_when_remote_url_but_no_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MEMPALACE_TOKEN", raising=False)
    from mempalace.miner import mine

    with pytest.raises(RuntimeError, match="remote-url requires a token"):
        mine(
            project_dir=str(tmp_path),
            palace_path=str(tmp_path / "palace"),
            remote_url="http://x",
        )
```

- [ ] **Step 2: Run — confirm failure**

```bash
python -m pytest tests/test_miner.py -k "remote" -v
```

Expected: `TypeError: mine() got an unexpected keyword argument 'remote_url'`

- [ ] **Step 3: Modify `miner.py`**

Find the `mine` function import at top:

```python
from .palace import SKIP_DIRS, get_collection, file_already_mined
```

Replace with:

```python
from .palace import SKIP_DIRS, get_collection, get_remote_collection, file_already_mined
```

Find the `mine` signature:

```python
def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
):
```

Replace with:

```python
def mine(
    project_dir: str,
    palace_path: str,
    wing_override: str = None,
    agent: str = "mempalace",
    limit: int = 0,
    dry_run: bool = False,
    respect_gitignore: bool = True,
    include_ignored: list = None,
    remote_url: str = None,
    remote_token: str = None,
):
```

Find the `Palace:` print line and collection setup:

```python
    print(f"  Palace:  {palace_path}")
    ...
    if not dry_run:
        collection = get_collection(palace_path)
    else:
        collection = None
```

Replace with:

```python
    print(f"  Palace:  {remote_url or palace_path}")
    ...
    if not dry_run:
        if remote_url:
            token = remote_token or os.environ.get("MEMPALACE_TOKEN", "")
            if not token:
                raise RuntimeError(
                    "remote-url requires a token — pass --remote-token or set MEMPALACE_TOKEN"
                )
            collection = get_remote_collection(remote_url, token)
        else:
            collection = get_collection(palace_path)
    else:
        collection = None
```

Ensure `import os` is present at the top of `miner.py` (check — add if missing).

Find the per-file print line in the `mine` loop:

```python
            if not dry_run:
                print(f"  ✓ [{i:4}/{len(files)}] {filepath.name[:50]:50} +{drawers}")
```

Add after it:

```python
            if not dry_run and hasattr(collection, "mark_file_done"):
                collection.mark_file_done(str(filepath))
```

- [ ] **Step 4: Run — confirm tests pass**

```bash
python -m pytest tests/test_miner.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add mempalace/miner.py tests/test_miner.py
git commit -m "feat(miner): add remote_url/remote_token support"
```

---

### Task 5: CLI flags

**Files:**
- Modify: `mempalace/cli.py`
- Test: `tests/test_cli.py` (extend)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_cli.py`:

```python
def test_mine_remote_url_flag_passed_to_mine_convos(tmp_path, monkeypatch):
    """--remote-url and --remote-token are forwarded to mine_convos."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace import cli
    from unittest.mock import patch

    with patch("mempalace.cli.mine_convos") as mock_mine:
        cli.main([
            "mine", str(tmp_path),
            "--mode", "convos",
            "--remote-url", "http://palace.test",
            "--remote-token", "secret",
        ])

    mock_mine.assert_called_once()
    kwargs = mock_mine.call_args[1] if mock_mine.call_args[1] else {}
    args = mock_mine.call_args[0] if mock_mine.call_args[0] else ()
    # Accept either positional or keyword
    call_args = mock_mine.call_args
    assert call_args is not None
    # remote_url should be "http://palace.test"
    all_args = {**dict(zip(
        ["convo_dir", "palace_path", "wing", "agent", "limit", "dry_run", "extract_mode",
         "remote_url", "remote_token"],
        call_args.args
    )), **(call_args.kwargs or {})}
    assert all_args.get("remote_url") == "http://palace.test"
    assert all_args.get("remote_token") == "secret"


def test_mine_remote_url_with_palace_prints_warning(tmp_path, monkeypatch, capsys):
    """Using --remote-url with --palace prints a warning."""
    monkeypatch.setenv("HOME", str(tmp_path))
    from mempalace import cli
    from unittest.mock import patch

    with patch("mempalace.cli.mine_convos"):
        cli.main([
            "mine", str(tmp_path),
            "--mode", "convos",
            "--palace", str(tmp_path / "palace"),
            "--remote-url", "http://palace.test",
            "--remote-token", "tok",
        ])

    out = capsys.readouterr().out + capsys.readouterr().err
    # Warning should appear somewhere in output
    # (checked via stderr in implementation)
```

- [ ] **Step 2: Run — confirm failure**

```bash
python -m pytest tests/test_cli.py -k "remote_url" -v
```

Expected: `error: unrecognized arguments: --remote-url`

- [ ] **Step 3: Add CLI flags to `cli.py`**

In `cli.py`, find the end of the `mine` subparser arguments (just before `# search`):

```python
    p_mine.add_argument(
        "--dry-run", action="store_true", help="Show what would be filed without filing"
    )
    p_mine.add_argument(
        "--extract",
        ...
    )

    # search
```

Add after the `--extract` argument:

```python
    p_mine.add_argument(
        "--remote-url",
        default=None,
        metavar="URL",
        help="Send drawers to a remote mempalace HTTP server instead of local palace",
    )
    p_mine.add_argument(
        "--remote-token",
        default=None,
        metavar="TOKEN",
        help="Bearer token for --remote-url (default: MEMPALACE_TOKEN env var)",
    )
```

In `cmd_mine`, find the `if args.mode == "convos":` block:

```python
    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
        )
    else:
        from .miner import mine

        mine(
            project_dir=args.dir,
            palace_path=palace_path,
            wing_override=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            respect_gitignore=not args.no_gitignore,
            include_ignored=include_ignored,
        )
```

Replace with:

```python
    remote_url = getattr(args, "remote_url", None)
    remote_token = getattr(args, "remote_token", None)

    if remote_url and args.palace:
        import sys
        print(
            "Warning: --palace is ignored when --remote-url is set",
            file=sys.stderr,
        )

    if args.mode == "convos":
        from .convo_miner import mine_convos

        mine_convos(
            convo_dir=args.dir,
            palace_path=palace_path,
            wing=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            extract_mode=args.extract,
            remote_url=remote_url,
            remote_token=remote_token,
        )
    else:
        from .miner import mine

        mine(
            project_dir=args.dir,
            palace_path=palace_path,
            wing_override=args.wing,
            agent=args.agent,
            limit=args.limit,
            dry_run=args.dry_run,
            respect_gitignore=not args.no_gitignore,
            include_ignored=include_ignored,
            remote_url=remote_url,
            remote_token=remote_token,
        )
```

- [ ] **Step 4: Run — confirm tests pass**

```bash
python -m pytest tests/test_cli.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add mempalace/cli.py tests/test_cli.py
git commit -m "feat(cli): add --remote-url and --remote-token to mine"
```

---

### Task 6: Full test suite + verify

**Files:** None — validation only.

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v --ignore=tests/benchmarks -x 2>&1 | tail -30
```

Expected: all tests pass. No regressions.

- [ ] **Step 2: Smoke-test the CLI help**

```bash
python -m mempalace.cli mine --help 2>&1 | grep -E "remote-url|remote-token"
```

Expected:
```
  --remote-url URL      Send drawers to a remote mempalace HTTP server instead of local palace
  --remote-token TOKEN  Bearer token for --remote-url (default: MEMPALACE_TOKEN env var)
```

- [ ] **Step 3: Smoke-test against the live k8s instance (manual)**

```bash
source ~/.secrets
export MEMPALACE_TOKEN="$MEMPALACE_TOKEN_VALUE"  # from k8s secret

# Dry-run first
python -m mempalace.cli mine ~/.claude/projects \
  --mode convos \
  --remote-url https://mempalace.${SECRET_DOMAIN} \
  --remote-token "$MEMPALACE_TOKEN" \
  --limit 5 \
  --dry-run

# Then real run on a small batch
python -m mempalace.cli mine ~/.claude/projects \
  --mode convos \
  --remote-url https://mempalace.${SECRET_DOMAIN} \
  --remote-token "$MEMPALACE_TOKEN" \
  --limit 10
```

Verify drawers appeared on the server:

```bash
mempalace hook run --hook session-start --harness claude-code  # check status output
```

Or via MCP: call `mempalace_status` and confirm drawer count increased.

- [ ] **Step 4: Final commit**

```bash
git add .claude/.verified
git commit -m "test: verify mine --remote-url full suite passes"
```
