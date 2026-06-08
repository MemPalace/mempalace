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
