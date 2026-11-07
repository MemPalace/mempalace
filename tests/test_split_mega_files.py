import json

from mempalace import split_mega_files as smf

# ── Config loading ─────────────────────────────────────────────────────


def test_load_known_people_falls_back_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", tmp_path / "missing.json")
    smf._KNOWN_NAMES_CACHE = None

    assert smf._load_known_people() == []
    assert smf._load_username_map() == {}


def test_load_known_people_from_list_config(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text(json.dumps(["Alice", "Ben"]))
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    smf._KNOWN_NAMES_CACHE = None

    assert smf._load_known_people() == ["Alice", "Ben"]
    assert smf._load_username_map() == {}


def test_load_known_people_from_dict_config(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text(json.dumps({"names": ["Alice"], "username_map": {"jdoe": "John"}}))
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    smf._KNOWN_NAMES_CACHE = None

    assert smf._load_known_people() == ["Alice"]
    assert smf._load_username_map() == {"jdoe": "John"}


def test_extract_people_uses_username_map(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text(json.dumps({"names": ["Alice"], "username_map": {"jdoe": "John"}}))
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    monkeypatch.setattr(smf, "KNOWN_PEOPLE", ["Alice"])
    smf._KNOWN_NAMES_CACHE = None

    people = smf.extract_people(["Working in /Users/jdoe/project\n"])
    assert "John" in people


def test_extract_people_detects_names_from_content(monkeypatch):
    monkeypatch.setattr(smf, "KNOWN_PEOPLE", ["Alice", "Ben"])
    people = smf.extract_people(["> Alice reviewed the change with Ben\n"])
    assert people == ["Alice", "Ben"]


def test_extract_people_empty_when_no_config(monkeypatch):
    """Without known_names.json, no names should be detected — even for common names."""
    monkeypatch.setattr(smf, "KNOWN_PEOPLE", [])
    people = smf.extract_people(["> Max reviewed the change with Sam and Jordan\n"])
    assert people == []


# ── Config: force_reload and invalid JSON ──────────────────────────────


def test_load_known_names_force_reload(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text(json.dumps(["Alice"]))
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    smf._KNOWN_NAMES_CACHE = None

    smf._load_known_names_config()
    assert smf._KNOWN_NAMES_CACHE == ["Alice"]

    config_path.write_text(json.dumps(["Bob"]))
    smf._load_known_names_config(force_reload=True)
    assert smf._KNOWN_NAMES_CACHE == ["Bob"]


def test_load_known_names_invalid_json(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text("not json {{{")
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    smf._KNOWN_NAMES_CACHE = None

    result = smf._load_known_names_config()
    assert result is None


def test_load_known_names_caching(monkeypatch, tmp_path):
    config_path = tmp_path / "known_names.json"
    config_path.write_text(json.dumps(["Alice"]))
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", config_path)
    smf._KNOWN_NAMES_CACHE = None

    smf._load_known_names_config()
    # Second call returns cached value without re-reading
    config_path.write_text(json.dumps(["Changed"]))
    result = smf._load_known_names_config()
    assert result == ["Alice"]


# ── is_true_session_start ──────────────────────────────────────────────


def test_is_true_session_start_yes():
    lines = ["Claude Code v1.0", "Some content", "More content", "", "", ""]
    assert smf.is_true_session_start(lines, 0) is True


def test_is_true_session_start_no_ctrl_e():
    lines = [
        "Claude Code v1.0",
        "Ctrl+E to show 5 previous messages",
        "",
        "",
        "",
        "",
    ]
    assert smf.is_true_session_start(lines, 0) is False


def test_is_true_session_start_no_previous_messages():
    lines = [
        "Claude Code v1.0",
        "Some text",
        "previous messages here",
        "",
        "",
        "",
    ]
    assert smf.is_true_session_start(lines, 0) is False


# ── find_session_boundaries ────────────────────────────────────────────


def test_find_session_boundaries_two_sessions():
    lines = [
        "Claude Code v1.0",
        "content 1",
        "",
        "",
        "",
        "",
        "",
        "Claude Code v1.0",
        "content 2",
        "",
        "",
        "",
        "",
        "",
    ]
    boundaries = smf.find_session_boundaries(lines)
    assert boundaries == [0, 7]


def test_find_session_boundaries_none():
    lines = ["Just some text", "No sessions here"]
    assert smf.find_session_boundaries(lines) == []


def test_find_session_boundaries_context_restore_skipped():
    lines = [
        "Claude Code v1.0",
        "content",
        "",
        "",
        "",
        "",
        "",
        "Claude Code v1.0",
        "Ctrl+E to show 5 previous messages",
        "",
        "",
        "",
        "",
    ]
    boundaries = smf.find_session_boundaries(lines)
    assert len(boundaries) == 1


# ── extract_timestamp ──────────────────────────────────────────────────


def test_extract_timestamp_found():
    lines = ["⏺ 2:30 PM Wednesday, March 25, 2026"]
    human, iso = smf.extract_timestamp(lines)
    assert human == "2026-03-25_230PM"
    assert iso == "2026-03-25"


def test_extract_timestamp_not_found():
    lines = ["No timestamp here"]
    human, iso = smf.extract_timestamp(lines)
    assert human is None
    assert iso is None


def test_extract_timestamp_only_checks_first_50():
    lines = ["filler\n"] * 51 + ["⏺ 1:00 AM Monday, January 01, 2026"]
    human, iso = smf.extract_timestamp(lines)
    assert human is None


# ── extract_subject ────────────────────────────────────────────────────


def test_extract_subject_found():
    lines = ["> How do we handle authentication?"]
    subject = smf.extract_subject(lines)
    assert "authentication" in subject.lower()


def test_extract_subject_skips_commands():
    lines = ["> cd /some/dir", "> git status", "> What is the plan?"]
    subject = smf.extract_subject(lines)
    assert "plan" in subject.lower()


def test_extract_subject_fallback():
    lines = ["No prompts at all", "Just text"]
    subject = smf.extract_subject(lines)
    assert subject == "session"


def test_extract_subject_short_prompt_skipped():
    lines = ["> ok", "> yes", "> What about the deployment strategy?"]
    subject = smf.extract_subject(lines)
    assert "deployment" in subject.lower()


def test_extract_subject_truncated():
    lines = ["> " + "a" * 100]
    subject = smf.extract_subject(lines)
    assert len(subject) <= 60


# ── split_file ─────────────────────────────────────────────────────────


def _make_mega_file(tmp_path, n_sessions=3, lines_per_session=15):
    """Create a mega-file with N sessions."""
    content = ""
    for i in range(n_sessions):
        content += f"Claude Code v1.{i}\n"
        content += f"> What about topic {i} and how it works?\n"
        for j in range(lines_per_session - 2):
            content += f"Line {j} of session {i}\n"
    path = tmp_path / "mega.txt"
    path.write_text(content)
    return path


def test_split_file_creates_output(tmp_path):
    mega = _make_mega_file(tmp_path)
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    written = smf.split_file(str(mega), str(out_dir))
    assert len(written) >= 2
    for p in written:
        assert p.exists()


def test_split_file_dry_run(tmp_path):
    mega = _make_mega_file(tmp_path)
    out_dir = tmp_path / "output"
    out_dir.mkdir()
    written = smf.split_file(str(mega), str(out_dir), dry_run=True)
    assert len(written) >= 2
    for p in written:
        assert not p.exists()


def test_split_file_not_mega(tmp_path):
    """File with fewer than 2 sessions is not split."""
    path = tmp_path / "single.txt"
    path.write_text("Claude Code v1.0\nJust one session\n" + "line\n" * 20)
    written = smf.split_file(str(path), str(tmp_path))
    assert written == []


def test_split_file_output_dir_none(tmp_path):
    """When output_dir is None, writes to same dir as source."""
    mega = _make_mega_file(tmp_path)
    written = smf.split_file(str(mega), None)
    assert len(written) >= 2
    for p in written:
        assert str(p.parent) == str(tmp_path)


def test_split_file_keeps_short_sessions(tmp_path):
    """Sessions under 10 lines are written, not dropped."""
    content = "Claude Code v1.0\nSHORT_SESSION_KEPT\n" * 2 + "Claude Code v1.0\n" + "line\n" * 20
    path = tmp_path / "tiny.txt"
    path.write_text(content)
    written = smf.split_file(str(path), str(tmp_path))
    assert len(written) == 3
    blob = "".join(p.read_text(encoding="utf-8") for p in written)
    assert blob.count("SHORT_SESSION_KEPT") == 2


def test_split_file_keeps_preamble(tmp_path):
    content = (
        "PREAMBLE_KEPT before any session\n"
        "Claude Code v1.0\n"
        "first session body\n"
        "Claude Code v1.0\n" + "second session line\n" * 12
    )
    path = tmp_path / "mega.txt"
    path.write_text(content, encoding="utf-8")
    written = smf.split_file(str(path), str(tmp_path))
    blob = "".join(path.read_text(encoding="utf-8") for path in written)
    assert "PREAMBLE_KEPT" in blob
    assert any("part00" in path.name for path in written)
    assert path.read_text(encoding="utf-8") == content


def test_split_file_does_not_overwrite_colliding_names(tmp_path):
    def session(marker: str) -> str:
        return (
            "Claude Code v1.0\n"
            "⏺ 2:30 PM Wednesday, March 25, 2026\n"
            "> What about topic and how it works?\n"
            f"{marker}\n"
        )

    content = session("FIRST_SESSION_MARKER") + session("SECOND_SESSION_MARKER")
    path = tmp_path / "mega.txt"
    path.write_text(content, encoding="utf-8")
    written = smf.split_file(str(path), str(tmp_path))
    texts = [item.read_text(encoding="utf-8") for item in written]
    assert len(written) == 2
    assert len({item.name for item in written}) == 2
    assert any(
        "FIRST_SESSION_MARKER" in text and "SECOND_SESSION_MARKER" not in text for text in texts
    )
    assert any(
        "SECOND_SESSION_MARKER" in text and "FIRST_SESSION_MARKER" not in text for text in texts
    )


def test_split_file_decodes_cp1252_without_replacement_char(tmp_path):
    def session(body: bytes) -> bytes:
        return b"Claude Code v1.0\n" + body

    raw = session(b"\x93FIRST quote\x94\n") + session(b"second session\n" * 4)
    path = tmp_path / "mega.txt"
    path.write_bytes(raw)
    written = smf.split_file(str(path), str(tmp_path))
    blob = "".join(item.read_text(encoding="utf-8") for item in written)
    assert "\ufffd" not in blob
    assert "\u201cFIRST quote\u201d" in blob
    assert path.read_bytes() == raw


def test_retire_original_preserves_bytes_and_never_clobbers_a_backup(tmp_path):
    src = tmp_path / "mega.txt"
    src.write_bytes(b"original-bytes-\xff")
    backup = smf._retire_original(src)
    assert backup == tmp_path / "mega.mega_backup"
    assert backup.read_bytes() == b"original-bytes-\xff"
    assert not src.exists()

    src.write_bytes(b"newer-original")
    second = smf._retire_original(src)
    assert second == tmp_path / "mega-2.mega_backup"
    assert second.read_bytes() == b"newer-original"
    assert backup.read_bytes() == b"original-bytes-\xff"
    assert not src.exists()


def test_split_file_rerun_writes_no_duplicates(tmp_path):
    """A second run over the same source reuses the files the first one wrote."""
    content = "Claude Code v1.0\n" + "first line\n" * 12 + "Claude Code v1.0\n" + "second\n" * 12
    path = tmp_path / "mega.txt"
    path.write_text(content, encoding="utf-8")
    (tmp_path / "out").mkdir()
    first = smf.split_file(str(path), str(tmp_path / "out"))
    second = smf.split_file(str(path), str(tmp_path / "out"))
    assert second == first
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == sorted(p.name for p in first)
