import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import chromadb
import pytest
import yaml

from mempalace.miner import detect_room, load_config, mine, scan_project, status
from mempalace.palace import NORMALIZE_VERSION, file_already_mined, prefetch_mined_set


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


def test_mine_computes_hallways_for_wing_post_mine(monkeypatch):
    """After mine() completes (non-dry-run), compute_hallways_for_wing must be
    called once with the wing name and the live collection.

    This is the integration test for the hallway primitive — without this
    call, the hallway module is dead code (no miner triggers it). Mirrors
    the existing tunnel-computation integration pattern at miner.py:1241.
    """

    from mempalace import miner as miner_mod

    hallway_calls = []

    def fake_compute(wing, col=None, min_count=2):
        hallway_calls.append({"wing": wing, "col": col, "min_count": min_count})
        return []  # no hallways materialized — that's not what we're testing

    # Patch at the call site (mempalace.miner.compute_hallways_for_wing) so
    # the integration in mine() routes through our stub.
    monkeypatch.setattr(miner_mod, "compute_hallways_for_wing", fake_compute)

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
                    "rooms": [{"name": "backend", "description": "Backend code"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        mine(str(project_root), str(palace_path))

        # Must have been called exactly once, with our wing name + a live
        # collection. We don't pin min_count — the integration may pick a
        # default that differs from the function's own default.
        assert len(hallway_calls) == 1, (
            f"expected compute_hallways_for_wing to be called once, got {len(hallway_calls)}"
        )
        call = hallway_calls[0]
        assert call["wing"] == "test_project"
        assert call["col"] is not None, (
            "must pass the live collection so hallways can query drawers"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_hallway_failure_does_not_crash_mine(monkeypatch):
    """If compute_hallways_for_wing raises, the mine must still complete.

    Mirrors the try/except wrap around the existing tunnel-computation block
    at miner.py:1244-1249. Hallway computation is a derived analytic, not
    load-bearing for the drawer write itself.
    """
    from mempalace import miner as miner_mod

    def angry_compute(wing, col=None, min_count=2):
        raise RuntimeError("simulated hallway-compute explosion")

    monkeypatch.setattr(miner_mod, "compute_hallways_for_wing", angry_compute)

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
                    "rooms": [{"name": "backend", "description": "Backend code"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        # Must NOT raise — the failure has to be caught + logged but not propagated.
        mine(str(project_root), str(palace_path))

        # Drawer-write side of the mine still committed.
        client = chromadb.PersistentClient(path=str(palace_path))
        col = client.get_collection("mempalace_drawers")
        assert col.count() > 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_computes_entity_tunnels_for_wing_post_mine(monkeypatch):
    """After mine() completes (non-dry-run), ``_compute_entity_tunnels_for_wing``
    must be called once with the wing name from mempalace.yaml.

    Without this call the entity-tunnel feature is dead code — no miner
    triggers it, no entity tunnels ever land in ~/.mempalace/tunnels.json.
    Mirrors the existing hallway- and topic-tunnel integration tests in
    this file.
    """
    from mempalace import miner as miner_mod

    entity_tunnel_calls = []

    def fake_compute(wing):
        entity_tunnel_calls.append({"wing": wing})
        return 0  # no tunnels — that's not what we're testing here

    # Patch at the call site (mempalace.miner._compute_entity_tunnels_for_wing)
    # so the integration in mine() routes through our stub.
    monkeypatch.setattr(miner_mod, "_compute_entity_tunnels_for_wing", fake_compute)

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
                    "rooms": [{"name": "backend", "description": "Backend code"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        mine(str(project_root), str(palace_path))

        assert len(entity_tunnel_calls) == 1, (
            f"expected _compute_entity_tunnels_for_wing to be called once, "
            f"got {len(entity_tunnel_calls)}"
        )
        assert entity_tunnel_calls[0]["wing"] == "test_project"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_mine_entity_tunnel_failure_does_not_crash_mine(monkeypatch):
    """If ``_compute_entity_tunnels_for_wing`` raises, the mine must still
    complete and the drawer write must remain committed.

    Mirrors the try/except wrap around the existing tunnel- and hallway-
    computation blocks. Entity tunnel computation is a derived analytic,
    not load-bearing for the drawer write itself.
    """
    from mempalace import miner as miner_mod

    def angry_compute(wing):
        raise RuntimeError("simulated entity-tunnel-compute explosion")

    monkeypatch.setattr(miner_mod, "_compute_entity_tunnels_for_wing", angry_compute)

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
                    "rooms": [{"name": "backend", "description": "Backend code"}],
                },
                f,
            )

        palace_path = project_root / "palace"
        # Must NOT raise — the failure has to be caught + logged but not propagated.
        mine(str(project_root), str(palace_path))

        # Drawer-write side of the mine still committed.
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires elevated privileges on Windows",
)
def test_scan_project_logs_skipped_symlinks(tmp_path, capsys):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    real_target = tmp_path / "outside" / "real.md"
    write_file(real_target, "real content\n" * 5)
    (project_root / "link.md").symlink_to(real_target)
    write_file(project_root / "regular.md", "regular content\n" * 5)

    files = scanned_files(project_root, respect_gitignore=False)

    assert "link.md" not in files
    assert "regular.md" in files
    err = capsys.readouterr().err
    assert err.count("SKIP:") == 1
    assert "  SKIP:" in err
    assert "link.md" in err
    assert "(symlink)" in err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires elevated privileges on Windows",
)
def test_scan_project_logs_dangling_symlink(tmp_path, capsys):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    real_target = tmp_path / "outside" / "ghost.md"
    real_target.parent.mkdir()
    real_target.touch()
    (project_root / "dangling.md").symlink_to(real_target)
    real_target.unlink()  # target deleted, link dangles

    files = scanned_files(project_root, respect_gitignore=False)

    assert files == []
    err = capsys.readouterr().err
    assert err.count("SKIP:") == 1
    assert "dangling.md" in err
    assert "(symlink)" in err


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="symlink creation requires elevated privileges on Windows",
)
def test_scan_project_logs_nested_symlink_with_relative_path(tmp_path, capsys):
    project_root = tmp_path / "proj"
    project_root.mkdir()
    real_target = tmp_path / "outside" / "real.md"
    write_file(real_target, "real content\n" * 5)
    deep = project_root / "deep" / "subdir"
    deep.mkdir(parents=True)
    (deep / "nested.md").symlink_to(real_target)

    files = scanned_files(project_root, respect_gitignore=False)

    assert files == []
    err = capsys.readouterr().err
    # Forward slash even on Windows (as_posix) and full relative path,
    # not just the leaf — proves relative_to(project_path) over .name.
    assert "deep/subdir/nested.md" in err
    assert "(symlink)" in err


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


def test_entity_metadata_matches_known_names_case_insensitively(monkeypatch):
    """Per-drawer entity tagging must mirror init-time case-insensitive matching.

    The init-time scanner in entity_detector.py already does case-insensitive
    matching against the corpus (line 276: ``name_line_indices = [...if name_lower
    in line.lower()...]``). The per-drawer tagger in miner.py:788 was not
    updated to use the same flag — so an entry like ``"Aya"`` in
    known_entities.json fails to match the lowercase mention ``aya`` that
    appears in chat transcripts, voice-typed notes, and journal-style
    drawers. This test pins down the contract: known-entity names must match
    the content case-insensitively.
    """
    from mempalace import miner

    # Stub the known-entity registry to a controlled set
    monkeypatch.setattr(miner, "_load_known_entities", lambda: frozenset({"Aya", "Lumi"}))

    # Lowercase mentions of seeded names must still be tagged.
    result = miner._extract_entities_for_metadata("aya talked to lumi today about the palace.")
    matched = set(result.split(";")) if result else set()
    assert "Aya" in matched, (
        f"lowercase 'aya' must match the seeded 'Aya' (case-insensitive). Got: {matched!r}"
    )
    assert "Lumi" in matched, (
        f"lowercase 'lumi' must match the seeded 'Lumi' (case-insensitive). Got: {matched!r}"
    )

    # Mixed case must also match
    result_mixed = miner._extract_entities_for_metadata("Aya saw lumi. AYA waved.")
    matched_mixed = set(result_mixed.split(";")) if result_mixed else set()
    assert "Aya" in matched_mixed
    assert "Lumi" in matched_mixed


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


def test_file_already_mined_scopes_convo_extract_mode():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = os.path.join(tmpdir, "palace")
        os.makedirs(palace_path)
        client = chromadb.PersistentClient(path=palace_path)
        col = client.get_or_create_collection(
            "mempalace_drawers", metadata={"hnsw:space": "cosine"}
        )

        source_file = os.path.join(tmpdir, "chat.jsonl")
        col.add(
            ids=["exchange"],
            documents=["exchange drawer"],
            metadatas=[
                {
                    "source_file": source_file,
                    "extract_mode": "exchange",
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )

        assert file_already_mined(col, source_file, extract_mode="exchange") is True
        assert file_already_mined(col, source_file, extract_mode="general") is False
        assert source_file in prefetch_mined_set(col, extract_mode="exchange")
        assert source_file not in prefetch_mined_set(col, extract_mode="general")

        col.add(
            ids=["general"],
            documents=["general drawer"],
            metadatas=[
                {
                    "source_file": source_file,
                    "extract_mode": "general",
                    "normalize_version": NORMALIZE_VERSION,
                }
            ],
        )

        assert file_already_mined(col, source_file, extract_mode="general") is True
        assert source_file in prefetch_mined_set(col, extract_mode="general")
    finally:
        del col, client
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_file_already_mined_extract_mode_paginates_large_sources():
    source_file = "/tmp/long-chat.jsonl"
    metadatas = [
        {
            "source_file": source_file,
            "extract_mode": "exchange",
            "normalize_version": NORMALIZE_VERSION,
        }
        for _ in range(1000)
    ]
    metadatas.append(
        {
            "source_file": source_file,
            "extract_mode": "general",
            "normalize_version": NORMALIZE_VERSION,
        }
    )

    class FakeCollection:
        def get(self, where=None, limit=1, offset=0, include=None):
            batch = metadatas[offset : offset + limit]
            return {
                "ids": [f"id-{i}" for i in range(offset, offset + len(batch))],
                "metadatas": batch,
            }

    assert file_already_mined(FakeCollection(), source_file, extract_mode="general") is True


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


def test_status_initialized_but_empty_palace_reports_empty(tmp_path, capsys):
    """State C from #1498: palace dir + chroma.sqlite3 exist but no drawers
    have been mined yet. status() must print the 'initialized but empty'
    message and suggest `mempalace mine`, not the misleading 'No palace
    found' / 'Run init' message."""
    import chromadb

    palace_path = tmp_path / "empty-palace"
    palace_path.mkdir()
    chromadb.PersistentClient(path=str(palace_path))  # creates chroma.sqlite3
    assert (palace_path / "chroma.sqlite3").is_file()

    status(str(palace_path))

    out = capsys.readouterr().out
    assert "initialized but empty" in out
    assert "mempalace mine" in out
    assert "No palace found" not in out


def test_status_palace_dir_without_db_reports_uninitialized(tmp_path, capsys):
    """State B from #1498: palace dir exists but chroma.sqlite3 is absent.
    Helper must short-circuit before invoking chromadb (which would lazily
    create the DB file as a side effect of a read-only inspection)."""
    palace_path = tmp_path / "no-db-palace"
    palace_path.mkdir()

    status(str(palace_path))

    out = capsys.readouterr().out
    assert "has no chroma.sqlite3 yet" in out
    # Side-effect check: chromadb was never touched.
    assert list(palace_path.iterdir()) == []


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

    with patch("mempalace.miner._open_collection_or_explain", return_value=FakeCol()):
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
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file, **kwargs: chunks)
    monkeypatch.setattr(miner, "detect_hall", lambda content: "code")
    monkeypatch.setattr(miner, "_extract_entities_for_metadata", lambda content: "")

    drawers, room, skip_reason = miner.process_file(
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
    assert skip_reason is None
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


def test_detect_room_uses_token_boundary_matching(tmp_path):
    """Path-part routing must not fire on incidental substrings.

    Regression: "views" is a substring of "interviews", so the old
    substring check routed every file under views/ into a room keyed
    by "interviews". Token-boundary matching prevents this while still
    matching real tokens like "frontend" in "frontend-app".
    """
    project = tmp_path
    rooms = [
        {"name": "billing-page", "keywords": ["billing-page"]},
        {"name": "interviews", "keywords": ["interviews"]},
        {"name": "general", "keywords": []},
    ]

    # views/<X>/... must NOT route to "interviews" on the "views"⊂"interviews" accident
    view_file = project / "views" / "billing-page" / "Foo.test.tsx"
    view_file.parent.mkdir(parents=True)
    view_file.write_text("content")
    assert detect_room(view_file, "content", rooms, project) == "billing-page"

    # data/interviews/... must route to "interviews" via the real token
    data_file = project / "data" / "interviews" / "index.ts"
    data_file.parent.mkdir(parents=True)
    data_file.write_text("content")
    assert detect_room(data_file, "content", rooms, project) == "interviews"


def test_detect_room_preserves_token_matches(tmp_path):
    """Real separator-bounded tokens still match in both directions."""
    project = tmp_path
    rooms = [
        {"name": "frontend", "keywords": ["frontend"]},
        {"name": "general", "keywords": []},
    ]

    # path part contains keyword as a token
    f1 = project / "frontend-app" / "main.ts"
    f1.parent.mkdir(parents=True)
    f1.write_text("x")
    assert detect_room(f1, "x", rooms, project) == "frontend"

    # keyword contains path part as a token (reverse direction)
    rooms2 = [
        {"name": "data-retention", "keywords": ["data-retention"]},
        {"name": "general", "keywords": []},
    ]
    f2 = project / "data" / "data-retention" / "policy.ts"
    f2.parent.mkdir(parents=True)
    f2.write_text("x")
    assert detect_room(f2, "x", rooms2, project) == "data-retention"


def test_detect_room_matches_keyword_distinct_from_name(tmp_path):
    """Regression: PR #145 — path part must match a keyword even when the
    room name itself doesn't contain the path part as a token.

    Scenario: a folder named ``docs/`` should route to a room named
    ``documentation`` that declares ``"docs"`` as a keyword.
    """
    project = tmp_path
    rooms = [
        {"name": "documentation", "keywords": ["docs"]},
        {"name": "general", "keywords": []},
    ]

    f = project / "docs" / "readme.md"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    assert detect_room(f, "x", rooms, project) == "documentation"


def test_detect_room_filename_match_uses_token_boundary(tmp_path):
    """Priority 2 (filename match) must also use token-boundary rules."""
    project = tmp_path
    rooms = [
        {"name": "review", "keywords": []},
        {"name": "general", "keywords": []},
    ]

    # "review" is a substring of "reviewmodule" but not a token — should NOT match
    f1 = project / "reviewmodule.ts"
    f1.write_text("x")
    assert detect_room(f1, "x", rooms, project) != "review"

    # "review" IS a token of "review-page" — should match
    f2 = project / "review-page.ts"
    f2.write_text("x")
    assert detect_room(f2, "x", rooms, project) == "review"

    # Dotted filename stems like "Foo.test" split on "." too
    rooms3 = [{"name": "foo", "keywords": []}, {"name": "general", "keywords": []}]
    f3 = project / "foo.test.ts"
    f3.write_text("x")
    assert detect_room(f3, "x", rooms3, project) == "foo"


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
    monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tunnels_file))
    monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(tunnels_file) + ".legacy")

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
    monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tunnels_file))
    monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(tunnels_file) + ".legacy")
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
    monkeypatch.setattr(palace_graph, "_get_tunnel_file", lambda *a, **kw: str(tunnels_file))
    monkeypatch.setattr(palace_graph, "_legacy_tunnel_file", lambda: str(tunnels_file) + ".legacy")

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
        return (1, "general", None)

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


def test_process_file_skips_when_chunks_exceed_max(tmp_path, monkeypatch, capsys):
    """A file exceeding the per-file chunk cap is skipped with a tagged
    return and a stderr/stdout message pointing at the config override. The
    cap is the rail against pathological artifacts (CSVs, lockfiles not in
    SKIP_FILENAMES) and against ONNX bad_alloc on Windows (#1296); #1455
    raised the default and made the cap configurable so legitimate
    long-form content is not silently dropped."""
    from unittest.mock import MagicMock

    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    monkeypatch.setattr(miner, "MAX_CHUNKS_PER_FILE", 5)
    over_cap = [{"content": f"chunk {i}", "chunk_index": i} for i in range(7)]
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file, **kwargs: over_cap)

    source = tmp_path / "huge.csv"
    source.write_text("col1,col2\n" + "x,y\n" * 500, encoding="utf-8")
    col = MagicMock()
    col.get.return_value = {"ids": []}

    drawers, room, skip_reason = miner.process_file(
        source,
        tmp_path,
        col,
        "wing",
        [{"name": "general", "description": "General"}],
        "agent",
        False,
    )

    assert drawers == 0
    assert skip_reason == "chunk_cap"
    col.upsert.assert_not_called()
    captured = capsys.readouterr()
    # Skip notice goes to stderr to match the existing symlink-skip
    # convention in ``scan_project`` so log piping stays coherent.
    assert "[skip]" in captured.err
    assert "--max-chunks-per-file" in captured.err
    assert "MEMPALACE_MAX_CHUNKS_PER_FILE" in captured.err
    assert "[skip]" not in captured.out


def test_resolve_max_chunks_default_when_no_override_no_env(monkeypatch):
    """With no override and no env var, the module-level default applies."""
    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    assert miner._resolve_max_chunks_per_file(None) == miner.MAX_CHUNKS_PER_FILE


def test_resolve_max_chunks_env_var_wins_over_default(monkeypatch):
    """A numeric env var overrides the module default."""
    from mempalace import miner

    monkeypatch.setenv("MEMPALACE_MAX_CHUNKS_PER_FILE", "777")
    assert miner._resolve_max_chunks_per_file(None) == 777


def test_resolve_max_chunks_override_wins_over_env(monkeypatch):
    """An explicit override (CLI flag plumbed in) wins over the env var."""
    from mempalace import miner

    monkeypatch.setenv("MEMPALACE_MAX_CHUNKS_PER_FILE", "777")
    assert miner._resolve_max_chunks_per_file(123) == 123


def test_resolve_max_chunks_sentinel_zero_disables(monkeypatch):
    """Sentinel ``0`` from override or env means "no cap"."""
    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    assert miner._resolve_max_chunks_per_file(0) == 0

    monkeypatch.setenv("MEMPALACE_MAX_CHUNKS_PER_FILE", "0")
    assert miner._resolve_max_chunks_per_file(None) == 0


def test_resolve_max_chunks_invalid_env_falls_back_to_default(monkeypatch, capsys):
    """A non-integer env value warns and uses the module default. This
    keeps a misconfigured shell from silently dropping content."""
    from mempalace import miner

    monkeypatch.setenv("MEMPALACE_MAX_CHUNKS_PER_FILE", "banana")
    assert miner._resolve_max_chunks_per_file(None) == miner.MAX_CHUNKS_PER_FILE
    err = capsys.readouterr().err
    assert "MEMPALACE_MAX_CHUNKS_PER_FILE" in err
    assert "banana" in err


def test_resolve_max_chunks_negative_env_falls_back_to_default(monkeypatch, capsys):
    """A negative env value warns and uses the module default."""
    from mempalace import miner

    monkeypatch.setenv("MEMPALACE_MAX_CHUNKS_PER_FILE", "-5")
    assert miner._resolve_max_chunks_per_file(None) == miner.MAX_CHUNKS_PER_FILE
    err = capsys.readouterr().err
    assert "MEMPALACE_MAX_CHUNKS_PER_FILE" in err
    assert "-5" in err


def test_process_file_sentinel_zero_disables_cap(tmp_path, monkeypatch):
    """With ``max_chunks_per_file=0`` even a pathologically large chunk
    count is processed (no skip)."""
    from unittest.mock import MagicMock

    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    monkeypatch.setattr(miner, "MAX_CHUNKS_PER_FILE", 5)
    big = [{"content": f"chunk {i}", "chunk_index": i} for i in range(20)]
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file, **kwargs: big)
    monkeypatch.setattr(miner, "detect_room", lambda *a, **k: "general")
    monkeypatch.setattr(miner, "_extract_entities_for_metadata", lambda content: "")
    monkeypatch.setattr(miner, "build_closet_lines", lambda *a, **k: [])
    monkeypatch.setattr(miner, "purge_file_closets", lambda *a, **k: None)
    monkeypatch.setattr(miner, "upsert_closet_lines", lambda *a, **k: None)

    source = tmp_path / "huge.csv"
    source.write_text("payload\n" * 500, encoding="utf-8")
    col = MagicMock()
    col.get.return_value = {"ids": []}

    drawers, _room, skip_reason = miner.process_file(
        source,
        tmp_path,
        col,
        "wing",
        [{"name": "general", "description": "General"}],
        "agent",
        False,
        max_chunks_per_file=0,
    )

    assert drawers == 20
    assert skip_reason is None
    col.upsert.assert_called()


def test_mine_summary_separates_chunk_cap_skips(tmp_path, monkeypatch, capsys):
    """Summary distinguishes residual skips from "chunk cap" skips so a
    user can see immediately that legitimate content was dropped (#1455).
    The chunk-cap summary line appears only when count > 0."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=3)
    palace_path = project_root / "palace"

    seq = iter(
        [
            (5, "general", None),
            (0, "general", "chunk_cap"),
            (0, "general", None),
        ]
    )

    def fake_process_file(*args, **kwargs):
        return next(seq)

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        mine(str(project_root), str(palace_path))

    out = capsys.readouterr().out
    assert "Files skipped (already filed or other): 1" in out
    assert "Files skipped (chunk cap" in out
    assert "--max-chunks-per-file" in out


def test_mine_summary_omits_chunk_cap_line_when_zero(tmp_path, monkeypatch, capsys):
    """When no file hits the chunk cap, the chunk-cap summary line is not
    printed at all, which keeps the happy-path output unchanged."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=2)
    palace_path = project_root / "palace"

    def fake_process_file(*args, **kwargs):
        return (3, "general", None)

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        mine(str(project_root), str(palace_path))

    out = capsys.readouterr().out
    assert "Files skipped (already filed or other): 0" in out
    assert "chunk cap" not in out


def test_resolve_max_chunks_negative_override_falls_back_to_default(monkeypatch, capsys):
    """A negative CLI override warns and falls back to the module default.
    Symmetric with the env-var path so ``--max-chunks-per-file=-500`` (a
    typo meaning "no, don't lower it that much") does not silently
    disable the cap and OOM the embedder on the original lockfile."""
    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    assert miner._resolve_max_chunks_per_file(-5) == miner.MAX_CHUNKS_PER_FILE
    err = capsys.readouterr().err
    assert "--max-chunks-per-file" in err
    assert "-5" in err


def test_resolve_max_chunks_reads_module_attribute_at_call_time(monkeypatch):
    """The resolver reads ``miner.MAX_CHUNKS_PER_FILE`` lazily, so a
    monkeypatch landed at test setup is honored. Regression guard against
    a future refactor that captures the import-time default."""
    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    monkeypatch.setattr(miner, "MAX_CHUNKS_PER_FILE", 7)
    assert miner._resolve_max_chunks_per_file(None) == 7


def test_process_file_chunk_cap_under_dry_run(tmp_path, monkeypatch):
    """Dry-run is the natural audit path for #1455; chunk-cap drops must
    return a tagged skip_reason there too so the summary counter can fire."""
    from unittest.mock import MagicMock

    from mempalace import miner

    monkeypatch.delenv("MEMPALACE_MAX_CHUNKS_PER_FILE", raising=False)
    monkeypatch.setattr(miner, "MAX_CHUNKS_PER_FILE", 5)
    over_cap = [{"content": f"chunk {i}", "chunk_index": i} for i in range(7)]
    monkeypatch.setattr(miner, "chunk_text", lambda content, source_file, **kwargs: over_cap)

    source = tmp_path / "big.csv"
    source.write_text("payload\n" * 200, encoding="utf-8")
    col = MagicMock()
    col.get.return_value = {"ids": []}

    drawers, _room, skip_reason = miner.process_file(
        source,
        tmp_path,
        col,
        "wing",
        [{"name": "general", "description": "General"}],
        "agent",
        True,  # dry_run
    )

    assert drawers == 0
    assert skip_reason == "chunk_cap"


def test_mine_dry_run_summary_counts_chunk_cap_drops(tmp_path, capsys):
    """The summary under dry-run also splits out chunk-cap skips. Without
    this a reporter running ``--dry-run`` to validate the new default
    against their corpus would see "Files processed: N / Files skipped: 0"
    even when chunk-cap drops occurred, which is exactly the silent-drop
    UX bug that #1455 is fixing."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=3)
    palace_path = project_root / "palace"

    seq = iter(
        [
            (5, "general", None),
            (0, "general", "chunk_cap"),
            (4, "general", None),
        ]
    )

    def fake_process_file(*args, **kwargs):
        return next(seq)

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        mine(str(project_root), str(palace_path), dry_run=True)

    out = capsys.readouterr().out
    assert "Files skipped (chunk cap" in out
    assert "1 (raise via" in out


def test_mine_plumbs_max_chunks_per_file_to_process_file(tmp_path):
    """``mine(max_chunks_per_file=0)`` reaches ``process_file`` as kwarg=0,
    enabling the sentinel-disable path end-to-end. Guards the wiring
    ``cmd_mine -> mine -> _mine_impl -> process_file``."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=1)
    palace_path = project_root / "palace"

    captured = {}

    def fake_process_file(*args, **kwargs):
        captured["max_chunks_per_file"] = kwargs.get("max_chunks_per_file")
        return (1, "general", None)

    with patch("mempalace.miner.process_file", side_effect=fake_process_file):
        mine(str(project_root), str(palace_path), max_chunks_per_file=0)

    assert captured["max_chunks_per_file"] == 0


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
        return (1, "general", None)

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
    """Our own per-target PID slot is removed in the finally clause."""
    import pytest
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=2)
    palace_path = project_root / "palace"

    pid_file = tmp_path / "mine_abc.pid"
    pid_file.write_text(str(os.getpid()))

    def fake_process_file(*args, **kwargs):
        raise KeyboardInterrupt

    # The mine subprocess receives its slot path via env var; the cleanup
    # hook in miner.py reads that var and removes the slot if it matches.
    with (
        patch.dict(os.environ, {"MEMPALACE_MINE_PID_FILE": str(pid_file)}),
        patch("mempalace.miner.process_file", side_effect=fake_process_file),
    ):
        with pytest.raises(SystemExit):
            mine(str(project_root), str(palace_path))

    assert not pid_file.exists(), "Our PID entry should be cleaned up on interrupt"


def test_mine_cleans_up_pid_file_on_clean_exit(tmp_path):
    """Successful mine also removes its own per-target PID slot."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=1)
    palace_path = project_root / "palace"

    pid_file = tmp_path / "mine_abc.pid"
    pid_file.write_text(str(os.getpid()))

    with patch.dict(os.environ, {"MEMPALACE_MINE_PID_FILE": str(pid_file)}):
        mine(str(project_root), str(palace_path))

    assert not pid_file.exists()


def test_mine_does_not_remove_other_processes_pid_file(tmp_path):
    """A PID slot pointing at someone else's PID is left untouched."""
    from unittest.mock import patch

    project_root = tmp_path / "proj"
    project_root.mkdir()
    _make_minable_project(project_root, n_files=1)
    palace_path = project_root / "palace"

    other_pid = os.getpid() + 999_999  # a PID that isn't us
    pid_file = tmp_path / "mine_abc.pid"
    pid_file.write_text(str(other_pid))

    with patch.dict(os.environ, {"MEMPALACE_MINE_PID_FILE": str(pid_file)}):
        mine(str(project_root), str(palace_path))

    assert pid_file.exists(), "Foreign PID entries must not be removed"
    assert pid_file.read_text().strip() == str(other_pid)
