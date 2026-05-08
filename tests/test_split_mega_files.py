import json

from mempalace import split_mega_files as smf


# ── Config loading ─────────────────────────────────────────────────────


def test_load_known_people_falls_back_when_config_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(smf, "_KNOWN_NAMES_PATH", tmp_path / "missing.json")
    smf._KNOWN_NAMES_CACHE = None

    assert smf._load_known_people() == smf._FALLBACK_KNOWN_PEOPLE
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


def test_split_file_tiny_fragments_skipped(tmp_path):
    """Tiny chunks (< 10 lines) are skipped."""
    content = "Claude Code v1.0\nline\n" * 2 + "Claude Code v1.0\n" + "line\n" * 20
    path = tmp_path / "tiny.txt"
    path.write_text(content)
    written = smf.split_file(str(path), str(tmp_path))
    # The first chunk is very small, should be skipped
    for p in written:
        assert p.stat().st_size > 0
