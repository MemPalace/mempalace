"""Startup drawer batching preserves policy and verbatim records."""

from types import SimpleNamespace

import pytest


class _Fake:
    """Stand-in ctx: memory() raises ToolError on error results, as ToolContext.memory does."""

    def __init__(self, hits, drawers, *, visible=None, missing="raise"):
        self.calls = []
        self.read_ids = []
        self._hits = hits
        self._drawers = drawers
        self._missing = missing
        self.principal = SimpleNamespace(
            policy=SimpleNamespace(tool_visible=visible or (lambda name: True))
        )

    def memory(self, name, args):
        self.calls.append(name)
        if name == "mempalace_search":
            return {"results": [{"drawer_id": h} for h in self._hits]}
        if name == "mempalace_get_drawer":
            drawer_id = args["drawer_id"]
            self.read_ids.append(drawer_id)
            if drawer_id in self._drawers:
                return self._drawers[drawer_id]
            if self._missing == "raise":
                from commonplace.extensions import ToolError

                raise ToolError(f"Drawer not found: {drawer_id}")
            return {"error": f"Drawer not found: {drawer_id}"}
        if name == "mempalace_get_drawers":
            self.read_ids.extend(args["drawer_ids"])
            return {
                "results": [
                    self._drawers.get(i, {"error": f"Drawer not found: {i}"})
                    for i in args["drawer_ids"]
                ]
            }
        raise AssertionError(f"unexpected tool {name}")


def _drawer(drawer_id):
    return {
        "drawer_id": drawer_id,
        "content": f"verbatim {drawer_id} line\nsecond line",
        "wing": "private_threads",
        "room": "thr_probe",
        "metadata": {"filed_at": "2026-11-05T00:00:00Z"},
    }


def test_search_reads_hits_in_one_batch():
    from commonplace import memory_tools

    ctx = _Fake(["a", "b", "c"], {i: _drawer(i) for i in "abc"})
    memory_tools._search(ctx, {"query": "q", "limit": 3})
    assert ctx.calls == ["mempalace_search", "mempalace_get_drawers"]


def test_output_equals_per_id_records():
    from commonplace import memory_tools

    ctx = _Fake(["a", "chunked"], {i: _drawer(i) for i in ["a", "chunked"]})
    got = memory_tools._search(ctx, {"query": "q", "limit": 3})
    want = [memory_tools._get(ctx, i) for i in ["a", "chunked"]]
    assert got == want


def test_duplicate_hits_keep_first_seen_order():
    from commonplace import memory_tools

    ctx = _Fake(["a", "b", "a"], {i: _drawer(i) for i in "ab"})
    got = memory_tools._search(ctx, {"query": "q", "limit": 3})
    assert [m["id"] for m in got] == ["a", "b"]
    assert ctx.read_ids == ["a", "b"]


def test_missing_item_raises_no_memory_not_found():
    from commonplace import memory_tools
    from commonplace.extensions import ToolError

    ctx = _Fake(["a", "x"], {"a": _drawer("a")}, missing="raise")
    with pytest.raises(ToolError) as info:
        memory_tools._search(ctx, {"query": "q", "limit": 3})
    assert str(info.value) == "no: memory not found"


def test_slim_profile_keeps_per_id_reads():
    from commonplace import memory_tools

    ctx = _Fake(
        ["a", "b"],
        {i: _drawer(i) for i in "ab"},
        visible=lambda name: name != "mempalace_get_drawers",
    )
    got = memory_tools._search(ctx, {"query": "q", "limit": 3})
    assert [m["id"] for m in got] == ["a", "b"]
    assert ctx.calls == ["mempalace_search", "mempalace_get_drawer", "mempalace_get_drawer"]


def test_zero_hits_do_not_fetch_drawers():
    from commonplace import memory_tools

    ctx = _Fake([], {})
    assert memory_tools._search(ctx, {"query": "q"}) == []
    assert ctx.calls == ["mempalace_search"]
