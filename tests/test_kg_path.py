"""Which knowledge graph file each entry point opens.

One rule, in ``mempalace.paths.knowledge_graph_path``: the palace's own graph
when it holds facts; otherwise the graph the palace used before, when that one
holds facts, so facts never split; otherwise the palace's own path. Before, the
legacy ``~/.mempalace/knowledge_graph.sqlite3`` served the default palace and
every palace selected without ``--palace``. Graphs are never merged.
"""

import argparse
import logging
import os

import pytest

from mempalace import knowledge_graph, paths

FILENAME = "knowledge_graph.sqlite3"


class _Opened(Exception):
    pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    os.makedirs(paths.default_palace_path())
    return home


@pytest.fixture
def mcp(monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_kg_by_path", {})
    monkeypatch.setattr(mcp_server, "_palace_flag_given", False)
    yield mcp_server
    for kg in mcp_server._kg_by_path.values():
        kg.close()


def _graph(path, state):
    """Give ``path`` a graph: ``"full"`` holds a fact, ``"empty"`` none, ``None`` no file."""
    if state is None:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kg = knowledge_graph.KnowledgeGraph(db_path=path)
    if state == "full":
        kg.add_triple("alice", "likes", "tea")
    kg.close()


def _triples(path):
    kg = knowledge_graph.KnowledgeGraph(db_path=path)
    try:
        return kg.stats()["triples"]
    finally:
        kg.close()


def _record_opens(monkeypatch):
    opened = []

    def fake(db_path=None, **_kwargs):
        opened.append(db_path)
        raise _Opened

    monkeypatch.setattr(knowledge_graph, "KnowledgeGraph", fake)
    return opened


def _resolver(palace, explicit, monkeypatch):
    return paths.knowledge_graph_path(palace, explicit=explicit)


def _mcp(palace, explicit, monkeypatch):
    from mempalace import mcp_server

    monkeypatch.setattr(mcp_server, "_palace_flag_given", explicit)
    return mcp_server._resolve_kg_path()


def _fact_checker(palace, explicit, monkeypatch):
    from mempalace import fact_checker

    opened = _record_opens(monkeypatch)
    fact_checker.check_text("Bob is Alice's brother", palace if explicit else None)
    return opened[0] if opened else None


def _audit(palace, explicit, monkeypatch):
    from mempalace import palace_audit

    opened = []

    def read_kg(path):
        opened.append(path)
        raise _Opened

    monkeypatch.setattr(palace_audit, "_read_wing_room_counts", lambda config: {})
    monkeypatch.setattr(palace_audit, "_load_tunnels", lambda config: [])
    monkeypatch.setattr(palace_audit, "list_hallways", lambda config: [])
    monkeypatch.setattr(palace_audit, "_read_kg", read_kg)
    with pytest.raises(_Opened):
        palace_audit.audit_palace(palace_path=palace, explicit_palace=explicit)
    return opened[0]


def _kg_normalize(palace, explicit, monkeypatch):
    import mempalace.cli as cli

    opened = _record_opens(monkeypatch)
    args = argparse.Namespace(
        kg_action="normalize", palace=palace if explicit else None, vocabulary=None, yes=False
    )
    try:
        cli.cmd_kg(args)
    except _Opened:
        return opened[0]
    except SystemExit:
        return None


ENTRY_POINTS = {
    "resolver": _resolver,
    "mcp": _mcp,
    "fact_checker": _fact_checker,
    "audit": _audit,
    "kg_normalize": _kg_normalize,
}
# These only read: with no file to read they open nothing.
READERS = {"fact_checker", "kg_normalize"}

# palace, how it was selected, local graph, global graph -> graph used, notice
CASES = {
    "palace_local_only": ("default", "configured", "full", None, "local", False),
    "legacy_only": ("default", "configured", None, "full", "global", False),
    "both": ("default", "configured", "full", "full", "local", True),
    "neither": ("default", "configured", None, None, "local", False),
    "empty_local_shadow": ("default", "configured", "empty", "full", "global", False),
    "empty_global": ("custom", "configured", "full", "empty", "local", False),
    "env_palace_global": ("custom", "configured", None, "full", "global", False),
    "env_palace_both": ("custom", "configured", "full", "full", "local", True),
    "flag_palace_global": ("custom", "flag", None, "full", "local", False),
}


def _setup(case, tmp_path, monkeypatch):
    which, selected, local_state, global_state, expected, notice = CASES[case]
    palace = paths.default_palace_path() if which == "default" else str(tmp_path / "custom")
    os.makedirs(palace, exist_ok=True)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
    files = {"local": os.path.join(palace, FILENAME), "global": paths.legacy_knowledge_graph_path()}
    _graph(files["local"], local_state)
    _graph(files["global"], global_state)
    return palace, selected == "flag", files, files[expected], notice


@pytest.mark.parametrize("entry", sorted(ENTRY_POINTS))
@pytest.mark.parametrize("case", sorted(CASES))
def test_each_entry_point_opens_the_palace_graph(case, entry, home, tmp_path, monkeypatch):
    palace, explicit, files, expected, _ = _setup(case, tmp_path, monkeypatch)

    opened = ENTRY_POINTS[entry](palace, explicit, monkeypatch)

    if entry in READERS and not os.path.isfile(expected):
        assert opened is None
        assert not os.path.exists(files["local"])
    else:
        assert paths.same_path(opened, expected), (opened, expected)


@pytest.mark.parametrize("case", sorted(CASES))
def test_notice_only_for_an_unused_graph_that_holds_facts(case, home, tmp_path, monkeypatch):
    palace, explicit, files, _, expected = _setup(case, tmp_path, monkeypatch)

    notice = knowledge_graph.knowledge_graph_notice(palace, explicit=explicit)

    if expected:
        assert files["global"] in notice
        assert f"mempalace kg import-legacy --from {files['global']}" in notice
    else:
        assert notice is None


def test_mcp_keeps_reading_and_writing_the_global_graph_of_an_env_palace(
    home, tmp_path, monkeypatch, mcp
):
    """A palace set through MEMPALACE_PALACE_PATH used the global graph; its facts stay whole."""
    palace = str(tmp_path / "custom")
    os.makedirs(palace)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
    _graph(paths.legacy_knowledge_graph_path(), "full")

    assert mcp.tool_kg_query("alice")["count"] == 1
    assert mcp.tool_kg_add("alice", "drinks", "coffee")["success"] is True
    assert "notice" not in mcp.tool_kg_stats()
    for kg in mcp._kg_by_path.values():
        kg.close()
    mcp._kg_by_path.clear()

    assert _triples(paths.legacy_knowledge_graph_path()) == 2
    assert not os.path.exists(os.path.join(palace, FILENAME))


def test_mcp_reads_never_create_a_graph(home, tmp_path, monkeypatch, mcp):
    palace = str(tmp_path / "custom")
    os.makedirs(palace)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)

    assert mcp.tool_kg_query("alice")["count"] == 0
    assert mcp.tool_kg_timeline()["count"] == 0
    assert mcp.tool_kg_stats()["triples"] == 0

    assert not os.path.exists(os.path.join(palace, FILENAME))
    assert not os.path.exists(paths.legacy_knowledge_graph_path())


def test_mcp_puts_the_notice_in_kg_responses_and_logs_it_once(
    home, tmp_path, monkeypatch, mcp, caplog
):
    palace = str(tmp_path / "custom")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
    _graph(os.path.join(palace, FILENAME), "full")
    _graph(paths.legacy_knowledge_graph_path(), "full")

    with caplog.at_level(logging.WARNING, logger="mempalace"):
        stats = mcp.tool_kg_stats()
        query = mcp.tool_kg_query("alice")

    assert "kg import-legacy" in stats["notice"]
    assert query["notice"] == stats["notice"]
    assert len([r for r in caplog.records if "kg import-legacy" in r.getMessage()]) == 1


def test_mcp_kg_responses_have_no_notice_beside_an_empty_global_graph(
    home, tmp_path, monkeypatch, mcp
):
    palace = str(tmp_path / "custom")
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
    _graph(os.path.join(palace, FILENAME), "full")
    _graph(paths.legacy_knowledge_graph_path(), "empty")

    assert "notice" not in mcp.tool_kg_stats()


def test_fact_checker_never_creates_a_graph(home, tmp_path):
    from mempalace import fact_checker

    palace = str(tmp_path / "custom")
    os.makedirs(palace)

    assert fact_checker.check_text("Bob is Alice's brother", palace) == []
    assert os.listdir(palace) == []


@pytest.mark.parametrize("typed_palace, found", [(False, True), (True, False)])
def test_fact_checker_cli_treats_only_a_typed_palace_as_explicit(
    typed_palace, found, home, tmp_path
):
    """Without --palace the configured palace reads the global graph that holds its facts."""
    import subprocess
    import sys

    palace = str(tmp_path / "custom")
    os.makedirs(palace)
    kg = knowledge_graph.KnowledgeGraph(db_path=paths.legacy_knowledge_graph_path())
    kg.add_triple("Bob", "husband", "Alice")
    kg.close()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), MEMPALACE_PALACE_PATH=palace)
    argv = [sys.executable, "-m", "mempalace.fact_checker", "Bob is Alice's brother"]
    if typed_palace:
        argv += ["--palace", palace]

    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, env=env)

    assert proc.returncode == (1 if found else 0), proc.stderr
    assert ("relationship_mismatch" in proc.stdout) is found
    assert os.listdir(palace) == []


def test_hermes_keeps_using_the_graph_beside_its_palace(tmp_path):
    from mempalace.integrations.hermes import MempalaceProvider

    provider = object.__new__(MempalaceProvider)
    provider._palace_path = str(tmp_path / "hermes" / "palace")
    os.makedirs(provider._palace_path)
    local = os.path.join(provider._palace_path, FILENAME)
    sibling = str(tmp_path / "hermes" / FILENAME)

    assert provider._tool_kg_stats()["triples"] == 0
    assert not os.path.exists(local) and not os.path.exists(sibling)
    _graph(sibling, "full")
    _graph(local, "empty")
    assert paths.same_path(provider._kg_db_path(), sibling)
    assert provider._tool_kg_stats()["triples"] == 1
    _graph(local, "full")
    assert paths.same_path(provider._kg_db_path(), local)
    assert sibling in knowledge_graph.knowledge_graph_notice(provider._palace_path, legacy=sibling)


@pytest.mark.parametrize("global_state, noted", [("full", True), ("empty", False)])
def test_status_prints_the_notice(global_state, noted, home, monkeypatch, capsys):
    import mempalace.cli as cli

    palace = paths.default_palace_path()
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", palace)
    _graph(os.path.join(palace, FILENAME), "full")
    _graph(paths.legacy_knowledge_graph_path(), global_state)
    monkeypatch.setattr("mempalace.miner.status", lambda palace_path: None)

    cli.cmd_status(argparse.Namespace(palace=None))

    assert ("mempalace kg import-legacy" in capsys.readouterr().out) is noted


def _import(palace, yes, source=None):
    import mempalace.cli as cli

    cli.cmd_kg(argparse.Namespace(kg_action="import-legacy", palace=palace, source=source, yes=yes))


def test_import_legacy_dry_run_writes_nothing_then_copies_on_yes(home, monkeypatch, capsys):
    import mempalace.cli as cli

    palace = paths.default_palace_path()
    legacy = paths.legacy_knowledge_graph_path()
    local = os.path.join(palace, FILENAME)
    _graph(legacy, "full")

    def no_lock(_palace):
        raise AssertionError("a dry run only reads")

    with monkeypatch.context() as m:
        m.setattr(cli, "_repair_lock", no_lock)
        _import(palace, yes=False)
    assert "Would copy 1 triples" in capsys.readouterr().out
    assert not os.path.exists(local)
    assert paths.same_path(paths.knowledge_graph_path(palace), legacy)

    _import(palace, yes=True)
    assert "Copied 1 triples" in capsys.readouterr().out
    assert paths.same_path(paths.knowledge_graph_path(palace), local)
    assert _triples(local) == 1

    _import(palace, yes=True)
    out = capsys.readouterr().out
    assert "Copied 0 triples" in out and "1 already in" in out


def test_import_matches_facts_not_ids_and_lists_conflicts(home, tmp_path, capsys):
    source = str(tmp_path / "old.sqlite3")
    palace = str(tmp_path / "custom")
    target = os.path.join(palace, FILENAME)
    kg = knowledge_graph.KnowledgeGraph(db_path=source)
    kg.add_triple("alice", "likes", "tea", valid_from="2024-01-01")
    kg.add_triple("alice", "works_at", "acme")
    kg.add_triple("alice", "lives_in", "paris", valid_from="2020-01-01")
    kg.invalidate("alice", "lives_in", "paris", ended="2023-01-01")
    kg.close()
    kg = knowledge_graph.KnowledgeGraph(db_path=target)
    kg.add_triple("alice", "likes", "tea", valid_from="2024-01-01")  # recorded again later
    kg.add_triple("alice", "works_at", "globex")
    kg.add_triple("alice", "lives_in", "berlin", valid_from="2023-01-01")
    kg.close()

    counts = knowledge_graph.import_graph_rows(source, target, apply=True)

    assert counts["triples"]["present"] == 1
    assert counts["triples"]["added"] == 1  # the ended Paris fact
    assert counts["triples"]["conflicts"] == [
        {"subject": "alice", "predicate": "works_at", "object": "acme", "current_object": "globex"}
    ]
    kg = knowledge_graph.KnowledgeGraph(db_path=target)
    try:
        current = {(f["predicate"], f["object"]) for f in kg.query_entity("alice") if f["current"]}
    finally:
        kg.close()
    assert current == {("likes", "tea"), ("works_at", "globex"), ("lives_in", "berlin")}

    _import(palace, yes=False, source=source)
    out = capsys.readouterr().out
    assert "1 current fact(s) not copied" in out
    assert "alice | works_at | acme (here: globex)" in out


def test_import_legacy_refuses_a_missing_source_or_the_palace_own_graph(home, tmp_path):
    palace = str(tmp_path / "custom")
    os.makedirs(palace)
    with pytest.raises(SystemExit):
        _import(palace, yes=True)
    _graph(os.path.join(palace, FILENAME), "empty")
    with pytest.raises(SystemExit):
        _import(palace, yes=True, source=os.path.join(palace, FILENAME))
