"""Drawer batching keeps singular read permissions and error semantics."""

from types import SimpleNamespace

import pytest

from commonplace import memory_tools
from commonplace.extensions import ToolError
from commonplace.gateway import _DROP, _filter_drawer_batch, _scrub
from commonplace.policy import Policy


def _policy(profile="standard", **kwargs):
    return Policy.build(
        profile=profile, scopes=["memory:read"], read_wings=["shared"], trust="standard", **kwargs
    )


class _Context:
    def __init__(self, policy, *, item=None, batch_error=None, short=False):
        self.principal = SimpleNamespace(policy=policy)
        self.calls = []
        self.item = item or {
            "drawer_id": "a",
            "content": "exact words\nsecond line",
            "wing": "shared",
            "room": "synthetic",
        }
        self.batch_error = batch_error
        self.short = short

    def memory(self, name, args):
        self.calls.append(name)
        if not self.principal.policy.tool_visible(name):
            raise ToolError(f"Unknown tool: {name}")
        if name == "mempalace_search":
            return {"results": [{"drawer_id": "a"}]}
        if name == "mempalace_get_drawer":
            item = _scrub(self.item, self.principal.policy.can_read_wing)
            if item is _DROP:
                raise ToolError("Drawer not found: a")
            if item.get("error"):
                raise ToolError(item["error"])
            return item
        if name == "mempalace_get_drawers":
            if self.batch_error:
                raise ToolError(self.batch_error)
            return _filter_drawer_batch(
                {"results": [] if self.short else [self.item]}, self.principal.policy
            )
        raise AssertionError(name)


def _outcome(call):
    try:
        return call()
    except ToolError as exc:
        return str(exc)


@pytest.mark.parametrize(
    "error,expected",
    [
        ("database is locked", "no: memory request refused or unavailable"),
        ("Drawer not found: a", "no: memory not found"),
        ("No palace found", "no: memory not found"),
        ("Chroma database missing", "no: memory not found"),
    ],
)
def test_scoped_item_errors_match_singular_reads(error, expected):
    ctx = _Context(_policy(), item={"drawer_id": "a", "error": error})
    singular = _outcome(lambda: memory_tools._get(ctx, "a"))
    searched = _outcome(lambda: memory_tools._search(ctx, {"query": "synthetic"}))
    assert singular == searched == expected


def test_scoped_batch_preserves_visible_storage_errors():
    result = _filter_drawer_batch(
        {"results": [{"drawer_id": "a", "error": "database is locked"}]}, _policy()
    )
    assert result == {
        "results": [{"drawer_id": "a", "error": "database is locked"}],
        "count": 1,
        "errors": 1,
    }


@pytest.mark.parametrize("error", ["No palace found", "Chroma database missing"])
def test_disappearing_palace_matches_singular_read(error):
    ctx = _Context(_policy(), item={"drawer_id": "a", "error": error}, batch_error=error)
    assert (
        _outcome(lambda: memory_tools._search(ctx, {"query": "synthetic"}))
        == "no: memory not found"
    )


def test_explicit_tools_without_singular_read_still_refuse_search():
    ctx = _Context(_policy(tools=("mempalace_search", "mempalace_get_drawers")))
    assert (
        _outcome(lambda: memory_tools._search(ctx, {"query": "synthetic"}))
        == "no: memory request refused or unavailable"
    )
    assert ctx.calls == ["mempalace_search", "mempalace_get_drawer"]


def test_real_slim_policy_keeps_individual_reads():
    ctx = _Context(_policy(profile="slim"))
    found = memory_tools._search(ctx, {"query": "synthetic"})
    assert found[0]["text"] == ctx.item["content"]
    assert ctx.calls == ["mempalace_search", "mempalace_get_drawer"]


def test_missing_batch_slot_is_not_found():
    ctx = _Context(_policy(), short=True)
    assert (
        _outcome(lambda: memory_tools._search(ctx, {"query": "synthetic"}))
        == "no: memory not found"
    )


@pytest.mark.parametrize(
    "item",
    [
        {"drawer_id": "a", "content": "secret", "wing": "private"},
        {"drawer_id": "a", "error": "database is locked", "metadata": {"wing": "private"}},
    ],
)
def test_hidden_items_still_look_missing(item):
    ctx = _Context(_policy(), item=item)
    assert (
        _outcome(lambda: memory_tools._search(ctx, {"query": "synthetic"}))
        == "no: memory not found"
    )
    assert _outcome(lambda: memory_tools._get(ctx, "a")) == "no: memory not found"
