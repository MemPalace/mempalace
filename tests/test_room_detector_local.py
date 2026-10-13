"""Tests for mempalace.room_detector_local."""

from unittest.mock import MagicMock, patch

import pytest

from mempalace.room_detector_local import (
    FOLDER_ROOM_MAP,
    detect_rooms_from_files,
    detect_rooms_from_folders,
    detect_rooms_local,
    get_user_approval,
    print_proposed_structure,
    save_config,
)


# ── FOLDER_ROOM_MAP ────────────────────────────────────────────────────


def test_folder_room_map_has_expected_mappings():
    assert FOLDER_ROOM_MAP["frontend"] == "frontend"
    assert FOLDER_ROOM_MAP["backend"] == "backend"
    assert FOLDER_ROOM_MAP["docs"] == "documentation"
    assert FOLDER_ROOM_MAP["tests"] == "testing"
    assert FOLDER_ROOM_MAP["config"] == "configuration"


def test_folder_room_map_alternative_names():
    assert FOLDER_ROOM_MAP["front-end"] == "frontend"
    assert FOLDER_ROOM_MAP["back-end"] == "backend"
    assert FOLDER_ROOM_MAP["server"] == "backend"
    assert FOLDER_ROOM_MAP["client"] == "frontend"
    assert FOLDER_ROOM_MAP["api"] == "backend"


# ── detect_rooms_from_folders ───────────────────────────────────────────


def test_detect_rooms_from_folders_standard_layout(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend").mkdir()
    (tmp_path / "docs").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    assert "frontend" in room_names
    assert "backend" in room_names
    assert "documentation" in room_names


def test_detect_rooms_from_folders_always_has_general(tmp_path):
    rooms = detect_rooms_from_folders(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    assert "general" in room_names


def test_detect_rooms_from_folders_empty_dir(tmp_path):
    rooms = detect_rooms_from_folders(str(tmp_path))
    # Should at least have "general"
    assert len(rooms) >= 1
    assert any(r["name"] == "general" for r in rooms)


def test_detect_rooms_from_folders_skips_oserror_at_top_level(tmp_path):
    """Windows reparse points (junctions, untrusted mount points) raise OSError
    when stat()'d.  The scanner must skip them and continue — not crash.

    Reproduces WinError 448: "The path cannot be traversed because it contains
    an untrusted mount point", seen on Windows when a project folder contains
    a git-submodule junction or a dev-drive reparse point.
    """
    (tmp_path / "frontend").mkdir()
    bad = tmp_path / "untrusted_junction"
    bad.mkdir()

    original_is_dir = bad.__class__.is_dir

    def patched_is_dir(self):
        if self == bad:
            raise OSError(
                "[WinError 448] The path cannot be traversed because it contains an untrusted mount point"
            )
        return original_is_dir(self)

    with patch.object(bad.__class__, "is_dir", patched_is_dir):
        rooms = detect_rooms_from_folders(str(tmp_path))

    room_names = {r["name"] for r in rooms}
    assert "frontend" in room_names


def test_detect_rooms_from_folders_skips_oserror_nested(tmp_path):
    """Same WinError 448 guard applies one level deeper (nested iterdir pass)."""
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "docs").mkdir()
    bad = skills / "bad_junction"
    bad.mkdir()

    original_is_dir = bad.__class__.is_dir

    def patched_is_dir(self):
        if self == bad:
            raise OSError(
                "[WinError 448] The path cannot be traversed because it contains an untrusted mount point"
            )
        return original_is_dir(self)

    with patch.object(bad.__class__, "is_dir", patched_is_dir):
        rooms = detect_rooms_from_folders(str(tmp_path))

    room_names = {r["name"] for r in rooms}
    assert "documentation" in room_names


def test_detect_rooms_from_folders_skips_iterdir_oserror(tmp_path):
    """iterdir() itself can raise OSError on some Windows reparse points even
    when is_dir() succeeds.  The nested pass must guard the iterdir() call too."""
    outer = tmp_path / "src"
    outer.mkdir()
    (tmp_path / "docs").mkdir()

    original_iterdir = outer.__class__.iterdir

    def patched_iterdir(self):
        if self == outer:
            raise OSError(
                "[WinError 448] The path cannot be traversed because it contains an untrusted mount point"
            )
        return original_iterdir(self)

    with patch.object(outer.__class__, "iterdir", patched_iterdir):
        rooms = detect_rooms_from_folders(str(tmp_path))

    room_names = {r["name"] for r in rooms}
    # docs is accessible; src fails on iterdir — neither should crash
    assert "documentation" in room_names


def test_detect_rooms_from_folders_skips_git(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "frontend").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    assert ".git" not in room_names
    assert "node_modules" not in room_names


def test_detect_rooms_from_folders_nested_dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "components").mkdir()
    (src / "routes").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    # Nested dirs should be detected at one level deep
    assert "frontend" in room_names or "backend" in room_names


def test_detect_rooms_from_folders_room_has_description(tmp_path):
    (tmp_path / "docs").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    doc_room = next((r for r in rooms if r["name"] == "documentation"), None)
    assert doc_room is not None
    assert "description" in doc_room
    assert "docs" in doc_room["description"]


def test_detect_rooms_from_folders_room_has_keywords(tmp_path):
    (tmp_path / "frontend").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    fe_room = next((r for r in rooms if r["name"] == "frontend"), None)
    assert fe_room is not None
    assert "keywords" in fe_room
    assert len(fe_room["keywords"]) > 0


def test_detect_rooms_from_folders_custom_named_dirs(tmp_path):
    (tmp_path / "mylib").mkdir()
    rooms = detect_rooms_from_folders(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    # Custom dir names that don't match FOLDER_ROOM_MAP get added as-is
    assert "mylib" in room_names or "general" in room_names


# ── detect_rooms_from_files ─────────────────────────────────────────────


def test_detect_rooms_from_files_with_matching_filenames(tmp_path):
    # Create files whose names contain room keywords
    for name in ["test_auth.py", "test_login.py", "test_api.py"]:
        (tmp_path / name).write_text("content")
    rooms = detect_rooms_from_files(str(tmp_path))
    room_names = {r["name"] for r in rooms}
    assert "testing" in room_names or "general" in room_names


def test_detect_rooms_from_files_empty_dir(tmp_path):
    rooms = detect_rooms_from_files(str(tmp_path))
    assert len(rooms) >= 1
    assert any(r["name"] == "general" for r in rooms)


def test_detect_rooms_from_files_caps_at_six(tmp_path):
    # Create many files with different keywords to hit the cap
    for keyword in ["test", "doc", "api", "config", "frontend", "backend", "design", "meeting"]:
        for i in range(3):
            (tmp_path / f"{keyword}_file_{i}.txt").write_text("content")
    rooms = detect_rooms_from_files(str(tmp_path))
    assert len(rooms) <= 6


# ── save_config ─────────────────────────────────────────────────────────


def test_save_config_creates_yaml(tmp_path):
    rooms = [
        {"name": "frontend", "description": "UI files", "keywords": ["frontend"]},
        {"name": "backend", "description": "Server files", "keywords": ["backend"]},
    ]
    save_config(str(tmp_path), "myproject", rooms)
    config_file = tmp_path / "mempalace.yaml"
    assert config_file.exists()
    content = config_file.read_text()
    assert "myproject" in content
    assert "frontend" in content
    assert "backend" in content


def test_save_config_valid_yaml(tmp_path):
    import yaml

    rooms = [{"name": "general", "description": "All files", "keywords": []}]
    save_config(str(tmp_path), "test_proj", rooms)
    config_file = tmp_path / "mempalace.yaml"
    data = yaml.safe_load(config_file.read_text())
    assert data["wing"] == "test_proj"
    assert len(data["rooms"]) == 1
    assert data["rooms"][0]["name"] == "general"


ROOMS = [{"name": "backend", "description": "Server files", "keywords": ["backend"]}]


@pytest.mark.parametrize("existing", ["mempalace.yml", "mempal.yaml", "mempal.yml"])
def test_save_config_updates_an_existing_alternate_config(tmp_path, existing):
    """init rewrites the config the miner reads instead of creating a
    mempalace.yaml that would shadow it, and keeps hand-written keys."""
    import yaml

    (tmp_path / existing).write_text(
        "wing: old\nrooms: []\nexclude_patterns:\n  - secret/\n", encoding="utf-8"
    )

    save_config(str(tmp_path), "myproject", ROOMS)

    assert not (tmp_path / "mempalace.yaml").exists()
    data = yaml.safe_load((tmp_path / existing).read_text(encoding="utf-8"))
    assert data["wing"] == "myproject"
    assert [r["name"] for r in data["rooms"]] == ["backend"]
    assert data["exclude_patterns"] == ["secret/"]


def test_save_config_keeps_hand_written_keys_in_mempalace_yaml(tmp_path):
    import yaml

    (tmp_path / "mempalace.yaml").write_text(
        "wing: old\nrooms: []\nexclude_patterns: ['*.log']\n", encoding="utf-8"
    )
    save_config(str(tmp_path), "myproject", ROOMS)
    data = yaml.safe_load((tmp_path / "mempalace.yaml").read_text(encoding="utf-8"))
    assert data["wing"] == "myproject"
    assert data["exclude_patterns"] == ["*.log"]


# A broken config is user data init cannot read. It is regenerated (init has
# to write something the miner can use) but only after a byte-exact copy is
# kept beside it, and the user is told where.
BROKEN = (
    "# hand-written, keep me\nwing: [unclosed\nexclude_patterns:\n  - secret/ # café\n".encode()
)


@pytest.mark.parametrize("name", ["mempalace.yml", "mempalace.yaml", "mempal.yaml", "mempal.yml"])
def test_save_config_backs_up_an_unparseable_config(tmp_path, capsys, name):
    import yaml

    (tmp_path / name).write_bytes(BROKEN)

    save_config(str(tmp_path), "myproject", ROOMS)

    backup = tmp_path / f"{name}.bak"
    assert backup.read_bytes() == BROKEN
    data = yaml.safe_load((tmp_path / name).read_text(encoding="utf-8"))
    assert data == {"wing": "myproject", "rooms": ROOMS}
    if name != "mempalace.yaml":
        assert not (tmp_path / "mempalace.yaml").exists()
    out = capsys.readouterr()
    assert str(backup) in out.err
    assert "does not parse" in out.err


def test_save_config_warns_before_regenerating(tmp_path, capsys, monkeypatch):
    """The backup and the warning both happen before the config is rewritten."""
    import mempalace.room_detector_local as rdl

    (tmp_path / "mempalace.yml").write_bytes(BROKEN)
    seen_at_dump = {}
    real_dump = rdl.yaml.dump

    def spy_dump(*args, **kwargs):
        seen_at_dump["err"] = capsys.readouterr().err
        seen_at_dump["backup"] = (tmp_path / "mempalace.yml.bak").read_bytes()
        return real_dump(*args, **kwargs)

    monkeypatch.setattr(rdl.yaml, "dump", spy_dump)
    save_config(str(tmp_path), "myproject", ROOMS)

    assert str(tmp_path / "mempalace.yml.bak") in seen_at_dump["err"]
    assert seen_at_dump["backup"] == BROKEN


def test_save_config_never_clobbers_an_existing_backup(tmp_path, capsys):
    older = b"wing: an older backup the user kept\n"
    (tmp_path / "mempalace.yml.bak").write_bytes(older)
    (tmp_path / "mempalace.yml").write_bytes(BROKEN)

    save_config(str(tmp_path), "myproject", ROOMS)

    assert (tmp_path / "mempalace.yml.bak").read_bytes() == older
    new_backups = sorted(tmp_path.glob("mempalace.yml.bak.*"))
    assert len(new_backups) == 1, new_backups
    assert new_backups[0].read_bytes() == BROKEN
    assert str(new_backups[0]) in capsys.readouterr().err


def test_save_config_backs_up_a_config_that_is_not_a_mapping(tmp_path):
    (tmp_path / "mempalace.yml").write_bytes(b"- just\n- a list\n")
    save_config(str(tmp_path), "myproject", ROOMS)
    assert (tmp_path / "mempalace.yml.bak").read_bytes() == b"- just\n- a list\n"


def test_save_config_does_not_back_up_an_empty_config(tmp_path):
    (tmp_path / "mempalace.yml").write_bytes(b"\n")
    save_config(str(tmp_path), "myproject", ROOMS)
    assert list(tmp_path.glob("*.bak*")) == []


@pytest.mark.parametrize(
    "raw",
    [b"# exclude_patterns:\n#   - secret/\n", b"# keep me\n{}\n", b"~ # nothing yet\n"],
)
def test_save_config_backs_up_a_config_that_parses_as_empty(tmp_path, capsys, raw):
    """Comments-only bytes parse to nothing but are still the user's file."""
    import yaml

    (tmp_path / "mempalace.yml").write_bytes(raw)

    save_config(str(tmp_path), "myproject", ROOMS)

    assert (tmp_path / "mempalace.yml.bak").read_bytes() == raw
    data = yaml.safe_load((tmp_path / "mempalace.yml").read_text(encoding="utf-8"))
    assert data == {"wing": "myproject", "rooms": ROOMS}
    assert "parses as empty" in capsys.readouterr().err


def test_save_config_refuses_to_regenerate_when_the_backup_fails(tmp_path, monkeypatch):
    import mempalace.room_detector_local as rdl

    (tmp_path / "mempalace.yml").write_bytes(BROKEN)

    def no_backup(path, data):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(rdl, "_write_backup", no_backup)
    with pytest.raises(OSError, match="not overwritten"):
        save_config(str(tmp_path), "myproject", ROOMS)
    assert (tmp_path / "mempalace.yml").read_bytes() == BROKEN


def test_init_then_load_config_sees_the_yml_settings(tmp_path):
    """Round trip: after save_config, the miner's load_config still applies
    the user's exclude_patterns from mempalace.yml."""
    from mempalace.miner import load_config

    (tmp_path / "mempalace.yml").write_text(
        "wing: old\nrooms: []\nexclude_patterns: ['secret/']\n", encoding="utf-8"
    )
    save_config(str(tmp_path), "myproject", ROOMS)
    config = load_config(str(tmp_path))
    assert config["wing"] == "myproject"
    assert config["exclude_patterns"] == ["secret/"]


# ── print_proposed_structure ──────────────────────────────────────────


def test_print_proposed_structure(capsys):
    rooms = [
        {"name": "frontend", "description": "UI files"},
        {"name": "general", "description": "Everything else"},
    ]
    print_proposed_structure("myapp", rooms, 42, "folder structure")
    out = capsys.readouterr().out
    assert "myapp" in out
    assert "frontend" in out
    assert "42 files" in out
    assert "folder structure" in out


# ── get_user_approval ─────────────────────────────────────────────────


def test_get_user_approval_accept_all():
    rooms = [{"name": "frontend", "description": "UI"}]
    with patch("builtins.input", return_value=""):
        result = get_user_approval(rooms)
    assert result == rooms


def test_get_user_approval_edit_remove():
    rooms = [
        {"name": "frontend", "description": "UI"},
        {"name": "backend", "description": "Server"},
    ]
    with patch("builtins.input", side_effect=["edit", "1", "n"]):
        result = get_user_approval(rooms)
    # Room 1 (frontend) removed
    assert len(result) == 1
    assert result[0]["name"] == "backend"


def test_get_user_approval_add_room():
    rooms = [{"name": "general", "description": "All files"}]
    with patch(
        "builtins.input",
        side_effect=[
            "add",
            "custom_room",
            "My custom room",
            "",
        ],
    ):
        result = get_user_approval(rooms)
    names = [r["name"] for r in result]
    assert "custom_room" in names


# ── detect_rooms_local ────────────────────────────────────────────────


def test_detect_rooms_local_yes_mode(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "readme.md").write_text("hello")
    mock_miner = MagicMock()
    mock_miner.scan_project.return_value = ["file1.py"]
    with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
        detect_rooms_local(str(tmp_path), yes=True)
    assert (tmp_path / "mempalace.yaml").exists()


def test_detect_rooms_local_fallback_to_files(tmp_path):
    """When folder detection gives only 'general', falls back to file patterns."""
    for i in range(3):
        (tmp_path / f"test_file_{i}.py").write_text("content")
    mock_miner = MagicMock()
    mock_miner.scan_project.return_value = ["f1", "f2"]
    with patch.dict("sys.modules", {"mempalace.miner": mock_miner}):
        detect_rooms_local(str(tmp_path), yes=True)
    assert (tmp_path / "mempalace.yaml").exists()


def test_detect_rooms_local_missing_dir():
    """Non-existent directory causes sys.exit."""
    import pytest

    with pytest.raises(SystemExit):
        detect_rooms_local("/nonexistent/path/that/does/not/exist", yes=True)


def test_detect_rooms_local_interactive(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("code")
    mock_miner = MagicMock()
    mock_miner.scan_project.return_value = ["f1"]
    with (
        patch.dict("sys.modules", {"mempalace.miner": mock_miner}),
        patch(
            "mempalace.room_detector_local.get_user_approval",
            return_value=[{"name": "general", "description": "All files", "keywords": []}],
        ),
    ):
        detect_rooms_local(str(tmp_path), yes=False)
    assert (tmp_path / "mempalace.yaml").exists()
