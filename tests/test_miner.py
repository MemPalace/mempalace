import os
import shlex
import shutil
import tempfile
from pathlib import Path

import chromadb
import yaml

from mempalace.miner import load_config, mine, scan_project, status
from mempalace.palace import NORMALIZE_VERSION, file_already_mined


def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def scanned_files(project_root: Path, **kwargs):
    files = scan_project(str(project_root), **kwargs)
    return sorted(path.relative_to(project_root).as_posix() for path in files)


def test_project_mining():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()
        os.makedirs(project_root / "backend")

        write_file(
            project_root / "backend" / "app.py",
            "def main():\n    print('hello world')\n" * 20,
        )
        with open(project_root / "mempalace.yaml", "w") as f:
            yaml.dump(
                {
                    "wing": "test_project",
                    "rooms": [
                        {"name": "backend", "description": "Backend code"},
                        {"name": "general", "description": "General"},
                    ],
                },
                f,
            )

        palace_path = project_root / "palace"
        mine(str(project_root), str(palace_path))

        client = chromadb.PersistentClient(path=str(palace_path))
        col = client.get_collection("mempalace_drawers")
        assert col.count() > 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_load_config_uses_defaults_when_yaml_missing():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()
        config = load_config(str(project_root))

        assert isinstance(config, dict)
        assert "wing" in config
        assert "rooms" in config
        assert config["wing"] == project_root.name
    finally:
        shutil.rmtree(tmpdir)


def test_load_config_no_yaml_normalizes_hyphenated_wing():
    """Fallback wing name is normalized so it matches topics_by_wing keys.

    Regression for the no-yaml branch of #1194: ``cmd_init`` writes
    ``topics_by_wing`` under the normalized slug, so the miner's
    fallback wing must use the same normalization or the tunnel lookup
    misses every key for hyphenated dirnames.
    """
    parent = tempfile.mkdtemp()
    try:
        project_root = Path(parent) / "my-cool-app"
        project_root.mkdir()
        config = load_config(str(project_root))
        assert config["wing"] == "my_cool_app"
    finally:
        shutil.rmtree(parent)


def test_scan_project_skips_mempalace_generated_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        project_root = Path(tmpdir).resolve()
        write_file(project_root / "entities.json", '{"people": [], "projects": []}')
        write_file(project_root / "mempalace.yaml", "wing: test\nrooms: []\n")
        write_file(project_root / "notes.md", "real user content\n" * 10)

        assert scanned_files(project_root) == ["notes.md"]


def test_scan_project_respects_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "ignored.py\ngenerated/\n")
        write_file(project_root / "src" / "app.py", "print('hello')\n" * 20)
        write_file(project_root / "ignored.py", "print('ignore me')\n" * 20)
        write_file(project_root / "generated" / "artifact.py", "print('artifact')\n" * 20)

        assert scanned_files(project_root) == ["src/app.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_respects_nested_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "*.log\n")
        write_file(project_root / "subrepo" / ".gitignore", "tasks/\n")
        write_file(project_root / "subrepo" / "src" / "main.py", "print('main')\n" * 20)
        write_file(project_root / "subrepo" / "tasks" / "task.py", "print('task')\n" * 20)
        write_file(project_root / "subrepo" / "debug.log", "debug\n" * 20)

        assert scanned_files(project_root) == ["subrepo/src/main.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_allows_nested_gitignore_override():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "*.csv\n")
        write_file(project_root / "subrepo" / ".gitignore", "!keep.csv\n")
        write_file(project_root / "drop.csv", "a,b,c\n" * 20)
        write_file(project_root / "subrepo" / "keep.csv", "a,b,c\n" * 20)

        assert scanned_files(project_root) == ["subrepo/keep.csv"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_allows_gitignore_negation_when_parent_dir_is_visible():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/*\n!generated/keep.py\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root) == ["generated/keep.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_does_not_reinclude_file_from_ignored_directory():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/\n!generated/keep.py\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root) == []
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_disable_gitignore():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "data/\n")
        write_file(project_root / "data" / "stuff.csv", "a,b,c\n" * 20)

        assert scanned_files(project_root, respect_gitignore=False) == ["data/stuff.csv"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_ignored_directory():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "docs/\n")
        write_file(project_root / "docs" / "guide.md", "# Guide\n" * 20)

        assert scanned_files(project_root, include_ignored=["docs"]) == ["docs/guide.md"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_specific_ignored_file():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "generated/\n")
        write_file(project_root / "generated" / "drop.py", "print('drop')\n" * 20)
        write_file(project_root / "generated" / "keep.py", "print('keep')\n" * 20)

        assert scanned_files(project_root, include_ignored=["generated/keep.py"]) == [
            "generated/keep.py"
        ]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_can_include_exact_file_without_known_extension():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".gitignore", "README\n")
        write_file(project_root / "README", "hello\n" * 20)

        assert scanned_files(project_root, include_ignored=["README"]) == ["README"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_include_override_beats_skip_dirs():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".pytest_cache" / "cache.py", "print('cache')\n" * 20)

        assert scanned_files(
            project_root,
            respect_gitignore=False,
            include_ignored=[".pytest_cache"],
        ) == [".pytest_cache/cache.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_scan_project_skip_dirs_still_apply_without_override():
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        write_file(project_root / ".pytest_cache" / "cache.py", "print('cache')\n" * 20)
        write_file(project_root / "main.py", "print('main')\n" * 20)

        assert scanned_files(project_root, respect_gitignore=False) == ["main.py"]
    finally:
        shutil.rmtree(tmpdir)


def test_entity_metadata_finds_cyrillic_names(monkeypatch):
    """Entity extraction must find non-Latin names when entity_languages includes the locale."""
    import mempalace.palace as palace_mod
    from mempalace.miner import _extract_entities_for_metadata

    # Reset cached patterns so they reload with the monkeypatched languages
    monkeypatch.setattr(palace_mod, "_CANDIDATE_RX_CACHE", None)
    monkeypatch.setattr(
        "mempalace.config.MempalaceConfig.entity_languages",
        property(lambda self: ("en", "ru")),
    )

    content = "Михаил написал код. Михаил отправил PR. Михаил получил ревью."
    result = _extract_entities_for_metadata(content)
    assert "Михаил" in result, f"Cyrillic name not found in entity metadata: {result!r}"


def test_file_already_mined_check_mtime():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        os.makedirs(palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )

        test_file = os.path.join(tmpdir, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        mtime = os.path.getmtime(test_file)

        # Not mined yet
        assert file_already_mined(col, test_file) is False
        assert file_already_mined(col, test_file, check_mtime=True) is False

        # Add it with mtime + current normalize_version
        col.add(
            ids=["d1"],
            documents=["hello world"],
            metadatas=[
                {
                    "source_file": test_file,
                    "source_mtime": str(mtime),
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )

        # Already mined (no mtime check)
        assert file_already_mined(col, test_file) is True
        # Already mined (mtime matches)
        assert file_already_mined(col, test_file, check_mtime=True) is True

        # Modify file and force a different mtime (Windows has low mtime resolution)
        with open(test_file, "w") as f:
            f.write("modified content")
        os.utime(test_file, (mtime + 10, mtime + 10))

        # Still mined without mtime check
        assert file_already_mined(col, test_file) is True
        # Needs re-mining with mtime check
        assert file_already_mined(col, test_file, check_mtime=True) is False

        # Record with no mtime stored should return False for check_mtime
        col.add(
            ids=["d2"],
            documents=["other"],
            metadatas=[
                {
                    "source_file": "/fake/no_mtime.txt",
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )
        assert file_already_mined(col, "/fake/no_mtime.txt", check_mtime=True) is False
    finally:
        # Release ChromaDB file handles before cleanup (required on Windows)
        del col, client
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_dry_run_with_tiny_file_no_crash():
    """Dry-run must not crash when process_file returns 0 drawers (room was None)."""
    tmpdir = tempfile.mkdtemp()
    try:
        project_root = Path(tmpdir).resolve()

        # One normal file and one that falls below MIN_CHUNK_SIZE
        write_file(project_root / "good.py", "def main():\n    print('hello world')\n" * 20)
        write_file(project_root / "tiny.txt", "x")

        with open(project_root / "mempalace.yaml", "w") as f:
            yaml.dump(
                {
                    "wing": "test_project",
                    "rooms": [{"name": "general", "description": "General"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        # Should not raise TypeError on the summary print
        mine(str(project_root), str(palace_path), dry_run=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_status_missing_palace_does_not_create_empty_collection(tmp_path, capsys):
    palace_path = tmp_path / "missing-palace"

    status(str(palace_path))

    out = capsys.readouterr().out
    assert "No palace found" in out
    assert not palace_path.exists()


def test_status_handles_none_metadata_without_crash(tmp_path, capsys):
    """status must not crash when col.get returns a None entry in metadatas.

    Palaces can contain drawers whose metadata was never set (older mining
    paths, drawers written by third-party tools). Before the guard, status
    crashed mid-tally with ``AttributeError: 'NoneType' object has no
    attribute 'get'`` at the wing/room histogram line."""
    from unittest.mock import patch

    class FakeCol:
        def count(self):
            return 2

        def get(self, *args, **kwargs):
            return {
                "ids": ["a", "b"],
                "documents": ["doc a", "doc b"],
                "metadatas": [{"wing": "proj", "room": "r"}, None],
            }

    with patch("mempalace.miner.get_collection", return_value=FakeCol()):
        status(str(tmp_path))

    out = capsys.readouterr().out
    # No crash; the None-metadata row is counted under the ?/? fallback
    # alongside the real wing=proj row.
    assert "WING: ?" in out
    assert "WING: proj" in out


def test_process_file_uses_bounded_upsert_batches(tmp_path, monkeypatch):
    from mempalace import miner

    class FakeCol:
        def __init__(self):
            self.batch_sizes = []

        def get(self, *args, **kwargs):
            return {"ids": []}

        def delete(self, *args, **kwargs):
            pass

        def upsert(self, documents, ids, metadatas):
            self.batch_sizes.append(len(documents))

    source = tmp_path / "src.py"
    source.write_text("print('hello')\n" * 20, encoding="utf-8")
    chunks = [{"content": f"chunk {i} " * 20, "chunk_index": i} for i in range(5)]
    col = FakeCol()
    monkeypatch.setattr(miner, "DRAWER_UPSERT_BATCH_SIZE", 2)
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file: chunks)
    monkeypatch.setattr(miner, "detect_hall", lambda content: "code")
    monkeypatch.setattr(miner, "_extract_entities_for_metadata", lambda content: "")

    drawers, room = miner.process_file(
        source,
        tmp_path,
        col,
        "wing",
        [{"name": "general", "description": "General"}],
        "agent",
        False,
    )

    assert drawers == 5
    assert room == "general"
    assert col.batch_sizes == [2, 2, 1]


# ── normalize_version schema gate ───────────────────────────────────────
#
# When the normalization pipeline changes shape (e.g., strip_noise lands),
# `NORMALIZE_VERSION` is bumped so pre-existing drawers can be silently
# rebuilt on the next mine. These tests pin that contract.


def test_file_already_mined_returns_false_for_stale_normalize_version():
    """Pre-v2 drawers (no field, or older integer) must not short-circuit."""
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        os.makedirs(palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection("mempalace_drawers")

        # Pre-v2 drawer: no normalize_version field at all
        col.add(
            ids=["d_old"],
            documents=["old"],
            metadatas=[{"source_file": "/fake/old.jsonl"}],
        )
        assert file_already_mined(col, "/fake/old.jsonl") is False

        # Explicitly older version
        col.add(
            ids=["d_v1"],
            documents=["v1"],
            metadatas=[{"source_file": "/fake/v1.jsonl", "normalize_version": 1}],
        )
        assert file_already_mined(col, "/fake/v1.jsonl") is False

        # Current version — short-circuits
        col.add(
            ids=["d_current"],
            documents=["cur"],
            metadatas=[
                {
                    "source_file": "/fake/current.jsonl",
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )
        assert file_already_mined(col, "/fake/current.jsonl") is True
    finally:
        del col, client
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_add_drawer_stamps_normalize_version(tmp_path):
    """Fresh drawers carry the current schema version so future upgrades work."""
    from mempalace.miner import add_drawer

    palace_path = tmp_path / "palace"
    palace_path.mkdir()
    client = chromadb.PersistentClient(path=str(palace_path))
    col = client.get_or_create_collection("mempalace_drawers")
    try:
        added = add_drawer(
            collection=col,
            wing="test",
            room="notes",
            content="hello",
            source_file=str(tmp_path / "src.md"),
            chunk_index=0,
            agent="unit",
        )
        assert added is True
        stored = col.get(limit=1)
        meta = stored["metadatas"][0]
        assert meta["normalize_version"] == NORMALIZE_VERSION
    finally:
        del col, client


def test_mine_creates_topic_tunnels_for_shared_topics(tmp_path, monkeypatch):
    """End-to-end: when two wings have already-confirmed topics that overlap,
    the miner's mine-time pass drops a cross-wing tunnel between them.

    Issue #1180.
    """
    from mempalace import miner, palace_graph

    # Redirect both the registry and tunnel-storage paths into tmp_path
    # so we never touch the developer's real ~/.mempalace directory.
    registry = tmp_path / "known_entities.json"
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})
    tunnels_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_TUNNEL_FILE", str(tunnels_file))

    # Pre-populate the registry as if init had been run for two wings that
    # share a topic.
    miner.add_to_known_entities({"topics": ["foo", "bar"]}, wing="wing_one")
    miner.add_to_known_entities({"topics": ["foo", "baz"]}, wing="wing_two")

    # Mine wing_two — should drop tunnels between wing_two and wing_one
    # for every shared topic. Just one in this case.
    project_root = tmp_path / "wing_two_project"
    project_root.mkdir()
    write_file(
        project_root / "notes.md",
        "Some prose long enough to make a chunk. " * 20,
    )
    with open(project_root / "mempalace.yaml", "w") as f:
        yaml.dump({"wing": "wing_two", "rooms": [{"name": "general"}]}, f)

    palace_path = tmp_path / "palace"
    mine(str(project_root), str(palace_path))

    listed = palace_graph.list_tunnels()
    assert len(listed) == 1
    rooms = {listed[0]["source"]["room"], listed[0]["target"]["room"]}
    # Topic tunnels use a ``topic:<name>`` synthetic room so they can't
    # collide with literal folder-derived rooms of the same name.
    assert rooms == {"topic:foo"}
    assert listed[0]["kind"] == "topic"
    wings = {listed[0]["source"]["wing"], listed[0]["target"]["wing"]}
    assert wings == {"wing_one", "wing_two"}


def test_mine_no_tunnel_when_threshold_blocks_overlap(tmp_path, monkeypatch):
    """Bumping ``MEMPALACE_TOPIC_TUNNEL_MIN_COUNT`` above the actual overlap
    suppresses tunnel creation."""
    from mempalace import miner, palace_graph

    registry = tmp_path / "known_entities.json"
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})
    tunnels_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_TUNNEL_FILE", str(tunnels_file))
    monkeypatch.setenv("MEMPALACE_TOPIC_TUNNEL_MIN_COUNT", "2")

    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_one")
    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_two")

    project_root = tmp_path / "wing_two_project"
    project_root.mkdir()
    write_file(
        project_root / "notes.md",
        "Some prose long enough to make a chunk. " * 20,
    )
    with open(project_root / "mempalace.yaml", "w") as f:
        yaml.dump({"wing": "wing_two", "rooms": [{"name": "general"}]}, f)

    palace_path = tmp_path / "palace"
    mine(str(project_root), str(palace_path))

    # min_count=2 but only 1 shared topic → no tunnel.
    assert palace_graph.list_tunnels() == []


def test_mine_no_tunnel_when_only_one_wing_has_topics(tmp_path, monkeypatch):
    """A wing in isolation (no other wing has confirmed topics) creates no tunnels."""
    from mempalace import miner, palace_graph

    registry = tmp_path / "known_entities.json"
    monkeypatch.setattr(miner, "_ENTITY_REGISTRY_PATH", str(registry))
    miner._ENTITY_REGISTRY_CACHE.update({"mtime": None, "names": frozenset(), "raw": {}})
    tunnels_file = tmp_path / "tunnels.json"
    monkeypatch.setattr(palace_graph, "_TUNNEL_FILE", str(tunnels_file))

    miner.add_to_known_entities({"topics": ["foo"]}, wing="wing_one")

    project_root = tmp_path / "wing_one_project"
    project_root.mkdir()
    write_file(
        project_root / "notes.md",
        "Some prose long enough to make a chunk. " * 20,
    )
    with open(project_root / "mempalace.yaml", "w") as f:
        yaml.dump({"wing": "wing_one", "rooms": [{"name": "general"}]}, f)

    palace_path = tmp_path / "palace"
    mine(str(project_root), str(palace_path))

    assert palace_graph.list_tunnels() == []


# ── graceful Ctrl-C handling (#1182) ────────────────────────────────────


def _make_minable_project(project_root: Path, n_files: int = 3) -> None:
    """Create a tiny project with N readable files + a config so mine() runs."""
    for idx in range(n_files):
        write_file(
            project_root / f"f{idx}.py",
            f"def fn_{idx}():\n    print('hi {idx}')\n" * 20,
        )
    with open(project_root / "mempalace.yaml", "w") as f:
        yaml.dump(
            {
                "wing": "interrupt_test",
                "rooms": [{"name": "general", "description": "General"}],
            },
            f,
        )


def test_mine_keyboard_interrupt_prints_summary_and_exits_130(tmp_path, capsys):
    """A KeyboardInterrupt mid-loop produces the clean summary + exit 130."""
    import pytest
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=4)
    palace_path = project_root / "palace"

    call_count = {"n": 0}

    def fake_process_file(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise KeyboardInterrupt
        return (1, "general")

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        with pytest.raises(SystemExit) as exc_info:
            mine(str(project_root), str(palace_path))

    assert exc_info.value.code == 130
    out = capsys.readouterr().out
    assert "Mine interrupted." in out
    assert "files_processed: 1/" in out
    assert "drawers_filed:" in out
    assert "last_file:" in out
    assert "upserted idempotently" in out


def test_mine_keyboard_interrupt_quotes_path_with_spaces_in_resume_hint(tmp_path, capsys):
    """Resume hint must shell-quote the project dir so a path containing
    spaces / metacharacters yields a copy-paste-safe `mempalace mine ...`
    command. Otherwise users on a path like "My Project" hit a broken
    invocation when they re-run after Ctrl-C."""
    import pytest
    from unittest.mock import patch

    project_root = tmp_path / "my project"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=2)
    palace_path = project_root / "palace"

    def fake_process_file(*args, **kwargs):
        raise KeyboardInterrupt

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        with pytest.raises(SystemExit):
            mine(str(project_root), str(palace_path))

    out = capsys.readouterr().out
    # Use shlex.quote so the assertion matches whatever the production
    # code emits on this platform (POSIX paths with spaces vs Windows
    # paths with backslashes both end up wrapped in single quotes).
    assert f"mempalace mine {shlex.quote(str(project_root))}" in out


def test_skip_filenames_includes_lockfiles():
    """pnpm-lock.yaml and yarn.lock must be skipped alongside package-lock.json
    so a Windows mine over a typical JS monorepo doesn't OOM the ONNX embedder
    on a 24K-line lockfile (#1296)."""
    from mempalace import miner

    assert "package-lock.json" in miner.SKIP_FILENAMES
    assert "pnpm-lock.yaml" in miner.SKIP_FILENAMES
    assert "yarn.lock" in miner.SKIP_FILENAMES


def test_process_file_skips_when_chunks_exceed_max(tmp_path, monkeypatch):
    """A file producing more than MAX_CHUNKS_PER_FILE chunks must be skipped
    with a clear message and zero upserts. Generated artifacts (CSVs, lock
    files not in SKIP_FILENAMES) hit this — the cap is what prevents ONNX
    bad_alloc on Windows when the embedder is asked to swallow thousands of
    chunks in one batch (#1296)."""
    from unittest.mock import MagicMock

    from mempalace import miner

    monkeypatch.setattr(miner, "MAX_CHUNKS_PER_FILE", 5)
    over_cap = [{"content": f"chunk {i}", "chunk_index": i} for i in range(7)]
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file: over_cap)

    source = tmp_path / "huge.csv"
    source.write_text("col1,col2\n" + "x,y\n" * 500, encoding="utf-8")
    col = MagicMock()
    col.get.return_value = {"ids": []}

    drawers, room = miner.process_file(
        source,
        tmp_path,
        col,
        "wing",
        [{"name": "general", "description": "General"}],
        "agent",
        False,
    )

    assert drawers == 0
    col.upsert.assert_not_called()


def test_mine_arbitrary_exception_prints_summary_and_reraises(tmp_path, capsys):
    """A non-KeyboardInterrupt exception mid-mine must surface a summary
    banner before propagating, so users don't see a silent exit-0 with no
    completion message (#1296 Failure 2). Re-raise preserves the traceback
    and yields a non-zero exit code."""
    import pytest
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=4)
    palace_path = project_root / "palace"

    call_count = {"n": 0}

    def fake_process_file(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated ONNX bad_alloc")
        return (1, "general")

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        with pytest.raises(RuntimeError, match="simulated ONNX bad_alloc"):
            mine(str(project_root), str(palace_path))

    out = capsys.readouterr().out
    assert "Mine aborted by exception." in out
    assert "files_processed: 1/" in out
    assert "drawers_filed:" in out
    assert "RuntimeError: simulated ONNX bad_alloc" in out
    assert "upserted idempotently" in out


def test_mine_cleans_up_pid_file_on_interrupt(tmp_path):
    """Our own PID entry in mine.pid is removed in the finally clause."""
    import pytest
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=2)
    palace_path = project_root / "palace"

    pid_file = tmp_path / "mine.pid"
    pid_file.write_text(str(os.getpid()))

    def fake_process_file(*args, **kwargs):
        raise KeyboardInterrupt

    with (
        patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file),
        patch("mempalace.miner.process_file", side_effect=fake_process_file),
    ):
        with pytest.raises(SystemExit):
            mine(str(project_root), str(palace_path))

    assert not pid_file.exists(), "Our PID entry should be cleaned up on interrupt"


def test_mine_cleans_up_pid_file_on_clean_exit(tmp_path):
    """Successful mine also removes its own PID entry in the finally clause."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=1)
    palace_path = project_root / "palace"

    pid_file = tmp_path / "mine.pid"
    pid_file.write_text(str(os.getpid()))

    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        mine(str(project_root), str(palace_path))

    assert not pid_file.exists()


def test_mine_does_not_remove_other_processes_pid_file(tmp_path):
    """A PID file pointing at someone else's PID is left untouched."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=1)
    palace_path = project_root / "palace"

    other_pid = os.getpid() + 999_999  # a PID that isn't us
    pid_file = tmp_path / "mine.pid"
    pid_file.write_text(str(other_pid))

    with patch("mempalace.hooks_cli._MINE_PID_FILE", pid_file):
        mine(str(project_root), str(palace_path))

    assert pid_file.exists(), "Foreign PID entries must not be removed"
    assert pid_file.read_text().strip() == str(other_pid)
