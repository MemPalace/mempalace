"""Wing migration keeps the real graph stores consistent without recomputing."""

import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from mempalace import hallways, palace_graph
from mempalace.config import MempalaceConfig
from mempalace.migrate import migrate_wing_names
from mempalace.palace import get_closets_collection, get_collection

DYNAMICS = {
    "strength": 0.73,
    "stability": 3.25,
    "last_activated": "2026-01-02T03:04:05+00:00",
    "access_count": 7,
}


@pytest.fixture
def graph_config(tmp_path):
    palace = tmp_path / "selected" / "palace"
    palace.mkdir(parents=True)
    return MempalaceConfig(palace_path=str(palace))


def _hallway(wing, **extra):
    return {
        "id": hallways._hallway_id(wing, "Aya", "Lumi"),
        "wing": wing,
        "entity_a": "Aya",
        "entity_b": "Lumi",
        "co_occurrence_count": 2,
        "rooms": ["diary"],
        "label": "Original label",
        "created_at": "2026-01-01",
        **DYNAMICS,
        **extra,
    }


def _tunnel(wing, room="room", target="beta", **extra):
    return {
        "id": palace_graph._canonical_tunnel_id(wing, room, target, room),
        "source": {"wing": wing, "room": room, "drawer_id": "drawer-verbatim"},
        "target": {"wing": target, "room": room},
        "kind": "topic" if room.startswith("topic:") else "entity",
        "label": "Original label",
        "created_at": "2026-01-01",
        **DYNAMICS,
        **extra,
    }


def _seed_hallways(config, records, storage):
    if storage == "sqlite":
        hallways._save_hallways(records, config)
    else:
        document = records if storage == "list" else {"schema_version": 1, "hallways": records}
        Path(config.hallway_file).write_text(json.dumps(document), encoding="utf-8")


def _sql_rows(config):
    with sqlite3.connect(hallways._get_hallway_db(config)) as conn:
        return dict(conn.execute("SELECT id, record FROM hallways"))


@pytest.mark.parametrize("storage", ["sqlite", "document", "list"])
def test_hallway_pure_rename_preserves_all_other_fields(graph_config, storage):
    old = _hallway("_alpha")
    untouched = _hallway("other")
    _seed_hallways(graph_config, [old, untouched], storage)
    assert hallways.rekey_hallway_wings({"_alpha": "alpha"}, graph_config) == 1
    expected = {**old, "wing": "alpha", "id": hallways._hallway_id("alpha", "Aya", "Lumi")}
    assert hallways.list_hallways("alpha", graph_config) == [expected]
    assert hallways.list_hallways("other", graph_config) == [untouched]
    before = _sql_rows(graph_config)
    assert hallways.rekey_hallway_wings({"_alpha": "alpha"}, graph_config) == 0
    assert _sql_rows(graph_config) == before
    if storage != "sqlite":
        kept = list(Path(graph_config.hallway_file).parent.glob("hallways.json.imported-*"))
        assert len(kept) == 1
        assert not Path(graph_config.hallway_file).exists()


def test_hallway_collision_merges_only_affected_pairs(graph_config):
    first = _hallway("_alpha")
    second = _hallway("alpha_", co_occurrence_count=3, rooms=["letters"], strength=2.5)
    target = _hallway("alpha", id="legacy-target", entity_a="Lumi", entity_b="Aya")
    untouched = [_hallway("other", id="first"), _hallway("other", id="duplicate")]
    hallways._save_hallways([first, second, target, *untouched], graph_config)
    before = _sql_rows(graph_config)
    assert hallways.rekey_hallway_wings({"_alpha": "alpha", "alpha_": "alpha"}, graph_config) == 2
    result = hallways.list_hallways("alpha", graph_config)
    assert len(result) == 1
    merged = result[0]
    assert merged["id"] == hallways._hallway_id("alpha", "Aya", "Lumi")
    assert merged["co_occurrence_count"] == 7
    assert merged["rooms"] == ["diary", "letters"]
    assert merged["strength"] == 2.5
    assert merged["access_count"] == 21
    assert merged["last_activated"] == DYNAMICS["last_activated"]
    assert merged["stability"] == DYNAMICS["stability"]
    assert merged["label"] == hallways._hallway_label("Lumi", "Aya", 7, merged["rooms"])
    assert hallways.list_hallways("other", graph_config) == untouched
    after = _sql_rows(graph_config)
    assert {key: after[key] for key in ("first", "duplicate")} == {
        key: before[key] for key in ("first", "duplicate")
    }


@pytest.mark.parametrize("helper", [hallways.rekey_hallway_wings, palace_graph.rekey_tunnel_wings])
@pytest.mark.parametrize("renames", [{}, {"alpha": "alpha"}, {"absent": "new"}])
def test_empty_identity_and_absent_maps_create_no_graph_store(graph_config, helper, renames):
    assert helper(renames, graph_config) == 0
    assert not Path(graph_config.hallway_file).exists()
    assert not Path(hallways._get_hallway_db(graph_config)).exists()
    assert not Path(graph_config.tunnel_file).exists()


def test_hallway_failed_collision_rolls_back_legacy_import(graph_config):
    records = [_hallway("_alpha"), _hallway("alpha", co_occurrence_count="invalid")]
    _seed_hallways(graph_config, records, "list")
    raw = Path(graph_config.hallway_file).read_bytes()
    with pytest.raises(ValueError):
        hallways.rekey_hallway_wings({"_alpha": "alpha"}, graph_config)
    assert Path(graph_config.hallway_file).read_bytes() == raw
    assert hallways.list_hallways(config=graph_config) == records
    with sqlite3.connect(hallways._get_hallway_db(graph_config)) as conn:
        assert conn.execute("SELECT count(*) FROM hallways").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM meta").fetchone()[0] == 0


@pytest.mark.parametrize("room", ["room", "topic:memory"])
def test_tunnel_pure_rename_preserves_endpoint_details_and_dynamics(graph_config, room):
    original = _tunnel("_alpha", room)
    untouched = _tunnel("other", room)
    palace_graph._save_tunnels([original, untouched, None], graph_config)
    assert palace_graph.rekey_tunnel_wings({"_alpha": "alpha"}, graph_config) == 1
    expected = deepcopy(original)
    expected["source"]["wing"] = "alpha"
    expected["id"] = palace_graph._canonical_tunnel_id("alpha", room, "beta", room)
    assert palace_graph._load_tunnels(graph_config) == [expected, untouched, None]
    before = Path(graph_config.tunnel_file).read_bytes()
    assert palace_graph.rekey_tunnel_wings({"_alpha": "alpha"}, graph_config) == 0
    assert Path(graph_config.tunnel_file).read_bytes() == before


def test_tunnel_collisions_use_endpoints_and_drop_new_self_links(graph_config):
    moved = _tunnel("_alpha", strength=2.5, last_activated="2026-02-01")
    target = _tunnel("alpha", id="legacy-id")
    target["source"], target["target"] = target["target"], target["source"]
    self_link = _tunnel("_alpha", target="alpha")
    # Different drawer details must not hide identical wing/room endpoints.
    untouched = [_tunnel("other", id="duplicate-1"), _tunnel("other", id="duplicate-2")]
    palace_graph._save_tunnels([moved, target, self_link, *untouched], graph_config)
    assert palace_graph.rekey_tunnel_wings({"_alpha": "alpha"}, graph_config) == 2
    result = palace_graph._load_tunnels(graph_config)
    assert len(result) == 3
    merged = result[0]
    assert merged["id"] == palace_graph._canonical_tunnel_id("alpha", "room", "beta", "room")
    assert merged["strength"] == 2.5
    assert merged["access_count"] == 14
    assert merged["stability"] == DYNAMICS["stability"]
    assert merged["last_activated"] == "2026-02-01"
    assert result[1:] == untouched


def _seed_drawers(config, wings):
    col = get_collection(config.palace_path, create=True)
    col.upsert(
        ids=[f"drawer-{i}" for i in range(len(wings))],
        documents=[f"Verbatim synthetic words {i}" for i in range(len(wings))],
        metadatas=[{"wing": wing, "room": "room", "entities": "Aya;Lumi"} for wing in wings],
        embeddings=[[float(i + 1)] * 8 for i in range(len(wings))],
    )
    return col


@pytest.mark.parametrize("storage", ["sqlite", "document", "list"])
def test_real_migration_uses_drawer_closet_and_topic_map(
    graph_config, monkeypatch, capsys, storage
):
    from mempalace import miner

    col = _seed_drawers(graph_config, ["_alpha", "alpha_", "beta"])
    before = col.get(include=["documents"])
    closets = get_closets_collection(graph_config.palace_path, create=True)
    closets.upsert(
        ids=["closet-id"],
        documents=["Exact closet words"],
        metadatas=[{"wing": "closet_", "room": "room"}],
        embeddings=[[1.0] * 8],
    )
    registry = Path(graph_config.palace_path) / "synthetic-entities.json"
    registry.write_text(json.dumps({"topics_by_wing": {"topic_": ["memory"]}}))
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    records = [_hallway(wing) for wing in ("_alpha", "alpha_", "closet_", "topic_", "orphan_")]
    _seed_hallways(graph_config, records, storage)
    palace_graph.create_tunnel("_alpha", "room", "beta", "room", config=graph_config)
    palace_graph.create_tunnel(
        "topic_", "topic:memory", "beta", "topic:memory", kind="topic", config=graph_config
    )
    pending = Path(graph_config.palace_path) / "wing-name-migration.pending.json"
    raw_hallways = hallways.list_hallways(config=graph_config)
    raw_tunnels = Path(graph_config.tunnel_file).read_bytes()
    assert migrate_wing_names(graph_config.palace_path, dry_run=True)
    assert not pending.exists()
    assert hallways.list_hallways(config=graph_config) == raw_hallways
    assert Path(graph_config.tunnel_file).read_bytes() == raw_tunnels
    assert migrate_wing_names(graph_config.palace_path, confirm=True)
    out = capsys.readouterr().out
    assert "4 hallway(s)" in out and "2 tunnel(s)" in out
    assert col.get(include=["documents"]) == before
    assert set(col.get(include=["metadatas"]).metadatas[i]["wing"] for i in range(3)) == {
        "alpha",
        "beta",
    }
    assert closets.get(include=["metadatas"]).metadatas[0]["wing"] == "closet"
    assert json.loads(registry.read_text())["topics_by_wing"] == {"topic": ["memory"]}
    assert {r["wing"] for r in hallways.list_hallways(config=graph_config)} == {
        "alpha",
        "closet",
        "topic",
        "orphan_",
    }
    assert hallways.list_hallways("alpha", graph_config)[0]["co_occurrence_count"] == 4
    assert {t["source"]["wing"] for t in palace_graph._load_tunnels(graph_config)} == {
        "alpha",
        "topic",
    }
    assert not pending.exists()
    assert migrate_wing_names(graph_config.palace_path, confirm=True) is False


@pytest.mark.parametrize("failed_store", ["hallway", "tunnel"])
def test_partial_graph_failure_can_retry_after_metadata_moved(
    graph_config, monkeypatch, capsys, failed_store
):
    col = _seed_drawers(graph_config, ["_alpha"])
    _seed_hallways(graph_config, [_hallway("_alpha")], "sqlite")
    palace_graph._save_tunnels([_tunnel("_alpha")], graph_config)
    module, method = (
        (hallways, "_row") if failed_store == "hallway" else (palace_graph, "_save_tunnels")
    )
    original = getattr(module, method)

    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(module, method, fail)
    assert migrate_wing_names(graph_config.palace_path, confirm=True)
    assert col.get(include=["metadatas"]).metadatas[0]["wing"] == "alpha"
    pending = Path(graph_config.palace_path) / "wing-name-migration.pending.json"
    assert json.loads(pending.read_text())["rename_map"] == {"_alpha": "alpha"}
    output = capsys.readouterr().out
    assert f"WARNING: {failed_store} re-key failed" in output
    assert "not rolled back" in output and "re-run migrate-wings" in output
    # The other graph store still completes and is not merged again on retry.
    assert hallways.list_hallways(config=graph_config)[0]["wing"] == (
        "_alpha" if failed_store == "hallway" else "alpha"
    )
    assert palace_graph._load_tunnels(graph_config)[0]["source"]["wing"] == (
        "_alpha" if failed_store == "tunnel" else "alpha"
    )
    monkeypatch.setattr(module, method, original)
    assert migrate_wing_names(graph_config.palace_path, dry_run=True)
    assert pending.exists()
    assert migrate_wing_names(graph_config.palace_path, confirm=True)
    assert not pending.exists()
    assert hallways.list_hallways(config=graph_config)[0] == _hallway("alpha")
    assert palace_graph._load_tunnels(graph_config)[0] == _tunnel("alpha")
    assert migrate_wing_names(graph_config.palace_path, confirm=True) is False


@pytest.mark.parametrize("filename", ["hallways.json", "tunnels.json"])
def test_corrupt_graph_is_kept_and_failure_reported(graph_config, filename, capsys):
    _seed_drawers(graph_config, ["_alpha"])
    path = Path(graph_config.hallway_file).parent / filename
    path.write_bytes(b"synthetic broken JSON")
    assert migrate_wing_names(graph_config.palace_path, confirm=True)
    assert "WARNING:" in capsys.readouterr().out
    assert (Path(graph_config.palace_path) / "wing-name-migration.pending.json").exists()
    kept = list(path.parent.glob(filename + ".corrupt-*"))
    assert len(kept) == 1 and kept[0].read_bytes() == b"synthetic broken JSON"


def test_declined_migration_leaves_graph_and_pending_untouched(graph_config, monkeypatch):
    _seed_drawers(graph_config, ["_alpha"])
    palace_graph._save_tunnels([_tunnel("_alpha")], graph_config)
    before = Path(graph_config.tunnel_file).read_bytes()
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert migrate_wing_names(graph_config.palace_path) is False
    assert Path(graph_config.tunnel_file).read_bytes() == before
    assert not (Path(graph_config.palace_path) / "wing-name-migration.pending.json").exists()


# Each invocation imports the public migration in a fresh process. The crash
# hook exits after the real quarantine rename, before its exception/warning.
_MIGRATION_CHILD = """
import os
import sys
from pathlib import Path

mode, palace, source = sys.argv[1:]
assert sys.version_info[:3] == (3, 12, 16)
os.environ["MEMPALACE_CONFIG_DIR"] = str(Path(palace).parent / "child-config")
from mempalace import paths
paths.state_home = lambda home=None: Path(palace).parent / "child-state"
import mempalace
assert Path(mempalace.__file__).resolve().parent == Path(source) / "mempalace"
from mempalace import fsio, palace_graph
from mempalace.migrate import migrate_wing_names
if mode == "crash":
    original = fsio.move_aside
    def crash(*args, **kwargs):
        kept = original(*args, **kwargs)
        if kwargs.get("label") == "corrupt":
            os._exit(86)
        return kept
    fsio.move_aside = crash
elif mode == "fail-tunnel":
    def fail(*args, **kwargs):
        raise OSError("synthetic tunnel write failure")
    palace_graph._save_tunnels = fail
assert migrate_wing_names(palace, confirm=True)
"""


def _migrate_in_fresh_process(config, mode="migrate", expected_exit=0):
    source = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": source}
    result = subprocess.run(
        [sys.executable, "-c", _MIGRATION_CHILD, mode, config.palace_path, source],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == expected_exit, result.stdout + result.stderr
    return result.stdout


@pytest.mark.parametrize("failed_store", ["hallway", "tunnel"])
@pytest.mark.parametrize("crash", [False, True])
def test_quarantined_input_blocks_completion_until_restored_in_fresh_process(
    graph_config, failed_store, crash
):
    col = _seed_drawers(graph_config, ["_alpha"])
    hallway_records = [_hallway("_alpha"), _hallway("alpha"), _hallway("other")]
    tunnel_records = [_tunnel("_alpha"), _tunnel("alpha"), _tunnel("other")]
    if failed_store == "hallway":
        path = Path(graph_config.hallway_file)
        valid = json.dumps(hallway_records)
        palace_graph._save_tunnels(tunnel_records, graph_config)
    else:
        path = Path(graph_config.tunnel_file)
        valid = json.dumps(tunnel_records)
        hallways._save_hallways(hallway_records, graph_config)
    damaged = valid + " trailing-corruption"
    path.write_text(damaged, encoding="utf-8")
    _migrate_in_fresh_process(graph_config, "crash" if crash else "migrate", 86 if crash else 0)
    assert col.get(include=["metadatas"]).metadatas[0]["wing"] == "alpha"
    pending = Path(graph_config.palace_path) / "wing-name-migration.pending.json"
    journal = json.loads(pending.read_text())
    assert journal["rename_map"] == {"_alpha": "alpha"}
    assert journal["required_graph_inputs"] == {"hallway": True, "tunnel": True}
    assert not path.exists()
    kept = list(path.parent.glob(path.name + ".corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == damaged

    # Retry before repair must retain recovery information and must not commit
    # an empty SQLite import that would hide subsequently restored JSON.
    output = _migrate_in_fresh_process(graph_config)
    assert f"WARNING: {failed_store} re-key failed" in output
    assert "expected graph input is missing" in output
    assert json.loads(pending.read_text()) == journal
    if failed_store == "hallway":
        with sqlite3.connect(hallways._get_hallway_db(graph_config)) as conn:
            assert (
                conn.execute("SELECT count(*) FROM meta WHERE key = 'json_imported'").fetchone()[0]
                == 0
            )
    path.write_text(valid, encoding="utf-8")
    output = _migrate_in_fresh_process(graph_config)
    assert "WARNING:" not in output
    assert f"1 {failed_store}(s)" in output
    assert not pending.exists()
    merged_h = hallways.list_hallways("alpha", graph_config)
    assert len(merged_h) == 1
    assert merged_h[0]["access_count"] == 14
    assert merged_h[0]["co_occurrence_count"] == 4
    assert hallways.list_hallways("other", graph_config) == [hallway_records[2]]
    tunnels = palace_graph._load_tunnels(graph_config)
    merged_t = next(t for t in tunnels if t["source"]["wing"] == "alpha")
    assert merged_t["access_count"] == 14
    assert next(t for t in tunnels if t["source"]["wing"] == "other") == tunnel_records[2]
    assert migrate_wing_names(graph_config.palace_path, confirm=True) is False


def test_ordinary_write_failure_retries_in_fresh_process_without_double_merge(graph_config):
    _seed_drawers(graph_config, ["_alpha"])
    hallways._save_hallways([_hallway("_alpha"), _hallway("alpha")], graph_config)
    palace_graph._save_tunnels([_tunnel("_alpha"), _tunnel("alpha")], graph_config)
    output = _migrate_in_fresh_process(graph_config, "fail-tunnel")
    assert "WARNING: tunnel re-key failed" in output
    assert hallways.list_hallways("alpha", graph_config)[0]["access_count"] == 14
    output = _migrate_in_fresh_process(graph_config)
    assert "WARNING:" not in output
    assert "0 hallway(s), 1 tunnel(s)" in output
    assert hallways.list_hallways("alpha", graph_config)[0]["access_count"] == 14
    assert palace_graph._load_tunnels(graph_config)[0]["access_count"] == 14
    assert not (Path(graph_config.palace_path) / "wing-name-migration.pending.json").exists()


@pytest.mark.parametrize("storage", ["sqlite", "list"])
@pytest.mark.parametrize(
    ("destination", "source", "winner"),
    [
        ("2026-01-02T11:00:00", "2026-01-02T10:00:00.000000+00:00", "source"),
        ("2026-01-02T10:00:00.000000+00:00", "2026-01-02T11:00:00", "destination"),
        ("2026-01-02T11:00:00+02:00", "2026-01-02T10:00:00+00:00", "source"),
    ],
)
def test_public_migration_merges_activation_instants_without_rewriting_values(
    graph_config, monkeypatch, storage, destination, source, winner
):
    from mempalace import timeutil

    monkeypatch.setattr(timeutil, "_LOCAL_TZ", timezone(timedelta(hours=2)))
    _seed_drawers(graph_config, ["_alpha", "_pure"])
    old = _hallway("_alpha", last_activated=source, strength=2.5, stability=9.0, rooms=["letters"])
    target = _hallway("alpha", last_activated=destination)
    pure = _hallway("_pure", last_activated=destination)
    _seed_hallways(graph_config, [old, target, pure], storage)
    tunnels = []
    for room in ("room", "topic:memory"):
        tunnels.extend(
            [
                _tunnel("_alpha", room, last_activated=source, strength=2.5, stability=9.0),
                _tunnel("alpha", room, last_activated=destination),
            ]
        )
    pure_t = _tunnel("_pure", last_activated=destination)
    palace_graph._save_tunnels([*tunnels, pure_t], graph_config)
    assert migrate_wing_names(graph_config.palace_path, confirm=True)
    expected = source if winner == "source" else destination
    merged = hallways.list_hallways("alpha", graph_config)[0]
    assert merged["last_activated"] == expected
    assert merged["co_occurrence_count"] == 4
    assert merged["rooms"] == ["diary", "letters"]
    assert merged["strength"] == 2.5 and merged["stability"] == 9.0
    assert merged["access_count"] == 14
    assert hallways.list_hallways("pure", graph_config) == [
        {**pure, "wing": "pure", "id": hallways._hallway_id("pure", "Aya", "Lumi")}
    ]
    result = palace_graph._load_tunnels(graph_config)
    for tunnel in result:
        if tunnel["source"]["wing"] == "alpha":
            assert tunnel["last_activated"] == expected
            assert tunnel["strength"] == 2.5 and tunnel["stability"] == 9.0
            assert tunnel["access_count"] == 14
    assert {t["kind"] for t in result if t["source"]["wing"] == "alpha"} == {"entity", "topic"}
    expected_pure = deepcopy(pure_t)
    expected_pure["source"]["wing"] = "pure"
    expected_pure["id"] = palace_graph._canonical_tunnel_id("pure", "room", "beta", "room")
    assert next(t for t in result if t["source"]["wing"] == "pure") == expected_pure
