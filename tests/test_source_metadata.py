import hashlib

import pytest

from mempalace.source_metadata import (
    SourceContext,
    build_source_metadata,
    source_kind_for_room,
)


def test_build_source_metadata_is_scalar_and_deterministic(tmp_path):
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("print('x')\n")
    context = SourceContext(
        root=str(tmp_path),
        source_file=str(source),
        source_kind="code",
        memory_tier="hot",
        source_revision="abc123",
    )

    first = build_source_metadata(context, "print('x')\n", 0)
    second = build_source_metadata(context, "print('x')\n", 0)

    assert first == second
    assert first["source_identity"] == "code:src/app.py"
    assert first["content_sha256"] == hashlib.sha256(b"print('x')\n").hexdigest()
    assert first["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert first["source_revision"] == "abc123"
    assert all(isinstance(value, (str, int, float, bool)) for value in first.values())


def test_source_identity_uses_canonical_root_for_symlink(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "README.md"
    source.write_text("docs")
    alias = tmp_path / "alias"
    alias.symlink_to(root)

    direct = build_source_metadata(
        SourceContext(str(root), str(source), "documentation"), "docs", 0
    )
    linked = build_source_metadata(
        SourceContext(str(alias), str(alias / "README.md"), "documentation"), "docs", 0
    )

    assert direct["source_root"] == linked["source_root"]
    assert direct["source_identity"] == linked["source_identity"]


@pytest.mark.parametrize("field,value", [("source_kind", "mystery"), ("memory_tier", "archive")])
def test_source_metadata_rejects_unknown_enums(tmp_path, field, value):
    source = tmp_path / "note.txt"
    source.write_text("note")
    values = {
        "root": str(tmp_path),
        "source_file": str(source),
        "source_kind": "curated",
        "memory_tier": "hot",
    }
    values[field] = value

    with pytest.raises(ValueError, match=field):
        build_source_metadata(SourceContext(**values), "note", 0)


def test_room_source_kind_overrides_project_default():
    config = {
        "source_kind": "code",
        "rooms": [{"name": "docs", "source_kind": "documentation"}],
    }

    assert source_kind_for_room(config, "docs") == "documentation"
    assert source_kind_for_room(config, "backend") == "code"
