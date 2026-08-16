"""Round-trip tests for the JSONL export/import pair (#452).

Covers the cross-device sync contract: export is deterministic and
git-friendly; import merges by drawer id, is idempotent, and survives
malformed lines without aborting.
"""

import json
import os
import shutil
import signal
import tempfile
from pathlib import Path

import pytest
import yaml

from mempalace.exporter import export_palace_jsonl
from mempalace.importer import import_palace
from mempalace.miner import mine
from mempalace.palace import get_collection


def write_file(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_palace(tmpdir):
    """Create a small palace with drawers across two wings."""
    project_a = Path(tmpdir) / "project_a"
    project_b = Path(tmpdir) / "project_b"
    palace_path = str(Path(tmpdir) / "palace")

    os.makedirs(project_a / "backend")
    write_file(project_a / "backend" / "server.py", "def serve():\n    return 'ok'\n" * 20)
    with open(project_a / "mempalace.yaml", "w") as f:
        yaml.dump(
            {"wing": "alpha", "rooms": [{"name": "backend", "description": "Backend code"}]},
            f,
        )

    os.makedirs(project_b / "docs")
    write_file(project_b / "docs" / "guide.md", "# Guide\n\nThis explains things.\n" * 20)
    with open(project_b / "mempalace.yaml", "w") as f:
        yaml.dump(
            {"wing": "beta", "rooms": [{"name": "docs", "description": "Documentation"}]},
            f,
        )

    mine(str(project_a), palace_path)
    mine(str(project_b), palace_path)
    return palace_path


def _read_all_lines(export_dir):
    lines = []
    for path in sorted(Path(export_dir).rglob("*.jsonl")):
        with open(path, encoding="utf-8") as f:
            lines.extend(json.loads(line) for line in f if line.strip())
    return lines


def test_jsonl_export_structure_and_determinism():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        out1 = os.path.join(tmpdir, "export1")
        out2 = os.path.join(tmpdir, "export2")
        stats = export_palace_jsonl(palace_path, out1)
        assert stats["drawers"] > 0
        assert stats["wings"] == 2

        # Structure: wing dirs with room jsonl files + manifest.
        assert (Path(out1) / "export-manifest.json").is_file()
        assert (Path(out1) / "alpha" / "backend.jsonl").is_file()
        assert (Path(out1) / "beta" / "docs.jsonl").is_file()

        manifest = json.loads((Path(out1) / "export-manifest.json").read_text())
        assert manifest["format_version"] == 1
        assert manifest["drawers"] == stats["drawers"]

        # Every line carries the id/document/metadata triple.
        lines = _read_all_lines(out1)
        assert len(lines) == stats["drawers"]
        for obj in lines:
            assert isinstance(obj["id"], str) and obj["id"]
            assert isinstance(obj["document"], str)
            assert isinstance(obj["metadata"], dict)

        # Determinism: exporting the same palace twice is byte-identical.
        export_palace_jsonl(palace_path, out2)
        for p1 in sorted(Path(out1).rglob("*")):
            if p1.is_file():
                p2 = Path(out2) / p1.relative_to(out1)
                assert p2.read_bytes() == p1.read_bytes(), f"non-deterministic: {p1.name}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_round_trip_and_idempotency():
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        export_dir = os.path.join(tmpdir, "export")
        stats = export_palace_jsonl(palace_path, export_dir)

        # Import into a fresh palace on the "other machine".
        target_palace = os.path.join(tmpdir, "palace_b")
        result = import_palace(target_palace, export_dir)
        assert result["imported"] == stats["drawers"]
        assert result["skipped_existing"] == 0
        assert result["malformed"] == 0

        col = get_collection(target_palace)
        assert col.count() == stats["drawers"]

        # Content survives the round trip verbatim.
        source_lines = {obj["id"]: obj for obj in _read_all_lines(export_dir)}
        some_id = sorted(source_lines)[0]
        got = col.get(ids=[some_id], include=["documents", "metadatas"])
        assert got["documents"][0] == source_lines[some_id]["document"]
        assert got["metadatas"][0]["wing"] == source_lines[some_id]["metadata"]["wing"]

        # Idempotent: importing again adds nothing.
        again = import_palace(target_palace, export_dir)
        assert again["imported"] == 0
        assert again["skipped_existing"] == stats["drawers"]
        assert col.count() == stats["drawers"]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_skips_malformed_lines():
    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        good = {"id": "drawer-1", "document": "hello world", "metadata": {"wing": "w"}}
        bad_json = "{not json"
        bad_shape = json.dumps({"document": "no id"})
        (export_dir / "room.jsonl").write_text(
            json.dumps(good) + "\n" + bad_json + "\n" + bad_shape + "\n", encoding="utf-8"
        )

        palace_path = os.path.join(tmpdir, "palace")
        result = import_palace(palace_path, str(export_dir.parent))
        assert result["imported"] == 1
        assert result["malformed"] == 2

        col = get_collection(palace_path)
        assert col.count() == 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_dry_run_writes_nothing():
    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        line = {"id": "drawer-1", "document": "hello", "metadata": {"wing": "w"}}
        (export_dir / "room.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

        palace_path = os.path.join(tmpdir, "palace")
        result = import_palace(palace_path, str(export_dir.parent), dry_run=True)
        assert result["imported"] == 1
        # A dry run must not create the palace.
        assert not os.path.isdir(palace_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO support required")
def test_import_skips_a_fifo_without_blocking():
    """A named pipe carrying a .jsonl name must be refused by type, not opened.

    Opening a FIFO for reading parks in the kernel until a writer appears, so a
    regression here does not raise — it hangs forever. The alarm bounds that
    into a failure rather than a stuck CI job. Same guard class as #2221/#2244
    on the ingest side.
    """
    tmpdir = tempfile.mkdtemp()
    old_handler = None
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        good = {"id": "drawer-1", "document": "hello world", "metadata": {"wing": "w"}}
        (export_dir / "room.jsonl").write_text(json.dumps(good) + "\n", encoding="utf-8")
        os.mkfifo(str(export_dir / "pipe.jsonl"))

        # NOT an OSError subclass, deliberately: TimeoutError is one, and the
        # importer's own `except (OSError, UnicodeDecodeError)` would swallow
        # the alarm, book it as malformed, and let every assertion below pass
        # against the very hang this test exists to catch.
        class _Blocked(Exception):
            pass

        def _blocked(_signum, _frame):
            raise _Blocked("import blocked opening a non-regular file")

        old_handler = signal.signal(signal.SIGALRM, _blocked)
        signal.alarm(15)
        try:
            result = import_palace(
                os.path.join(tmpdir, "palace"), str(export_dir.parent), dry_run=True
            )
        finally:
            signal.alarm(0)

        # The real line still imports; the pipe is booked as unreadable, not read.
        assert result["imported"] == 1
        assert result["malformed"] == 1
        assert result["files"] == 2
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGALRM, old_handler)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_refuses_entries_outside_the_tree():
    """A symlinked DIRECTORY is traversed by recursive glob; O_NOFOLLOW cannot see it.

    `glob.glob(..., recursive=True)` follows symlinked directories, and
    O_NOFOLLOW only guards the final component — so without a containment
    check a regular file living outside the import tree is imported as if it
    belonged to it. The symlinked leaf is refused by O_NOFOLLOW; the file
    reached *through* the symlinked dir is what this test is about.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        inside = {"id": "inside-1", "document": "in the tree", "metadata": {"wing": "w"}}
        (export_dir / "room.jsonl").write_text(json.dumps(inside) + "\n", encoding="utf-8")

        outside = Path(tmpdir) / "elsewhere"
        outside.mkdir()
        stray = {"id": "outside-1", "document": "not in the tree", "metadata": {"wing": "w"}}
        (outside / "stray.jsonl").write_text(json.dumps(stray) + "\n", encoding="utf-8")
        try:
            os.symlink(str(outside), str(export_dir / "linked_dir"))
        except (OSError, NotImplementedError, AttributeError) as exc:
            # Windows has os.symlink but refuses it without privileges, so
            # hasattr() is not a usable guard — the attempt is.
            pytest.skip(f"symlink creation unavailable: {exc}")

        result = import_palace(os.path.join(tmpdir, "palace"), str(export_dir.parent), dry_run=True)
        # The stray file is globbed (glob follows the symlinked dir) but refused.
        assert result["imported"] == 1, "a file outside the import tree was imported"
        assert result["malformed"] == 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_reports_dropped_non_scalar_metadata():
    """Non-scalar metadata is dropped, but never silently.

    Chroma rejects non-scalars at write time, but `sqlite_exact` serializes
    metadata with an unrestricted json.dumps — so a palace on that backend can
    hold a list, the exporter writes it out raw, and this filter drops it on
    the way back in. That is a legitimate lossy round trip; an unreported one
    is not.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        line = {
            "id": "drawer-1",
            "document": "hello",
            "metadata": {"wing": "w", "tags": ["a", "b"], "nested": {"k": "v"}},
        }
        (export_dir / "room.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")

        result = import_palace(os.path.join(tmpdir, "palace"), str(export_dir.parent), dry_run=True)
        assert result["imported"] == 1
        assert result["metadata_dropped"] == 2, "the list and the dict must both be counted"
        assert result["malformed"] == 0, "a droppable value is not malformed input"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_reports_wholly_discarded_metadata():
    """A non-dict `metadata` is discarded WHOLE and must still be counted.

    Replacing a non-dict with {} before counting reports zero drops for a
    total loss — the same silent-loss class the counter exists to end, one
    level up. An ABSENT metadata key is a different thing: nothing was lost,
    so nothing is reported.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        discarded = {"id": "d-1", "document": "hello", "metadata": ["lost-a", "lost-b"]}
        absent = {"id": "d-2", "document": "hello"}
        (export_dir / "room.jsonl").write_text(
            json.dumps(discarded) + "\n" + json.dumps(absent) + "\n", encoding="utf-8"
        )

        result = import_palace(os.path.join(tmpdir, "palace"), str(export_dir.parent), dry_run=True)
        assert result["imported"] == 2
        assert result["metadata_dropped"] == 1, (
            "the discarded list must be counted, the absent key must not"
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_reexport_prunes_stale_room_files():
    """A room that no longer exists must not survive in the export tree.

    Import walks every *.jsonl without consulting the manifest, so a stale room
    file is not a cosmetic git wrinkle — the next device re-imports it and the
    deleted drawers come back.
    """
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        out = os.path.join(tmpdir, "export")
        export_palace_jsonl(palace_path, out)

        # A room file from a previous export whose room is now gone.
        stale = Path(out) / "alpha" / "retired.jsonl"
        stale.write_text(
            json.dumps({"id": "ghost-1", "document": "deleted", "metadata": {"wing": "alpha"}})
            + "\n",
            encoding="utf-8",
        )
        orphan_wing = Path(out) / "gamma"
        orphan_wing.mkdir()
        (orphan_wing / "old.jsonl").write_text("{}\n", encoding="utf-8")

        export_palace_jsonl(palace_path, out)

        assert not stale.exists(), "stale room file survived a re-export"
        assert not orphan_wing.exists(), "emptied wing directory survived a re-export"
        assert (Path(out) / "alpha" / "backend.jsonl").is_file(), "live room was pruned"

        # And the ghost cannot come back through import.
        target = os.path.join(tmpdir, "palace_b")
        result = import_palace(target, out)
        ids = get_collection(target).get(include=[])["ids"]
        assert "ghost-1" not in ids
        assert result["imported"] > 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_export_never_prunes_a_directory_it_did_not_write():
    """Pruning is gated on a prior manifest — a first export deletes nothing."""
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        out = os.path.join(tmpdir, "someones_dir")
        os.makedirs(os.path.join(out, "alpha"))
        bystander = Path(out) / "alpha" / "not-ours.jsonl"
        bystander.write_text("{}\n", encoding="utf-8")

        export_palace_jsonl(palace_path, out)  # no export-manifest.json existed

        assert bystander.exists(), "a first export deleted a file it did not write"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_empty_palace_leaves_a_previous_export_alone(capsys):
    """Zero drawers is also what a failed palace open looks like — do not prune."""
    tmpdir = tempfile.mkdtemp()
    try:
        palace_path = _setup_palace(tmpdir)
        out = os.path.join(tmpdir, "export")
        export_palace_jsonl(palace_path, out)
        survivor = Path(out) / "alpha" / "backend.jsonl"
        assert survivor.is_file()

        empty_palace = os.path.join(tmpdir, "empty_palace")
        export_palace_jsonl(empty_palace, out)

        assert survivor.is_file(), "a zero-drawer palace wiped a good export"
        assert "WARNING" in capsys.readouterr().out
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_holds_the_writer_lease(monkeypatch):
    """A real import takes mine_palace_lock; a dry run must not."""
    import contextlib as _ctx

    import mempalace.importer as importer_mod

    taken = []

    @_ctx.contextmanager
    def _spy(path):
        taken.append(path)
        yield

    monkeypatch.setattr(importer_mod, "mine_palace_lock", _spy)

    tmpdir = tempfile.mkdtemp()
    try:
        export_dir = Path(tmpdir) / "export" / "wing"
        export_dir.mkdir(parents=True)
        line = {"id": "d-1", "document": "hello", "metadata": {"wing": "w"}}
        (export_dir / "room.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
        palace_path = os.path.join(tmpdir, "palace")

        import_palace(palace_path, str(export_dir.parent), dry_run=True)
        assert taken == [], "a dry run took the writer lease"

        import_palace(palace_path, str(export_dir.parent))
        assert taken == [palace_path], "a real import did not take the writer lease"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_import_rejects_non_directory():
    tmpdir = tempfile.mkdtemp()
    try:
        try:
            import_palace(os.path.join(tmpdir, "palace"), os.path.join(tmpdir, "missing"))
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
