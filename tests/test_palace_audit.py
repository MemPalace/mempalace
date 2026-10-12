"""Tests for mempalace.palace_audit and the `mempalace audit` CLI command."""

import json
import re
import sqlite3
from argparse import Namespace

import pytest

from mempalace import palace_audit
from mempalace.hallways import is_generic_entity
from mempalace.palace_audit import (
    _analyze_hallways,
    _name_list,
    _analyze_naming,
    _analyze_rooms,
    _analyze_tunnels,
    _read_kg,
    audit_palace,
    drift_key,
    hallway_entity_key,
    render_audit,
    terminal_progress,
)

# ── name normalization ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "a, b",
    [
        ("acme-app", "acme_app"),
        ("release-3.6.0", "release_3_6_0"),
        ("concierge-automation", "concierge-automations"),
        ("PR Reviews", "pr_reviews"),
    ],
)
def test_drift_key_collides_drifted_spellings(a, b):
    assert drift_key(a) == drift_key(b)


def test_drift_key_keeps_distinct_names_apart():
    assert drift_key("ops") != drift_key("operations")
    assert drift_key("arcade") != drift_key("arcade_game")


@pytest.mark.parametrize(
    "a, b",
    [
        ("mcp_server", "mcp_server.py"),
        ("main.zig", "src/main.zig"),
        ("RootView", "Users/x/dev/acme-app/RootView.swift"),
        ("device.zig", "src\\wireguard\\device.zig"),
    ],
)
def test_hallway_entity_key_collapses_self_links(a, b):
    assert hallway_entity_key(a) == hallway_entity_key(b)


def test_hallway_entity_key_keeps_methods_and_domains_apart():
    assert hallway_entity_key("ChatStore") != hallway_entity_key("ChatStore.send")
    assert hallway_entity_key("MemPalace") != hallway_entity_key("github.com")


def test_generic_entity_heuristic():
    assert is_generic_entity("content")
    assert is_generic_entity("thinking")
    assert not is_generic_entity("ChatStore")
    assert not is_generic_entity("store.baseURL")
    assert not is_generic_entity("weatherstation")
    assert (
        is_generic_entity("WebFetch") and is_generic_entity("Server") and is_generic_entity("lib/")
    )
    assert is_generic_entity("compose.yml") and not is_generic_entity("pool.ts")
    assert (
        is_generic_entity("/app")
        and is_generic_entity("MESSAGES")
        and is_generic_entity("cancelled")
    )
    assert not is_generic_entity("chain.token") and not is_generic_entity("clientsite_user")
    # Generic source-file stems and library references link nothing.
    for name in ("app.js", "model.ts", "mod.rs", "main.py", "index.tsx", "repository.ts"):
        assert is_generic_entity(name), name
    for name in ("pathlib.Path", "page.evaluate", "console.log", "os.path.join", "np.array"):
        assert is_generic_entity(name), name
    for name in ("Cargo.toml", "ROADMAP.md", "ChangeDetectionStrategy.OnPush", "created_by"):
        assert is_generic_entity(name), name
    # A project's own file or qualified symbol still passes.
    for name in ("ChatStore.swift", "swim.zig", "wing_split.py", "chain.token", "store.baseURL"):
        assert not is_generic_entity(name), name


# ── analyses on synthetic counts ────────────────────────────────────────────

WING_ROOMS = {
    "convos": {"technical": 950, "planning": 40, "decisions": 10},
    "acme-app": {"decisions": 3},
    "acme_app": {"decisions": 4, "diary": 2},
    "mempalace": {"release-3.6.0": 2, "release_3_6_0": 1, "reviews": 5},
    "arcade": {"general": 1},
    "arcade_game": {"design": 8},
}


def test_analyze_rooms_generic_share_and_flat_wings():
    rooms = _analyze_rooms(WING_ROOMS)
    assert rooms["total_drawers"] == 1026
    assert rooms["generic_drawers"] == 991
    assert rooms["generic_share"] == pytest.approx(991 / 1026, abs=1e-4)
    assert [f["wing"] for f in rooms["flat_wings"]] == ["convos"]
    assert rooms["flat_wings"][0]["room"] == "technical"
    # Cross-wing rooms are sorted by how many wings share them.
    assert rooms["cross_wing_rooms"][0]["room"] == "decisions"
    assert rooms["cross_wing_rooms"][0]["wings"] == 3


def test_analyze_rooms_empty_palace():
    rooms = _analyze_rooms({})
    assert rooms["total_drawers"] == 0
    assert rooms["generic_share"] == 0.0
    assert rooms["flat_wings"] == []


def test_analyze_naming_detects_wing_and_room_drift():
    naming = _analyze_naming(WING_ROOMS)
    assert naming["wing_drift"] == [["acme-app", "acme_app"]]
    assert ["arcade", "arcade_game"] in naming["wing_prefix_pairs"]
    assert any(d["rooms"] == ["release-3.6.0", "release_3_6_0"] for d in naming["room_drift"])
    assert {t["wing"] for t in naming["tiny_wings"]} == {"acme-app", "arcade"}


def test_analyze_naming_flags_wings_that_mix_source_projects():
    mix = {
        "convos": {"projects": 40, "drawers": 5000, "top": [("p--a", 900), ("p--b", 800)]},
        "mempalace": {"projects": 2, "drawers": 200, "top": [("-x", 200)]},
        "gone": {"projects": 90, "drawers": 9000, "top": []},  # not in wing_rooms → ignored
    }
    naming = _analyze_naming({"convos": {"technical": 5000}, "mempalace": {"decisions": 200}}, mix)
    assert [m["wing"] for m in naming["mixed_wings"]] == ["convos"]
    assert naming["mixed_wings"][0]["projects"] == 40
    assert _analyze_naming(WING_ROOMS, None)["mixed_wings"] == []


def test_analyze_naming_prefix_pairs_are_not_duplicated_with_drift_groups():
    naming = _analyze_naming({"foo": {"a": 1}, "foo_bar": {"a": 1}, "foo-bar": {"a": 1}})
    assert naming["wing_drift"] == [["foo-bar", "foo_bar"]]
    # foo/foo_bar and foo/foo-bar are both prefix pairs; the drift group is
    # reported once and never re-listed as a prefix pair.
    assert ["foo", "foo-bar"] in naming["wing_prefix_pairs"]
    assert ["foo", "foo_bar"] in naming["wing_prefix_pairs"]
    assert ["foo-bar", "foo_bar"] not in naming["wing_prefix_pairs"]


def test_analyze_tunnels_counts_untraversed_and_generic():
    tunnels = [
        {
            "kind": "entity",
            "access_count": 0,
            "source": {"wing": "a", "room": "entity:content"},
            "target": {"wing": "b", "room": "entity:content"},
        },
        {
            "kind": "entity",
            "access_count": 3,
            "source": {"wing": "a", "room": "entity:ChatStore"},
            "target": {"wing": "b", "room": "entity:ChatStore"},
        },
        {"kind": "manual", "source": {"wing": "a", "room": "decisions"}, "target": None},
        "corrupt-record",
    ]
    out = _analyze_tunnels(tunnels)
    assert out["total"] == 4
    assert out["by_kind"] == {"entity": 2, "manual": 1}
    assert out["never_traversed"] == 2
    assert out["generic_entities"] == ["content"]
    assert out["artifacts"] == 1 and out["dangling_endpoints"] == 0


def test_analyze_tunnels_flags_dangling_and_duplicate_spellings():
    tunnels = [
        {
            "kind": "entity",
            "access_count": 5,
            "source": {"wing": "a", "room": "entity:ChatStore"},
            "target": {"wing": "b", "room": "entity:ChatStore"},
        },
        {
            "kind": "entity",
            "access_count": 0,
            "source": {"wing": "b", "room": "entity:ChatStore.swift"},
            "target": {"wing": "a", "room": "entity:ChatStore.swift"},
        },
        {
            "kind": "entity",
            "access_count": 0,
            "source": {"wing": "a", "room": "entity:RootView"},
            "target": {"wing": "gone", "room": "entity:RootView"},
        },
    ]
    out = _analyze_tunnels(tunnels, wings={"a", "b"})
    assert out["duplicates"] == 1  # same wing pair, same entity under another spelling
    assert out["dangling_endpoints"] == 1
    assert out["artifacts"] == 2
    assert out["artifact_share"] == pytest.approx(2 / 3, abs=1e-4)


def test_analyze_hallways_counts_self_links():
    hallways = [
        {"entity_a": "mcp_server", "entity_b": "mcp_server.py"},
        {"entity_a": "daemon.py", "entity_b": "service.py"},
        {"entity_a": "ChatStore", "entity_b": "ChatStore.send"},
        {"entity_a": None, "entity_b": "x"},
    ]
    out = _analyze_hallways(hallways)
    assert out["total"] == 4
    assert out["self_links"] == 1
    assert out["duplicates"] == 0
    assert out["artifact_sample"] == ["mcp_server ↔ mcp_server.py"]
    assert out["top_n"] == 4


def test_analyze_hallways_counts_spelling_variant_duplicates():
    hallways = [
        {"wing": "w", "entity_a": "ChatStore", "entity_b": "RootView", "co_occurrence_count": 240},
        {
            "wing": "w",
            "entity_a": "ChatStore.swift",
            "entity_b": "RootView",
            "co_occurrence_count": 221,
        },
        {
            "wing": "w",
            "entity_a": "ChatStore.swift",
            "entity_b": "RootView.swift",
            "co_occurrence_count": 218,
        },
        {
            "wing": "other",
            "entity_a": "ChatStore.swift",
            "entity_b": "RootView",
            "co_occurrence_count": 3,
        },
        {"wing": "w", "entity_a": "codec.zig", "entity_b": "swim.zig", "co_occurrence_count": 195},
    ]
    out = _analyze_hallways(hallways)
    assert out["self_links"] == 0
    assert out["duplicates"] == 2  # same association in the same wing; other wing is its own group
    assert out["top_artifacts"] == 2
    assert out["artifact_share"] == pytest.approx(0.4)
    assert out["artifact_sample"] == [
        "ChatStore.swift ↔ RootView",
        "ChatStore.swift ↔ RootView.swift",
    ]


def test_analyze_hallways_scores_the_strongest_links():
    # 2 self-links among 200 overall (1%), but both are the strongest — the
    # slice a traversal reads — so the top-N share is what gets reported.
    weak = [
        {"entity_a": f"a{i}", "entity_b": f"b{i}", "co_occurrence_count": 1} for i in range(198)
    ]
    strong = [
        {"entity_a": "main.zig", "entity_b": "src/main.zig", "co_occurrence_count": 400},
        {"entity_a": "daemon", "entity_b": "daemon.py", "co_occurrence_count": 300},
    ]
    out = _analyze_hallways(weak + strong)
    assert out["artifact_share"] == pytest.approx(0.01)
    assert out["top_n"] == 100
    assert out["top_artifacts"] == 2
    assert out["top_artifact_share"] == pytest.approx(0.02)


# ── knowledge graph reader ──────────────────────────────────────────────────


def _make_kg(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE entities (id TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE triples (
            id TEXT PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT,
            valid_from TEXT, valid_to TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO triples (id, subject, predicate, object, valid_to) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.executemany("INSERT INTO entities VALUES (?, ?)", [("e1", "A"), ("e2", "B")])
    conn.commit()
    conn.close()


def test_read_kg_predicate_reuse(tmp_path):
    kg = tmp_path / "kg.sqlite3"
    _make_kg(
        kg,
        [
            ("t1", "e1", "works_on", "e2", None),
            ("t2", "e2", "works_on", "e1", None),
            ("t3", "e1", "one_off_fact", "e2", "2026-01-01"),
        ],
    )
    out = _read_kg(str(kg))
    assert out["entities"] == 2
    assert out["triples"] == 3
    assert out["current_facts"] == 2
    assert out["expired_facts"] == 1
    # The expired one_off_fact is history, not a predicate in use.
    assert out["distinct_predicates"] == 1
    assert out["one_off_sample"] == []
    assert out["predicate_reuse"] == pytest.approx(1 - 1 / 2, abs=1e-4)


def test_read_kg_missing_file_returns_none_and_does_not_create_it(tmp_path):
    missing = tmp_path / "nope.sqlite3"
    assert _read_kg(str(missing)) is None
    assert not missing.exists()


# ── end to end ──────────────────────────────────────────────────────────────


@pytest.fixture
def fake_palace(tmp_path, monkeypatch):
    """A palace directory whose readers are stubbed at the seams."""
    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setattr(palace_audit, "_read_wing_room_counts", lambda config: dict(WING_ROOMS))
    monkeypatch.setattr(palace_audit, "_read_wing_project_mix", lambda config: None)
    monkeypatch.setattr(
        palace_audit,
        "_load_tunnels",
        lambda config=None: [
            {
                "kind": "entity",
                "access_count": 0,
                "source": {"wing": "a", "room": "entity:thinking"},
                "target": {"wing": "b", "room": "entity:thinking"},
            }
        ],
    )
    monkeypatch.setattr(
        palace_audit,
        "list_hallways",
        lambda wing=None, config=None: [
            {"entity_a": "main.zig", "entity_b": "src/main.zig"},
            {"entity_a": "codec.zig", "entity_b": "swim.zig"},
        ],
    )
    _make_kg(palace / "knowledge_graph.sqlite3", [("t1", "e1", "p", "e2", None)])
    return palace


def test_audit_palace_end_to_end(fake_palace):
    report = audit_palace(palace_path=str(fake_palace))
    scores = report["scores"]
    assert report["counts_source"] == "sqlite"
    assert scores["rooms"] == 3  # 991/1026 generic → 3.4% navigable
    assert scores["naming"] == 80  # one wing drift group + one room drift group
    assert scores["tunnels"] == 0
    assert scores["hallways"] == 50
    assert scores["knowledge_graph"] == 0  # 1 predicate for 1 fact
    assert scores["overall"] == round((3 + 80 + 0 + 50 + 0) / 5)
    assert report["knowledge_graph"]["path"] == str(fake_palace / "knowledge_graph.sqlite3")
    joined = "\n".join(f["text"] for f in report["findings"])
    assert "acme-app / acme_app" in joined
    assert "thinking" in joined
    assert "main.zig" not in joined  # samples live in --json, not findings
    assert "never been followed" in joined
    # Findings come out grouped by layer, in layer order.
    layers = [f["layer"] for f in report["findings"]]
    assert layers == sorted(
        layers, key=["rooms", "naming", "tunnels", "hallways", "knowledge graph"].index
    )


def test_audit_palace_reports_progress_per_reader(fake_palace):
    calls = []
    audit_palace(
        palace_path=str(fake_palace), progress=lambda step, detail: calls.append((step, detail))
    )
    steps = [s for s, d in calls if d is None]
    assert steps == ["drawers", "tunnels", "hallways", "knowledge graph", "source projects"]
    done = dict((s, d) for s, d in calls if d is not None)
    assert done["drawers"] == "1026 in 6 wings"
    assert done["tunnels"] == "1"
    assert done["hallways"] == "2"
    assert done["knowledge graph"] == "1 facts"


def test_terminal_progress_writes_one_timed_line_per_step():
    import io

    buf = io.StringIO()
    cb = terminal_progress(buf)
    cb("hallways", None)
    cb("hallways", "148791")
    out = buf.getvalue()
    assert out.startswith("  reading hallways...")
    assert re.search(r" 148791 \(\d+\.\ds\)\n$", out)


def test_name_list_caps_long_lists():
    assert _name_list(["a", "b"]) == "a, b"
    assert _name_list(list("abcdefgh")) == "a, b, c, d, e, f, +2 more"


def test_render_audit_wraps_to_width_with_hanging_indent(fake_palace):
    report = audit_palace(palace_path=str(fake_palace))
    text = render_audit(report, width=50)
    body = [ln for ln in text.splitlines() if ln.startswith("    ")]
    assert body, "expected wrapped finding lines"
    assert all(len(ln) <= 50 for ln in body)
    continuations = [ln for ln in body if not ln.startswith("    - ")]
    assert continuations and all(ln.startswith("      ") for ln in continuations)
    assert "  ROOMS" in text and "  NAMING" in text


def test_audit_palace_falls_back_to_collection(fake_palace, monkeypatch):
    monkeypatch.setattr(palace_audit, "_read_wing_room_counts", lambda config: None)
    monkeypatch.setattr(
        palace_audit, "_read_wing_room_counts_from_collection", lambda config: dict(WING_ROOMS)
    )
    report = audit_palace(palace_path=str(fake_palace))
    assert report["counts_source"] == "collection"


def test_audit_palace_missing_dir(tmp_path):
    with pytest.raises(FileNotFoundError):
        audit_palace(palace_path=str(tmp_path / "missing"))


def test_audit_palace_unreadable_counts(fake_palace, monkeypatch):
    monkeypatch.setattr(palace_audit, "_read_wing_room_counts", lambda config: None)
    monkeypatch.setattr(palace_audit, "_read_wing_room_counts_from_collection", lambda config: None)
    with pytest.raises(RuntimeError):
        audit_palace(palace_path=str(fake_palace))


def test_render_audit_marks_empty_layers_na(fake_palace, monkeypatch):
    monkeypatch.setattr(palace_audit, "_load_tunnels", lambda config=None: [])
    report = audit_palace(palace_path=str(fake_palace))
    assert report["scores"]["tunnels"] is None
    text = render_audit(report)
    assert re.search(r"tunnels\s+n/a", text)
    assert "overall" in text


def test_render_audit_clean_palace():
    report = {
        "palace": "/p",
        "scores": {k: 100 for k in ("overall", "rooms", "naming", "tunnels", "hallways")}
        | {"knowledge_graph": None},
        "rooms": {"total_drawers": 3, "wings": 1, "distinct_rooms": 1},
        "tunnels": {"total": 0},
        "hallways": {"total": 0},
        "knowledge_graph": None,
        "findings": [],
    }
    assert "well organized" in render_audit(report)


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cmd_audit_text_and_json(fake_palace, capsys):
    from mempalace.cli import cmd_audit

    cmd_audit(Namespace(palace=str(fake_palace), json=False, fail_under=None))
    out = capsys.readouterr().out
    assert "MemPalace audit" in out
    assert "  ROOMS" in out

    cmd_audit(Namespace(palace=str(fake_palace), json=True, fail_under=None))
    data = json.loads(capsys.readouterr().out)
    assert data["scores"]["naming"] == 80
    assert data["hallways"]["artifact_sample"] == ["main.zig ↔ src/main.zig"]


def test_cmd_audit_no_progress_when_stderr_is_not_a_tty(fake_palace, capsys):
    from mempalace.cli import cmd_audit

    cmd_audit(Namespace(palace=str(fake_palace), json=False, fail_under=None))
    assert capsys.readouterr().err == ""


def test_cmd_audit_progress_on_tty_goes_to_stderr_only(fake_palace, capsys, monkeypatch):
    import sys

    from mempalace.cli import cmd_audit

    monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)
    cmd_audit(Namespace(palace=str(fake_palace), json=False, fail_under=None))
    captured = capsys.readouterr()
    assert "reading hallways..." in captured.err
    assert "reading" not in captured.out

    cmd_audit(Namespace(palace=str(fake_palace), json=True, fail_under=None))
    captured = capsys.readouterr()
    assert captured.err == ""
    json.loads(captured.out)


def test_cmd_audit_fail_under(fake_palace, capsys):
    from mempalace.cli import cmd_audit

    with pytest.raises(SystemExit) as exc:
        cmd_audit(Namespace(palace=str(fake_palace), json=False, fail_under=90))
    assert exc.value.code == 2
    capsys.readouterr()
    # At or above the threshold is a normal exit.
    cmd_audit(Namespace(palace=str(fake_palace), json=False, fail_under=10))


def test_cmd_audit_missing_palace_exits_1(tmp_path, capsys):
    from mempalace.cli import cmd_audit

    with pytest.raises(SystemExit) as exc:
        cmd_audit(Namespace(palace=str(tmp_path / "nope"), json=False, fail_under=None))
    assert exc.value.code == 1
    assert "palace not found" in capsys.readouterr().out


def test_main_dispatches_audit(monkeypatch):
    import mempalace.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_audit", lambda args: seen.update(vars(args)))
    monkeypatch.setattr("sys.argv", ["mempalace", "audit", "--json", "--fail-under", "50"])
    cli.main()
    assert seen["command"] == "audit"
    assert seen["json"] is True
    assert seen["fail_under"] == 50


def test_analyze_tunnels_coverage_counts_linkable_wings_without_a_sound_tunnel():
    hallways = [
        {"wing": "a", "entity_a": "ChatStore", "entity_b": "RootView", "co_occurrence_count": 40},
        {"wing": "b", "entity_a": "ChatStore", "entity_b": "Bridge", "co_occurrence_count": 12},
        {"wing": "c", "entity_a": "swim.zig", "entity_b": "codec.zig", "co_occurrence_count": 9},
        {"wing": "d", "entity_a": "swim.zig", "entity_b": "Mesh", "co_occurrence_count": 8},
        {"wing": "lonely", "entity_a": "Solo", "entity_b": "Alone", "co_occurrence_count": 99},
    ]
    good = {
        "kind": "entity",
        "access_count": 0,
        "source": {"wing": "a", "room": "entity:ChatStore"},
        "target": {"wing": "b", "room": "entity:ChatStore"},
    }
    dangling = {
        "kind": "entity",
        "source": {"wing": "c", "room": "entity:swim.zig"},
        "target": {"wing": "gone", "room": "entity:swim.zig"},
    }
    out = _analyze_tunnels(
        [good, dangling], wings={"a", "b", "c", "d", "lonely"}, hallways=hallways
    )
    assert out["linkable_wings"] == 4  # lonely shares nothing
    assert out["unlinked_wings"] == ["c", "d"]  # the dangling tunnel covers nothing
    assert out["coverage"] == pytest.approx(0.5)

    # No hallways: nothing is linkable, coverage is full.
    assert _analyze_tunnels([good])["coverage"] == 1.0
    assert _analyze_tunnels([good])["linkable_wings"] == 0


def test_score_tunnels_is_quality_times_coverage_and_never_traversal():
    from mempalace.palace_audit import _score

    rooms = {"total_drawers": 10, "generic_share": 0.0, "wings": ["a"]}
    naming = {"wing_drift": [], "room_drift": [], "mixed_wings": []}
    hallways = {"total": 0, "top_artifact_share": 0.0}

    def tunnels(**kw):
        base = {
            "total": 10,
            "artifact_share": 0.0,
            "never_traversed_share": 1.0,
            "coverage": 1.0,
            "linkable_wings": 4,
        }
        base.update(kw)
        return base

    assert _score(rooms, naming, tunnels(), hallways, None)["tunnels"] == 100
    assert _score(rooms, naming, tunnels(coverage=0.5), hallways, None)["tunnels"] == 50
    assert (
        _score(rooms, naming, tunnels(artifact_share=0.2, coverage=0.5), hallways, None)["tunnels"]
        == 40
    )
    # No tunnels but wings that could be linked: 0, not n/a.
    assert _score(rooms, naming, tunnels(total=0, coverage=0.0), hallways, None)["tunnels"] == 0
    # No tunnels and nothing linkable: the layer is empty.
    assert (
        _score(rooms, naming, tunnels(total=0, coverage=1.0, linkable_wings=0), hallways, None)[
            "tunnels"
        ]
        is None
    )


def test_resolve_kg_path_uses_the_home_graph_only_for_the_legacy_default_palace(
    tmp_path, monkeypatch
):
    import mempalace.config as config_mod
    import mempalace.knowledge_graph as kg_mod
    from mempalace.palace_audit import resolve_kg_path

    home = tmp_path / "home"
    default_palace = home / "palace"
    default_palace.mkdir(parents=True)
    home_graph = home / "knowledge_graph.sqlite3"
    home_graph.write_text("")
    monkeypatch.setattr(config_mod, "DEFAULT_PALACE_PATH", str(default_palace))
    monkeypatch.setattr(kg_mod, "DEFAULT_KG_PATH", str(home_graph))

    # The legacy default palace without a local graph uses the home graph...
    assert resolve_kg_path(str(default_palace)) == str(home_graph)
    # ...but a custom palace never does, however it was selected
    # (--palace, MEMPALACE_PALACE_PATH, config.json): no fallback.
    custom = tmp_path / "custom"
    custom.mkdir()
    assert resolve_kg_path(str(custom)) == str(custom / "knowledge_graph.sqlite3")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(custom))
    assert resolve_kg_path(str(custom)) == str(custom / "knowledge_graph.sqlite3")
    # A palace-local graph always wins, and --palace forces the local path.
    (default_palace / "knowledge_graph.sqlite3").write_text("")
    assert resolve_kg_path(str(default_palace)) == str(default_palace / "knowledge_graph.sqlite3")
    assert resolve_kg_path(str(custom), explicit=True) == str(custom / "knowledge_graph.sqlite3")


def test_analyze_hallways_does_not_count_same_named_files_as_duplicates():
    """The audit and --prune-spellings agree: two files are two associations."""
    from mempalace.palace_audit import _analyze_hallways

    def rec(a, b, n):
        return {"wing": "w", "entity_a": a, "entity_b": b, "co_occurrence_count": n}

    hallways = [
        rec("src/models/user.py", "Account", 9),
        rec("tests/models/user.py", "Account", 5),
        # A real spelling variant: only src/models/user.py can be this file.
        rec("repo/src/models/user.py", "Account.py", 3),
        # Ambiguous: could be either file, so it duplicates neither.
        rec("models/user.py", "Account", 2),
    ]
    out = _analyze_hallways(hallways)
    assert out["duplicates"] == 1
    assert out["artifact_sample"] == ["repo/src/models/user.py ↔ Account.py"]
