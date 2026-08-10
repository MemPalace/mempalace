"""Round-trip tests for the JSONL export/import pair (#452).

Covers the cross-device sync contract: export is deterministic and
git-friendly; import merges by drawer id, is idempotent, and survives
malformed lines without aborting.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

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
