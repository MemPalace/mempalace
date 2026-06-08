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
