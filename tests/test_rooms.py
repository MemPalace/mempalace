"""Tests for mempalace.rooms — closed room sets: propose (LLM) and apply (embedding decider)."""

import json
from argparse import Namespace

import pytest

from mempalace.config import MempalaceConfig
from mempalace.rooms import (
    EmbeddingRoomDecider,
    RoomSet,
    RoomSpec,
    apply_plan,
    load_room_set,
    plan_rooms,
    propose_rooms,
    sample_drawers,
    save_room_set,
    slugify_room,
)


class FakeCollection:
    """Enough of the collection contract for rooms: paged get(where=), get(ids=), update()."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self.updates = []

    def get(self, *, ids=None, where=None, limit=None, offset=None, include=None):
        include = include or []
        if ids is not None:
            picked = [self.rows[i] for i in ids if i in self.rows]
        else:
            picked = [
                r
                for r in self.rows.values()
                if not where or all(r["meta"].get(k) == v for k, v in where.items())
            ]
            picked = picked[offset or 0 :]
            if limit is not None:
                picked = picked[:limit]
        out = {"ids": [r["id"] for r in picked]}
        if "metadatas" in include:
            out["metadatas"] = [r["meta"] for r in picked]
        if "documents" in include:
            out["documents"] = [r.get("doc", "") for r in picked]
        if "embeddings" in include:
            out["embeddings"] = [r.get("emb") for r in picked]
        return out

    def update(self, *, ids, metadatas=None, documents=None, embeddings=None):
        self.updates.append((list(ids), list(metadatas)))
        for i, m in zip(ids, metadatas):
            self.rows[i]["meta"].update(m)


class FakeEmbed:
    """Two-dimensional 'embedder': 'release' text → x axis, 'bug' text → y axis."""

    def __call__(self, texts):
        out = []
        for t in texts:
            t = t.lower()
            out.append([1.0 if "release" in t else 0.0, 1.0 if "bug" in t else 0.0])
        return out


class FakeProvider:
    name = "fake"
    model = "m"

    def __init__(self, text, follow_up=None):
        self.text = text
        self.follow_up = follow_up
        self.calls = []

    def classify(self, system, user, json_mode=True, think=None):
        self.calls.append((system, user))
        text = self.text if len(self.calls) == 1 else (self.follow_up or "{}")
        return type("R", (), {"text": text})()

    def check_available(self):
        return True, "ok"

    @property
    def is_external_service(self):
        return False


def _rows():
    return [
        {
            "id": "r1",
            "meta": {"wing": "w", "room": "technical"},
            "doc": "cut the release",
            "emb": [1.0, 0.0],
        },
        {
            "id": "r2",
            "meta": {"wing": "w", "room": "releases"},
            "doc": "release notes",
            "emb": [0.9, 0.1],
        },
        {
            "id": "r3",
            "meta": {"wing": "w", "room": "technical"},
            "doc": "fix the bug",
            "emb": [0.0, 1.0],
        },
        {
            "id": "r4",
            "meta": {"wing": "w", "room": "general"},
            "doc": "weather chat",
            "emb": [0.2, 0.2],
        },
        {"id": "r5", "meta": {"wing": "w", "room": "general"}, "doc": "no vector", "emb": None},
        {
            "id": "x1",
            "meta": {"wing": "other", "room": "technical"},
            "doc": "other wing",
            "emb": [1.0, 0.0],
        },
    ]


# ── room set ──


def test_slugify_room():
    assert slugify_room("Release Process") == "release-process"
    assert slugify_room("  Bugs & Fixes!! ") == "bugs-fixes"
    assert slugify_room("Release 3.6.0") == "release-3.6.0"
    assert slugify_room("..odd..") == "odd"


def test_room_set_round_trip_and_validation(tmp_path):
    cfg = MempalaceConfig(palace_path=str(tmp_path))
    rs = RoomSet(wing="w", rooms=[RoomSpec("releases", "Release work", ["tag"])])
    path = save_room_set(cfg, rs)
    assert path == str(tmp_path / "rooms" / "w.json")
    loaded = load_room_set(cfg, "w")
    assert loaded.names() == ["releases"]
    assert loaded.rooms[0].keywords == ["tag"]
    with pytest.raises(ValueError):
        RoomSet.from_dict({"wing": "w", "rooms": []})
    with pytest.raises(ValueError):
        RoomSet.from_dict(
            {
                "wing": "w",
                "rooms": [{"name": "a", "description": ""}, {"name": "A", "description": ""}],
            }
        )


# ── sampling + propose ──


def test_sample_drawers_is_reproducible_and_wing_scoped():
    col = FakeCollection(_rows())
    a = sample_drawers(col, "w", n=3, seed=1)
    b = sample_drawers(col, "w", n=3, seed=1)
    assert [s["id"] for s in a] == [s["id"] for s in b]
    assert all(s["id"] != "x1" for s in a)
    assert all("excerpt" in s and s["room"] for s in a)
    assert sample_drawers(col, "missing") == []


def test_propose_rooms_parses_fenced_json_slugs_names_and_records_exemplars():
    provider = FakeProvider(
        '```json\n{"rooms": [{"name": "Release Process", "description": "cutting releases", '
        '"keywords": ["tag", "changelog"]}, {"name": "bug-fixes", "description": "fixing bugs"}], '
        '"assignments": [{"excerpt": 1, "room": "Release Process"}, {"excerpt": 2, "room": "bug-fixes"}, '
        '{"excerpt": 9, "room": "bug-fixes"}, {"excerpt": 1, "room": "nope"}]}\n```'
    )
    samples = sample_drawers(FakeCollection(_rows()), "w", n=2)
    rs = propose_rooms("w", samples, provider, max_rooms=5)
    assert rs.rooms[0].exemplars == [samples[0]["id"]]
    assert rs.rooms[1].exemplars == [samples[1]["id"]]  # out-of-range and unknown rooms ignored
    assert len(provider.calls) == 1  # inline assignments: no follow-up call


def test_propose_rooms_asks_again_when_inline_assignments_are_partial():
    # One valid label out of four samples is not enough to build centroids on.
    provider = FakeProvider(
        '{"rooms": [{"name": "releases", "description": "d"}, {"name": "bug-fixes", "description": "d"}], '
        '"assignments": [{"excerpt": 1, "room": "releases"}]}',
        follow_up='{"assignments": [{"excerpt": 1, "room": "releases"}, {"excerpt": 2, "room": "bug-fixes"}, '
        '{"excerpt": 3, "room": "bug-fixes"}, {"excerpt": 4, "room": "releases"}]}',
    )
    samples = sample_drawers(FakeCollection(_rows()), "w", n=4)
    rs = propose_rooms("w", samples, provider)
    assert len(provider.calls) == 2
    assert sum(len(r.exemplars) for r in rs.rooms) == len(samples)


def test_propose_rooms_asks_again_when_assignments_are_missing():
    provider = FakeProvider(
        '{"rooms": [{"name": "releases", "description": "d"}, {"name": "bug-fixes", "description": "d"}]}',
        follow_up='{"assignments": [{"excerpt": 1, "room": "bug-fixes"}, {"excerpt": 2, "room": "releases"}]}',
    )
    samples = sample_drawers(FakeCollection(_rows()), "w", n=2)
    rs = propose_rooms("w", samples, provider)
    assert len(provider.calls) == 2
    system, user = provider.calls[1]
    assert "never invent a room" in system
    assert "releases: d" in user and "excerpt 2" in user
    assert rs.rooms[0].exemplars == [samples[1]["id"]]
    assert rs.rooms[1].exemplars == [samples[0]["id"]]
    assert rs.names() == ["releases", "bug-fixes"]
    assert rs.proposed_by == "fake/m"
    assert rs.sample_size == 2
    system, user = provider.calls[0]
    assert "CLOSED set" in system
    assert "Wing: w" in user and "excerpt 1" in user


def test_propose_rooms_snaps_names_to_existing_spellings():
    from mempalace.rooms import existing_rooms, snap_to_existing

    provider = FakeProvider(
        '{"rooms": [{"name": "Release 3-6-0", "description": "d"}, {"name": "bug-fixes", "description": "d"}]}',
        follow_up='{"assignments": []}',
    )
    samples = sample_drawers(FakeCollection(_rows()), "w", n=1)
    rs = propose_rooms("w", samples, provider, existing=["release-3.6.0", "decisions"])
    assert rs.names() == ["release-3.6.0", "bug-fixes"]
    assert snap_to_existing(rs, ["Bug Fixes"]) == [("bug-fixes", "Bug Fixes")]
    assert existing_rooms(FakeCollection(_rows()), "w") == {
        "general": 2,
        "releases": 1,
        "technical": 2,
    }


def test_snap_to_existing_never_collapses_two_rooms_onto_one_name():
    from mempalace.rooms import RoomSet, RoomSpec, snap_to_existing

    rs = RoomSet(
        wing="w",
        rooms=[RoomSpec("release-3-6-0", "a"), RoomSpec("release_3_6_0", "b"), RoomSpec("x", "c")],
    )
    renames = snap_to_existing(rs, ["release-3.6.0"])
    assert renames == [("release-3-6-0", "release-3.6.0")]
    assert rs.names() == ["release-3.6.0", "release_3_6_0", "x"]
    RoomSet.from_dict(rs.to_dict())  # still a valid, unique set


def test_propose_rooms_rejects_empty_or_garbage():
    with pytest.raises(ValueError):
        propose_rooms("w", [], FakeProvider("{}"))
    with pytest.raises(ValueError):
        propose_rooms(
            "w", [{"id": "r1", "room": "", "excerpt": "x"}], FakeProvider("not json at all")
        )


# ── plan + apply ──


def _room_set():
    return RoomSet(
        wing="w",
        rooms=[
            RoomSpec("releases", "release work"),
            RoomSpec("bug-fixes", "bug reports and fixes"),
        ],
    )


def test_plan_rooms_only_touches_generic_rooms_by_default():
    rows = _rows()
    rows[0]["meta"]["room"] = "decisions"  # r1 filed by hand: must stay put
    col = FakeCollection(rows)
    plan = plan_rooms(col, "w", EmbeddingRoomDecider(_room_set(), FakeEmbed()), threshold=0.75)
    assert sorted(plan.changes) == [("r3", "technical", "bug-fixes")]
    assert plan.per_room["decisions"] == 1
    everything = plan_rooms(
        col, "w", EmbeddingRoomDecider(_room_set(), FakeEmbed()), threshold=0.75, from_rooms=None
    )
    assert ("r1", "decisions", "releases") in everything.changes


def test_plan_rooms_moves_kept_threshold_and_missing():
    col = FakeCollection(_rows())
    decider = EmbeddingRoomDecider(_room_set(), FakeEmbed())
    plan = plan_rooms(col, "w", decider, threshold=0.75, from_rooms=None)
    assert plan.total == 5
    # r1 → releases (moves), r2 already releases (kept), r3 → bug-fixes (moves),
    # r4 similarity 0.707 < 0.75 (kept in general), r5 has no vector (kept).
    assert sorted(plan.changes) == [
        ("r1", "technical", "releases"),
        ("r3", "technical", "bug-fixes"),
    ]
    assert plan.kept == 1
    assert plan.below_threshold == 1
    assert plan.no_embedding == 1
    assert plan.per_room == {"bug-fixes": 1, "general": 2, "releases": 2}
    assert col.updates == []  # planning writes nothing


def test_plan_examples_and_excerpts():
    col = FakeCollection(_rows())
    plan = plan_rooms(
        col, "w", EmbeddingRoomDecider(_room_set(), FakeEmbed()), threshold=0.75, from_rooms=None
    )
    assert set(plan.examples) == {"releases", "bug-fixes"}
    assert plan.examples["releases"][0][0] == "r1"
    excerpts = plan.example_excerpts(col, per_room=1)
    assert excerpts["releases"] == ["[1.00] cut the release"]
    assert excerpts["bug-fixes"] == ["[1.00] fix the bug"]


def test_apply_plan_updates_room_and_last_modified_only():
    col = FakeCollection(_rows())
    plan = plan_rooms(
        col, "w", EmbeddingRoomDecider(_room_set(), FakeEmbed()), threshold=0.75, from_rooms=None
    )
    assert apply_plan(col, plan) == 2
    assert col.rows["r1"]["meta"]["room"] == "releases"
    assert col.rows["r3"]["meta"]["room"] == "bug-fixes"
    assert col.rows["r1"]["doc"] == "cut the release"
    assert set(col.updates[0][1][0]) == {"room", "last_modified"}
    assert col.rows["r4"]["meta"]["room"] == "general"


def test_embedding_decider_uses_exemplar_centroids_when_collection_given():
    col = FakeCollection(_rows())
    rs = RoomSet(
        wing="w",
        rooms=[
            RoomSpec("releases", "unrelated words", exemplars=["r1", "r2"]),
            RoomSpec("bug-fixes", "bug reports"),  # no exemplars → description embedding
        ],
    )
    decider = EmbeddingRoomDecider(rs, FakeEmbed(), col=col)
    assert decider.prototype_source == {"releases": "centroid of 2", "bug-fixes": "description"}
    assert decider.prototypes[0] == pytest.approx([0.95, 0.05])
    assert decider.prototypes[1] == [0.0, 1.0]
    assert (
        decider.decide([{"id": "q", "metadata": {}, "embedding": [1.0, 0.0]}])[0][0] == "releases"
    )


def test_embedding_decider_prefers_embed_query():
    class QueryEmbed(FakeEmbed):
        def __init__(self):
            self.query_calls = 0

        def embed_query(self, texts):
            self.query_calls += 1
            return self(texts)

    embed = QueryEmbed()
    EmbeddingRoomDecider(_room_set(), embed)
    assert embed.query_calls == 1


# ── CLI ──


def test_cmd_rooms_apply_dry_run_then_yes(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli

    cfg = MempalaceConfig(palace_path=str(tmp_path))
    save_room_set(cfg, _room_set())
    col = FakeCollection(_rows())
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: col)
    monkeypatch.setattr("mempalace.embedding.get_embedding_function", lambda: FakeEmbed())
    import contextlib

    monkeypatch.setattr("mempalace.palace.mine_palace_lock", lambda p: contextlib.nullcontext())

    cli.cmd_rooms(
        Namespace(rooms_action="apply", palace=str(tmp_path), wing="w", threshold=0.75, yes=False)
    )
    out = capsys.readouterr().out
    assert "2 would move" in out and "Dry run" in out
    assert col.updates == []

    cli.cmd_rooms(
        Namespace(rooms_action="apply", palace=str(tmp_path), wing="w", threshold=0.75, yes=True)
    )
    out = capsys.readouterr().out
    assert "Moved 2 drawers" in out
    assert col.rows["r1"]["meta"]["room"] == "releases"


def test_cmd_rooms_apply_without_room_set_exits_1(tmp_path, capsys):
    import mempalace.cli as cli

    with pytest.raises(SystemExit) as exc:
        cli.cmd_rooms(
            Namespace(
                rooms_action="apply", palace=str(tmp_path), wing="w", threshold=None, yes=False
            )
        )
    assert exc.value.code == 1
    assert "rooms propose" in capsys.readouterr().out


def test_cmd_rooms_propose_saves_file(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli

    col = FakeCollection(_rows())
    monkeypatch.setattr("mempalace.palace.get_collection", lambda *a, **k: col)
    provider = FakeProvider('{"rooms": [{"name": "releases", "description": "d", "keywords": []}]}')
    seen = {}

    def fake_get_provider(**kw):
        seen.update(kw)
        return provider

    monkeypatch.setattr(cli, "get_provider", fake_get_provider)
    cli.cmd_rooms(
        Namespace(
            rooms_action="propose",
            palace=str(tmp_path),
            wing="w",
            sample=3,
            seed=0,
            max_rooms=8,
            llm_provider="ollama",
            llm_model="m",
            llm_endpoint=None,
            llm_api_key=None,
            accept_external_llm=False,
        )
    )
    out = capsys.readouterr().out
    assert "Proposed 1 rooms" in out
    data = json.loads((tmp_path / "rooms" / "w.json").read_text())
    assert data["rooms"][0]["name"] == "releases"
    assert data["sample_size"] == 3
    assert seen["timeout"] == 600  # Namespace omits llm_timeout → default


def test_cmd_rooms_propose_refuses_external_without_consent(tmp_path, monkeypatch, capsys):
    import mempalace.cli as cli

    class External(FakeProvider):
        endpoint = "https://api.example.com"

        @property
        def is_external_service(self):
            return True

    monkeypatch.setattr(cli, "get_provider", lambda **kw: External("{}"))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_rooms(
            Namespace(
                rooms_action="propose",
                palace=str(tmp_path),
                wing="w",
                sample=3,
                seed=0,
                max_rooms=8,
                llm_provider="openai-compat",
                llm_model="m",
                llm_endpoint="https://api.example.com",
                llm_api_key="k",
                accept_external_llm=False,
            )
        )
    assert exc.value.code == 1
    assert "EXTERNAL" in capsys.readouterr().out


def test_main_dispatches_rooms(monkeypatch):
    import mempalace.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_rooms", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(
        "sys.argv", ["mempalace", "rooms", "apply", "--wing", "w", "--threshold", "0.4", "--yes"]
    )
    cli.main()
    assert (
        seen["rooms_action"] == "apply"
        and seen["wing"] == "w"
        and seen["threshold"] == 0.4
        and seen["yes"]
    )
