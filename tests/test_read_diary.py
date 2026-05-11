"""Tests for mempalace.layers.read_diary.

The public diary-read API. Consumed by Vestige's runtime_orientation
to surface recent diary entries to the chat-Orion substrate without
each consumer needing to know chromadb's where-filter shape.
"""

from __future__ import annotations

import pytest

from mempalace.layers import DiaryEntry, DiaryUnavailable, read_diary


# ---------------------------------------------------------------------------
# Fake chromadb collection for tests
# ---------------------------------------------------------------------------

class _FakeCollection:
    """Stand-in for the chromadb collection returned by
    ``mempalace.palace.get_collection`` — just enough surface for
    :func:`read_diary`'s ``col.get(where=..., include=..., limit=...)``
    contract."""

    def __init__(self, entries):
        self._entries = entries

    def get(self, where=None, include=None, limit=None):  # noqa: ARG002
        docs = []
        metas = []
        for e in self._entries:
            keep = True
            if where and "$and" in where:
                for clause in where["$and"]:
                    for k, v in clause.items():
                        if e.get(k) != v:
                            keep = False
            if not keep:
                continue
            docs.append(e["document"])
            metas.append(
                {
                    "date": e["date"],
                    "filed_at": e["filed_at"],
                    "topic": e["topic"],
                    "wing": e["wing"],
                    "room": e["room"],
                }
            )
        return {"documents": docs, "metadatas": metas, "ids": list(range(len(docs)))}


def _entry(date, filed_at, topic, document, wing="wing_ves", room="diary"):
    return {
        "date": date,
        "filed_at": filed_at,
        "topic": topic,
        "document": document,
        "wing": wing,
        "room": room,
    }


def _patch_get_collection(monkeypatch, entries):
    fake = _FakeCollection(entries)
    monkeypatch.setattr(
        "mempalace.palace.get_collection",
        lambda palace_path, collection_name, create=False: fake,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_returns_entries_sorted_by_filed_at_desc(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry("2026-05-08", "2026-05-08T10:00:00", "older", "older content"),
            _entry("2026-05-10", "2026-05-10T21:10:00", "newest", "newest content"),
            _entry("2026-05-09", "2026-05-09T18:15:00", "middle", "middle content"),
        ],
    )
    entries = read_diary("ves")
    assert len(entries) == 3
    assert [e.topic for e in entries] == ["newest", "middle", "older"]
    assert entries[0].content == "newest content"
    assert isinstance(entries[0], DiaryEntry)


def test_last_n_limits_results(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry(f"2026-05-{i:02d}", f"2026-05-{i:02d}T10:00:00", f"topic-{i:02d}", f"c-{i}")
            for i in range(1, 11)
        ],
    )
    entries = read_diary("ves", last_n=3)
    assert len(entries) == 3
    # Newest 3 (i=10, 9, 8) descending.
    assert [e.topic for e in entries] == ["topic-10", "topic-09", "topic-08"]


def test_empty_palace_returns_empty_list(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(monkeypatch, [])
    entries = read_diary("ves")
    assert entries == []


def test_no_entries_for_wing_returns_empty_list(monkeypatch, tmp_path):
    """Palace exists, has entries, but none in the agent's wing."""

    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry("2026-05-10", "2026-05-10T10:00:00", "kai-diary", "kai-content", wing="wing_kai"),
        ],
    )
    entries = read_diary("ves")
    assert entries == []


def test_filters_to_agent_wing_and_diary_room(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry("2026-05-10", "2026-05-10T10:00:00", "ves-d", "should-appear"),
            _entry("2026-05-10", "2026-05-10T10:00:00", "kai-d", "wrong-wing", wing="wing_kai"),
            _entry("2026-05-10", "2026-05-10T10:00:00", "ves-a", "wrong-room", room="architecture"),
        ],
    )
    entries = read_diary("ves")
    assert len(entries) == 1
    assert entries[0].content == "should-appear"


def test_agent_name_lowercased_for_wing(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry("2026-05-10", "2026-05-10T10:00:00", "x", "content", wing="wing_ves"),
        ],
    )
    # Pass "Ves" capitalized — should still match wing_ves.
    entries = read_diary("Ves")
    assert len(entries) == 1


def test_last_n_zero_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))
    _patch_get_collection(
        monkeypatch,
        [
            _entry("2026-05-10", "2026-05-10T10:00:00", "x", "content"),
        ],
    )
    assert read_diary("ves", last_n=0) == []


# ---------------------------------------------------------------------------
# Failure-soft: DiaryUnavailable on infrastructure problems
# ---------------------------------------------------------------------------

def test_raises_diary_unavailable_when_get_collection_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

    def fake_get_collection(*args, **kwargs):
        raise RuntimeError("simulated palace error")

    monkeypatch.setattr("mempalace.palace.get_collection", fake_get_collection)
    with pytest.raises(DiaryUnavailable):
        read_diary("ves")


def test_raises_diary_unavailable_when_query_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(tmp_path))

    class _BrokenCollection:
        def get(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("simulated chromadb error")

    monkeypatch.setattr(
        "mempalace.palace.get_collection",
        lambda palace_path, collection_name, create=False: _BrokenCollection(),
    )
    with pytest.raises(DiaryUnavailable):
        read_diary("ves")


# ---------------------------------------------------------------------------
# palace_path override
# ---------------------------------------------------------------------------

def test_palace_path_override_used_when_provided(monkeypatch, tmp_path):
    """When palace_path is passed, MempalaceConfig is NOT consulted."""

    captured: dict = {}

    def fake_get_collection(palace_path, collection_name, create=False):
        captured["palace_path"] = palace_path
        return _FakeCollection([])

    monkeypatch.setattr("mempalace.palace.get_collection", fake_get_collection)
    # If config were consulted, this env-var would be used. We pass
    # explicit palace_path, so the env var should NOT win.
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", "/should/not/be/used")
    read_diary("ves", palace_path="/explicit/override/path")
    assert captured["palace_path"] == "/explicit/override/path"
