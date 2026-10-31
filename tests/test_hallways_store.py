"""The SQLite hallway store: one-time import, per-wing writes, reduced reads.

Every mine used to load the whole ``hallways.json``, replace one wing's
records and write the whole file back. On a palace with ~830k hallways that
file was ~545 MB, and a mine of a wing holding a few dozen of them spent
~20 s decoding and re-encoding it while holding the GIL, so the HTTP hub
answered nothing else meanwhile. These tests pin what replaced that: a mine
reads and writes only its own wing's rows, an unchanged record is not
rewritten, and ``hallways.json`` is imported once, by a write, and kept.
"""

import json
import os
import sqlite3
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

with patch.dict("sys.modules", {"chromadb": MagicMock()}):
    from mempalace import hallways as hallways_mod
    from mempalace.palace_graph import entity_tunnel_candidates


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the store at ``tmp_path``; returns the (json, sqlite) paths."""
    json_path = tmp_path / "hallways.json"
    monkeypatch.setattr(hallways_mod, "_get_hallway_file", lambda *a, **k: str(json_path))
    monkeypatch.setattr(hallways_mod, "_legacy_hallway_file", lambda: str(tmp_path / "legacy.json"))
    return json_path, tmp_path / "hallways.sqlite3"


def _collection(drawers):
    col = MagicMock()
    col.count.return_value = len(drawers)

    def _get(limit=None, offset=0, include=None, where=None, **_kw):
        rows = [d for d in drawers if where is None or d.get("wing") == where.get("wing")]
        page = rows[offset : offset + limit] if limit is not None else rows
        return {"ids": [str(i) for i in range(len(page))], "metadatas": page}

    col.get.side_effect = _get
    return col


def _rows(sqlite_path, wing=None):
    """``{id: (rowid, record)}`` read straight from the table."""
    conn = sqlite3.connect(sqlite_path)
    try:
        sql = "SELECT id, rowid, record FROM hallways"
        rows = conn.execute(sql + " WHERE wing = ?", (wing,)) if wing else conn.execute(sql)
        return {hid: (rowid, json.loads(record)) for hid, rowid, record in rows}
    finally:
        conn.close()


def _kept(directory, label):
    return sorted(
        p.name for p in directory.iterdir() if p.name.startswith(f"hallways.json.{label}-")
    )


RECORDS = [
    {
        "id": "hallway_w_Aya_Lumi_1",
        "wing": "w",
        "entity_a": "Aya",
        "entity_b": "Lumi",
        "co_occurrence_count": 4,
        "rooms": ["diary"],
        "label": "Aya ↔ Lumi",
        "strength": 1.5,
    },
    {
        "id": "hallway_x_A_B_2",
        "wing": "x",
        "entity_a": "A",
        "entity_b": "B",
        "co_occurrence_count": 2,
    },
]


class TestImport:
    def test_a_read_before_any_write_reads_the_json_and_writes_nothing(self, store):
        json_path, sqlite_path = store
        json_path.write_text(json.dumps({"schema_version": 1, "hallways": RECORDS}), "utf-8")
        assert hallways_mod.list_hallways() == RECORDS
        assert hallways_mod.list_hallways("x") == [RECORDS[1]]
        assert json_path.exists()
        assert not sqlite_path.exists()

    def test_the_first_write_imports_the_json_and_keeps_it(self, store):
        json_path, sqlite_path = store
        original = json.dumps({"schema_version": 1, "hallways": RECORDS})
        json_path.write_text(original, "utf-8")

        assert hallways_mod.delete_hallway("no_such_id") is False

        assert hallways_mod.list_hallways() == RECORDS
        assert not json_path.exists()
        kept = _kept(json_path.parent, "imported")
        assert len(kept) == 1
        assert (json_path.parent / kept[0]).read_text("utf-8") == original

    def test_a_store_without_tables_yet_reads_the_json(self, store, caplog):
        """A reader that lands between a writer creating the file and its tables."""
        json_path, sqlite_path = store
        json_path.write_text(json.dumps({"hallways": RECORDS}), "utf-8")
        sqlite_path.touch()
        with caplog.at_level("WARNING", logger="mempalace.hallways"):
            assert hallways_mod.list_hallways() == RECORDS
        assert not caplog.records

    def test_a_legacy_list_document_imports_too(self, store):
        json_path, _ = store
        json_path.write_text(json.dumps(RECORDS), "utf-8")
        hallways_mod.drop_wing_hallways("none")
        assert hallways_mod.list_hallways() == RECORDS

    def test_a_json_written_after_the_import_is_not_imported_again(self, store):
        json_path, _ = store
        json_path.write_text(json.dumps({"hallways": RECORDS}), "utf-8")
        hallways_mod.delete_hallway(RECORDS[1]["id"])
        stale = [dict(RECORDS[1], co_occurrence_count=99)]
        json_path.write_text(json.dumps({"hallways": stale}), "utf-8")

        hallways_mod.delete_hallway("no_such_id")

        assert hallways_mod.list_hallways() == [RECORDS[0]]
        assert json_path.exists()

    def test_a_corrupt_json_refuses_the_write_and_reads_as_empty(self, store):
        json_path, sqlite_path = store
        json_path.write_text('{"hallways": [', "utf-8")
        assert hallways_mod.list_hallways() == []
        with pytest.raises(OSError):
            hallways_mod.delete_hallway("x")
        assert _kept(json_path.parent, "corrupt")
        # The refused write left nothing imported: the next write starts empty.
        hallways_mod.delete_hallway("x")
        assert hallways_mod.list_hallways() == []

    def test_records_round_trip_verbatim(self, store):
        odd = [
            {"wing": "w", "entity_a": "A", "entity_b": "B", "note": "hand-made, no id"},
            {
                "id": "n",
                "wing": "w",
                "co_occurrence_count": "7",
                "rooms": None,
                "extra": {"k": [1]},
            },
        ]
        hallways_mod._save_hallways(odd)
        assert hallways_mod.list_hallways() == odd

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
    def test_the_store_is_private(self, store):
        _, sqlite_path = store
        hallways_mod._save_hallways(RECORDS)
        assert os.stat(sqlite_path).st_mode & 0o777 == 0o600


class TestPerWingWrites:
    DRAWERS = [
        {"wing": "w", "room": "r1", "entities": "Aya;Lumi"},
        {"wing": "w", "room": "r2", "entities": "Aya;Lumi;Kai"},
        {"wing": "w", "room": "r2", "entities": "Aya;Kai"},
    ]

    def _other_wings(self, n=300):
        return [
            {
                "id": f"hallway_big_{i}",
                "wing": "big",
                "entity_a": f"E{i}",
                "entity_b": f"F{i}",
                "co_occurrence_count": i % 7 + 1,
            }
            for i in range(n)
        ]

    def test_a_mine_never_loads_or_rewrites_other_wings(self, store, monkeypatch):
        _, sqlite_path = store
        hallways_mod._save_hallways(self._other_wings())
        before = _rows(sqlite_path, "big")

        def whole_store(*_a, **_k):
            raise AssertionError("a mine read every hallway of the palace")

        monkeypatch.setattr(hallways_mod, "_load_hallways", whole_store)
        monkeypatch.setattr(hallways_mod, "_load_hallways_for_write", whole_store)
        created = hallways_mod.compute_hallways_for_wing(
            "w", col=_collection(self.DRAWERS), min_count=1
        )

        assert {(h["entity_a"], h["entity_b"]) for h in created} == {
            ("Aya", "Kai"),
            ("Aya", "Lumi"),
            ("Kai", "Lumi"),
        }
        assert _rows(sqlite_path, "big") == before

    def test_an_unchanged_record_is_not_rewritten(self, store):
        _, sqlite_path = store
        col = _collection(self.DRAWERS)
        first = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)
        stored = _rows(sqlite_path, "w")

        again = hallways_mod.compute_hallways_for_wing("w", col=col, min_count=1)

        assert again == first
        assert _rows(sqlite_path, "w") == stored

    def test_a_changed_pair_is_rewritten_in_place_and_a_vanished_one_dropped(self, store):
        _, sqlite_path = store
        hallways_mod.compute_hallways_for_wing("w", col=_collection(self.DRAWERS), min_count=1)
        stored = _rows(sqlite_path, "w")
        by_pair = {
            (r["entity_a"], r["entity_b"]): (hid, rowid) for hid, (rowid, r) in stored.items()
        }

        drawers = self.DRAWERS[:2] + [{"wing": "w", "room": "r3", "entities": "Aya;Lumi"}]
        created = hallways_mod.compute_hallways_for_wing("w", col=_collection(drawers), min_count=1)

        after = _rows(sqlite_path, "w")
        counts = {(h["entity_a"], h["entity_b"]): h["co_occurrence_count"] for h in created}
        assert counts == {("Aya", "Kai"): 1, ("Aya", "Lumi"): 3, ("Kai", "Lumi"): 1}
        changed_id, changed_rowid = by_pair[("Aya", "Lumi")]
        assert after[changed_id][0] == changed_rowid
        assert after[changed_id][1]["co_occurrence_count"] == 3
        unchanged_id = by_pair[("Kai", "Lumi")][0]
        assert after[unchanged_id] == stored[unchanged_id]
        assert set(after) == {h["id"] for h in created}

    def test_concurrent_mines_of_two_wings_keep_both(self, store):
        cols = {
            wing: _collection([dict(d, wing=wing) for d in self.DRAWERS]) for wing in ("w1", "w2")
        }
        errors = []

        def mine(wing):
            try:
                for _ in range(5):
                    hallways_mod.compute_hallways_for_wing(wing, col=cols[wing], min_count=1)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=mine, args=(w,)) for w in cols]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors
        assert len(hallways_mod.list_hallways("w1")) == 3
        assert len(hallways_mod.list_hallways("w2")) == 3

    def test_drop_wing_hallways_removes_only_that_wing(self, store):
        hallways_mod._save_hallways(RECORDS + self._other_wings(5))
        assert hallways_mod.drop_wing_hallways("big") == 5
        assert hallways_mod.list_hallways() == RECORDS


# Entity tunnels read one strength per (wing, entity); the store hands over
# only that. The candidates must come out the same as from every record.
_ENTITIES = [
    "Aya",
    "Lumi",
    "ChatStore",
    "ChatStore.swift",
    "src/main.zig",
    "main.zig",
    "content",
    "src/models/user.py",
    "tests/models/user.py",
    "user.py",
    None,
]
_WINGS = ["alpha", "beta", "gamma", "delta", "epsilon"]

_record = st.fixed_dictionaries(
    {
        "wing": st.sampled_from(_WINGS),
        "entity_a": st.sampled_from(_ENTITIES),
        "entity_b": st.sampled_from(_ENTITIES),
    },
    optional={"co_occurrence_count": st.integers(min_value=0, max_value=9)},
)


def _numbered(records):
    return [dict(r, id=f"h{i}") for i, r in enumerate(records)]


class TestEntityStrengthRecords:
    def test_the_candidates_match_those_from_every_record(self, store):
        hallways_mod._save_hallways(
            _numbered(
                [
                    {
                        "wing": "alpha",
                        "entity_a": "ChatStore",
                        "entity_b": "Aya",
                        "co_occurrence_count": 4,
                    },
                    {
                        "wing": "alpha",
                        "entity_a": "ChatStore.swift",
                        "entity_b": "Lumi",
                        "co_occurrence_count": 7,
                    },
                    {
                        "wing": "beta",
                        "entity_a": "ChatStore",
                        "entity_b": "Lumi",
                        "co_occurrence_count": 3,
                    },
                    {"wing": "beta", "entity_a": "Aya", "entity_b": "main.zig"},
                    {
                        "wing": "gamma",
                        "entity_a": "src/main.zig",
                        "entity_b": "Aya",
                        "co_occurrence_count": 5,
                    },
                    {
                        "wing": "gamma",
                        "entity_a": "content",
                        "entity_b": "Lumi",
                        "co_occurrence_count": 9,
                    },
                ]
            )
        )
        full = entity_tunnel_candidates(hallways_mod.list_hallways())
        assert full
        assert entity_tunnel_candidates(hallways_mod.entity_strength_records()) == full

    @settings(
        max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    @given(records=st.lists(_record, max_size=40), min_count=st.integers(min_value=0, max_value=5))
    def test_the_candidates_match_for_any_records(self, store, records, min_count):
        hallways_mod._save_hallways(_numbered(records))
        full = entity_tunnel_candidates(hallways_mod.list_hallways(), min_count=min_count)
        reduced = entity_tunnel_candidates(
            hallways_mod.entity_strength_records(), min_count=min_count
        )
        assert reduced == full

    def test_before_the_import_the_full_records_are_used(self, store):
        json_path, sqlite_path = store
        json_path.write_text(json.dumps({"hallways": RECORDS}), "utf-8")
        assert hallways_mod.entity_strength_records() == RECORDS
        assert not sqlite_path.exists()
