"""Tests for the wing-name normalization migration (migrate-wings).

normalize_wing_name strips leading/trailing separators (#1675); palaces built
before that rule filed drawers under the old name (e.g. ``_alpha``).
``migrate_wing_names`` re-keys the ``wing`` metadata in place so those memories
stay discoverable under the new name, merging collisions. IDs are left untouched
(they are opaque keys), and the pass is idempotent.
"""

from mempalace.migrate import migrate_wing_names, plan_wing_renames


# --- pure planner ---------------------------------------------------------


def test_plan_renames_strips_leading_and_trailing():
    summary, updates = plan_wing_renames(
        [
            ("d1", {"wing": "_alpha", "room": "r"}),
            ("d2", {"wing": "beta_", "room": "r"}),
            ("d3", {"wing": "clean", "room": "r"}),
        ]
    )
    assert dict(summary) == {("_alpha", "alpha"): 1, ("beta_", "beta"): 1}
    assert {u[0] for u in updates} == {"d1", "d2"}
    # only 'wing' is rewritten; other metadata is preserved
    by_id = {u[0]: u[1] for u in updates}
    assert by_id["d1"]["wing"] == "alpha"
    assert by_id["d1"]["room"] == "r"
    assert by_id["d2"]["wing"] == "beta"


def test_plan_renames_noop_for_clean_wings():
    summary, updates = plan_wing_renames([("d", {"wing": "already_clean", "room": "r"})])
    assert not summary
    assert not updates


def test_plan_renames_ignores_empty_nonstring_and_all_separator():
    _, updates = plan_wing_renames(
        [
            ("a", {"wing": "_"}),  # normalizes to "" -> skip (never strand a drawer)
            ("b", {"wing": ""}),
            ("c", {"wing": None}),
            ("d", {}),
        ]
    )
    assert updates == []


def test_plan_renames_collision_maps_both_to_same_target():
    _, updates = plan_wing_renames(
        [
            ("d1", {"wing": "_gamma"}),
            ("d2", {"wing": "gamma_"}),
        ]
    )
    assert {u[1]["wing"] for u in updates} == {"gamma"}


# --- integration over a real backend collection ---------------------------


def _seed(palace, rows):
    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=True)
    # Explicit embeddings keep the test hermetic (no embedding model needed) —
    # the migration only reads/writes metadata.
    col.upsert(
        ids=[r["id"] for r in rows],
        documents=[r["doc"] for r in rows],
        metadatas=[r["meta"] for r in rows],
        embeddings=[[float(i + 1)] * 8 for i in range(len(rows))],
    )
    return col


def _wing_ids(palace, wing):
    from mempalace.palace import get_collection

    col = get_collection(str(palace), create=False)
    res = col.get(where={"wing": wing}, include=["metadatas"])
    return set(res.ids if hasattr(res, "ids") else res["ids"])


def _meta(wing, room, source_file, idx):
    return {"wing": wing, "room": room, "source_file": source_file, "chunk_index": idx}


def test_migrate_relabels_old_format_wings(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed(
        palace,
        [
            {"id": "drawer__alpha_r_1", "doc": "auth jwt", "meta": _meta("_alpha", "r", "a.py", 0)},
            {"id": "drawer_beta__r_2", "doc": "db alembic", "meta": _meta("beta_", "r", "b.py", 0)},
            {
                "id": "drawer_clean_r_3",
                "doc": "react query",
                "meta": _meta("clean", "r", "c.py", 0),
            },
        ],
    )

    assert migrate_wing_names(str(palace), confirm=True) is True

    assert _wing_ids(palace, "alpha") == {"drawer__alpha_r_1"}
    assert _wing_ids(palace, "beta") == {"drawer_beta__r_2"}
    assert _wing_ids(palace, "_alpha") == set()
    assert _wing_ids(palace, "beta_") == set()
    # an already-clean wing is left untouched
    assert _wing_ids(palace, "clean") == {"drawer_clean_r_3"}


def test_migrate_merges_collision_into_existing_wing(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed(
        palace,
        [
            {"id": "drawer_gamma_r_1", "doc": "current", "meta": _meta("gamma", "r", "g1.py", 0)},
            {"id": "drawer__gamma_r_2", "doc": "legacy", "meta": _meta("_gamma", "r", "g2.py", 0)},
        ],
    )

    migrate_wing_names(str(palace), confirm=True)

    assert _wing_ids(palace, "gamma") == {"drawer_gamma_r_1", "drawer__gamma_r_2"}
    assert _wing_ids(palace, "_gamma") == set()


def test_migrate_dry_run_changes_nothing(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed(palace, [{"id": "drawer__x_r_1", "doc": "d", "meta": _meta("_x", "r", "x.py", 0)}])

    assert migrate_wing_names(str(palace), dry_run=True) is True
    # nothing actually moved
    assert _wing_ids(palace, "_x") == {"drawer__x_r_1"}
    assert _wing_ids(palace, "x") == set()


def test_migrate_is_idempotent(tmp_path):
    palace = tmp_path / "palace"
    palace.mkdir()
    _seed(palace, [{"id": "drawer__y_r_1", "doc": "d", "meta": _meta("_y", "r", "y.py", 0)}])

    assert migrate_wing_names(str(palace), confirm=True) is True
    # second run finds nothing left to normalize
    assert migrate_wing_names(str(palace), confirm=True) is False
    assert _wing_ids(palace, "y") == {"drawer__y_r_1"}
