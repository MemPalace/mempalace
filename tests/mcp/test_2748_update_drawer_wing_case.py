"""Regression #2748: update_drawer must apply a case-variant wing rename.

Before the fix, moving a drawer from ``wing_Codex`` to ``wing_codex``
(returning ``success: true``) silently left the stored wing at ``wing_Codex``
because the target is canonicalised (lower-cased) and then compared against a
normalised copy of the stored wing, so a case-only difference reads as "equal"
and the rename is dropped. ``last_modified`` still bumps, so the call looks
like it landed.

Expected: the stored wing becomes exactly the caller's target (case-preserved),
so ``success: true`` matches the stored state.
"""

from tests._mcp_server_helpers import _get_collection, _patch_mcp_server


def _stored_wing(col, drawer_id):
    rows = col.get(ids=[drawer_id], include=["metadatas"])
    assert rows["metadatas"], f"drawer {drawer_id} missing"
    return rows["metadatas"][0].get("wing")


class TestIssue2748UpdateDrawerWingCase:
    def test_case_variant_rename_is_applied(self, monkeypatch, config, palace_path, kg):
        """Moving to a case-variant target wing must actually change the wing."""
        _patch_mcp_server(monkeypatch, config, kg)
        client, col = _get_collection(palace_path, create=True)
        from mempalace.mcp_server import tool_add_drawer, tool_update_drawer

        # File a drawer whose stored wing preserves mixed case (as a legacy
        # palace would hold it). ``add_drawer`` canonicalises, so insert the
        # case-variant source row directly to model the reporter's palace.
        src_wing = "wing_Codex"
        add = tool_add_drawer(wing="wing_codex", room="diary", content="source drawer body.")
        assert add["success"]
        src_id = add["drawer_id"]
        # Rewrite the stored wing to the legacy mixed-case form.
        rows = col.get(ids=[src_id], include=["metadatas"])
        meta = dict(rows["metadatas"][0])
        meta["wing"] = src_wing
        col.update(ids=[src_id], metadatas=[meta])
        assert _stored_wing(col, src_id) == "wing_Codex", "precondition: source wing"

        # Now the reporter's exact call: move it to the lowercase variant.
        result = tool_update_drawer(drawer_id=src_id, wing="wing_codex", room="diary")
        assert result["success"] is True, result

        # THE BUG: before the fix this is still "wing_Codex" (a silent no-op).
        assert _stored_wing(col, src_id) == "wing_codex", (
            f"case-only rename dropped: stored={_stored_wing(col, src_id)!r}, "
            f"response_wing={result.get('wing')!r}"
        )
        # The response must echo the actually-stored wing.
        assert result.get("wing") == "wing_codex", (
            f"response wing {result.get('wing')!r} != stored wing 'wing_codex'"
        )

    def test_exact_case_target_is_a_true_noop(self, monkeypatch, config, palace_path, kg):
        """A target identical to the stored wing (exact case) stays a no-op."""
        _patch_mcp_server(monkeypatch, config, kg)
        client, col = _get_collection(palace_path, create=True)
        from mempalace.mcp_server import tool_add_drawer, tool_update_drawer

        add = tool_add_drawer(wing="wing_codex", room="diary", content="stable body text.")
        assert add["success"]
        before = _stored_wing(col, add["drawer_id"])

        result = tool_update_drawer(drawer_id=add["drawer_id"], wing=before)
        assert result["success"] is True
        assert _stored_wing(col, add["drawer_id"]) == before
