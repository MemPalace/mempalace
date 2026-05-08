"""Tests for mempalace.onboarding."""

import os
from unittest.mock import patch

from mempalace.onboarding import (
    DEFAULT_WINGS,
    _ask,
    _ask_mode,
    _ask_people,
    _ask_projects,
    _ask_wings,
    _auto_detect,
    _generate_aaak_bootstrap,
    _header,
    _hr,
    _warn_ambiguous,
    _yn,
    quick_setup,
    run_onboarding,
)

# Force UTF-8 for Windows (source file contains Unicode symbols like hearts/stars)
os.environ["PYTHONUTF8"] = "1"


# ── DEFAULT_WINGS ───────────────────────────────────────────────────────


def test_default_wings_has_expected_keys():
    assert "work" in DEFAULT_WINGS
    assert "personal" in DEFAULT_WINGS
    assert "combo" in DEFAULT_WINGS


def test_default_wings_work_has_projects():
    assert "projects" in DEFAULT_WINGS["work"]


def test_default_wings_personal_has_family():
    assert "family" in DEFAULT_WINGS["personal"]


def test_default_wings_combo_has_both():
    wings = DEFAULT_WINGS["combo"]
    assert "family" in wings
    assert "work" in wings


def test_default_wings_values_are_lists():
    for mode, wings in DEFAULT_WINGS.items():
        assert isinstance(wings, list), f"{mode} wings should be a list"
        assert len(wings) >= 3, f"{mode} should have at least 3 wings"


# ── _warn_ambiguous ─────────────────────────────────────────────────────


def test_warn_ambiguous_flags_common_words():
    people = [
        {"name": "Grace", "relationship": "friend"},
        {"name": "Riley", "relationship": "daughter"},
    ]
    result = _warn_ambiguous(people)
    assert "Grace" in result
    # Riley is not a common English word
    assert "Riley" not in result


def test_warn_ambiguous_empty_list():
    result = _warn_ambiguous([])
    assert result == []


def test_warn_ambiguous_no_ambiguous_names():
    people = [
        {"name": "Riley", "relationship": "daughter"},
        {"name": "Devon", "relationship": "friend"},
    ]
    result = _warn_ambiguous(people)
    assert result == []


def test_warn_ambiguous_multiple_hits():
    people = [
        {"name": "Grace", "relationship": "friend"},
        {"name": "May", "relationship": "aunt"},
        {"name": "Joy", "relationship": "sister"},
    ]
    result = _warn_ambiguous(people)
    assert "Grace" in result
    assert "May" in result
    assert "Joy" in result


# ── quick_setup ─────────────────────────────────────────────────────────


def test_quick_setup_creates_registry(tmp_path):
    registry = quick_setup(
        mode="personal",
        people=[{"name": "Riley", "relationship": "daughter", "context": "personal"}],
        projects=["MemPalace"],
        config_dir=tmp_path,
    )
    assert "Riley" in registry.people
    assert "MemPalace" in registry.projects
    assert registry.mode == "personal"


def test_quick_setup_work_mode(tmp_path):
    registry = quick_setup(
        mode="work",
        people=[{"name": "Alice", "relationship": "colleague", "context": "work"}],
        projects=["Acme"],
        config_dir=tmp_path,
    )
    assert registry.mode == "work"
    assert "Alice" in registry.people
    assert "Acme" in registry.projects


def test_quick_setup_empty(tmp_path):
    registry = quick_setup(mode="personal", people=[], config_dir=tmp_path)
    assert len(registry.people) == 0
    assert len(registry.projects) == 0


def test_quick_setup_saves_to_disk(tmp_path):
    quick_setup(
        mode="personal",
        people=[{"name": "Riley", "relationship": "daughter", "context": "personal"}],
        config_dir=tmp_path,
    )
    assert (tmp_path / "entity_registry.json").exists()


# ── _generate_aaak_bootstrap ───────────────────────────────────────────


def test_generate_aaak_bootstrap_creates_files(tmp_path):
    people = [
        {"name": "Riley", "relationship": "daughter", "context": "personal"},
        {"name": "Devon", "relationship": "friend", "context": "personal"},
    ]
    projects = ["MemPalace"]
    wings = ["family", "creative"]
    _generate_aaak_bootstrap(people, projects, wings, "personal", config_dir=tmp_path)

    assert (tmp_path / "aaak_entities.md").exists()
    assert (tmp_path / "critical_facts.md").exists()


def test_generate_aaak_bootstrap_entities_content(tmp_path):
    people = [{"name": "Riley", "relationship": "daughter", "context": "personal"}]
    projects = ["MemPalace"]
    wings = ["family"]
    _generate_aaak_bootstrap(people, projects, wings, "personal", config_dir=tmp_path)

    content = (tmp_path / "aaak_entities.md").read_text(encoding="utf-8")
    assert "Riley" in content
    assert "RIL" in content  # entity code
    assert "MemPalace" in content


def test_generate_aaak_bootstrap_facts_content(tmp_path):
    people = [
        {"name": "Alice", "relationship": "colleague", "context": "work"},
    ]
    projects = ["Acme"]
    wings = ["projects"]
    _generate_aaak_bootstrap(people, projects, wings, "work", config_dir=tmp_path)

    content = (tmp_path / "critical_facts.md").read_text(encoding="utf-8")
    assert "Alice" in content
    assert "Acme" in content
    assert "work" in content.lower()


def test_generate_aaak_bootstrap_empty_people(tmp_path):
    _generate_aaak_bootstrap([], [], ["general"], "personal", config_dir=tmp_path)
    assert (tmp_path / "aaak_entities.md").exists()
    assert (tmp_path / "critical_facts.md").exists()


def test_generate_aaak_bootstrap_collision(tmp_path):
    """Two people with same 3-letter code get different codes."""
    people = [
        {"name": "Alice", "relationship": "friend", "context": "work"},
        {"name": "Alison", "relationship": "coworker", "context": "work"},
    ]
    _generate_aaak_bootstrap(people, [], ["work"], "work", config_dir=tmp_path)
    content = (tmp_path / "aaak_entities.md").read_text(encoding="utf-8")
    assert "ALI" in content
    assert "ALIS" in content


def test_generate_aaak_bootstrap_no_relationship(tmp_path):
    """Person without relationship string still generates entry."""
    people = [{"name": "Bob", "context": "work"}]
    _generate_aaak_bootstrap(people, [], ["work"], "work", config_dir=tmp_path)
    content = (tmp_path / "aaak_entities.md").read_text(encoding="utf-8")
    assert "BOB=Bob" in content


# ── _hr, _header ──────────────────────────────────────────────────────


def test_hr_prints_line(capsys):
    _hr()
    out = capsys.readouterr().out
    assert "─" in out


def test_header_prints_banner(capsys):
    _header("Test Title")
    out = capsys.readouterr().out
    assert "Test Title" in out
    assert "=" in out


# ── _ask ──────────────────────────────────────────────────────────────


def test_ask_with_default_uses_default():
    with patch("builtins.input", return_value=""):
        result = _ask("prompt", default="fallback")
    assert result == "fallback"


def test_ask_with_default_uses_input():
    with patch("builtins.input", return_value="custom"):
        result = _ask("prompt", default="fallback")
    assert result == "custom"


def test_ask_no_default():
    with patch("builtins.input", return_value="answer"):
        result = _ask("prompt")
    assert result == "answer"


# ── _yn ───────────────────────────────────────────────────────────────


def test_yn_default_yes_empty_input():
    with patch("builtins.input", return_value=""):
        assert _yn("continue?") is True


def test_yn_default_no_empty_input():
    with patch("builtins.input", return_value=""):
        assert _yn("continue?", default="n") is False


def test_yn_explicit_yes():
    with patch("builtins.input", return_value="yes"):
        assert _yn("continue?", default="n") is True


def test_yn_explicit_no():
    with patch("builtins.input", return_value="no"):
        assert _yn("continue?") is False


# ── _ask_mode ─────────────────────────────────────────────────────────


def test_ask_mode_work():
    with patch("builtins.input", return_value="1"):
        assert _ask_mode() == "work"


def test_ask_mode_personal():
    with patch("builtins.input", return_value="2"):
        assert _ask_mode() == "personal"


def test_ask_mode_combo():
    with patch("builtins.input", return_value="3"):
        assert _ask_mode() == "combo"


def test_ask_mode_retries_on_bad_input():
    with patch("builtins.input", side_effect=["x", "bad", "1"]):
        assert _ask_mode() == "work"


# ── _ask_people ───────────────────────────────────────────────────────


def test_ask_people_personal_mode():
    with patch("builtins.input", side_effect=["Alice, daughter", "", "done"]):
        people, aliases = _ask_people("personal")
    assert len(people) == 1
    assert people[0]["name"] == "Alice"
    assert people[0]["relationship"] == "daughter"


def test_ask_people_work_mode():
    with patch("builtins.input", side_effect=["Bob, manager", "", "done"]):
        people, aliases = _ask_people("work")
    assert len(people) == 1
    assert people[0]["name"] == "Bob"
    assert people[0]["context"] == "work"


def test_ask_people_combo_mode():
    with patch(
        "builtins.input",
        side_effect=[
            "Alice, daughter",
            "",
            "done",  # personal
            "Bob, boss",
            "done",  # work
        ],
    ):
        people, aliases = _ask_people("combo")
    assert len(people) == 2


def test_ask_people_with_nickname():
    with patch("builtins.input", side_effect=["Alice, daughter", "Ali", "done"]):
        people, aliases = _ask_people("personal")
    assert aliases == {"Ali": "Alice"}


def test_ask_people_empty_name_skipped():
    with patch("builtins.input", side_effect=["", "done"]):
        people, aliases = _ask_people("personal")
    assert len(people) == 0


# ── _ask_projects ─────────────────────────────────────────────────────


def test_ask_projects_personal_returns_empty():
    result = _ask_projects("personal")
    assert result == []


def test_ask_projects_work_mode():
    with patch("builtins.input", side_effect=["Acme", "BigCo", "done"]):
        result = _ask_projects("work")
    assert result == ["Acme", "BigCo"]


def test_ask_projects_empty_entry_stops():
    with patch("builtins.input", side_effect=["Acme", ""]):
        result = _ask_projects("work")
    assert result == ["Acme"]


# ── _ask_wings ────────────────────────────────────────────────────────


def test_ask_wings_accept_defaults():
    with patch("builtins.input", return_value=""):
        result = _ask_wings("work")
    assert result == DEFAULT_WINGS["work"]


def test_ask_wings_custom():
    with patch("builtins.input", return_value="alpha, beta, gamma"):
        result = _ask_wings("personal")
    assert result == ["alpha", "beta", "gamma"]


# ── _auto_detect ──────────────────────────────────────────────────────


def test_auto_detect_no_files(tmp_path):
    result = _auto_detect(str(tmp_path), [])
    assert result == []


def test_auto_detect_filters_known(tmp_path):
    known = [{"name": "Alice"}]
    fake_detected = {
        "people": [
            {"name": "Alice", "confidence": 0.9, "signals": ["test"]},
            {"name": "Bob", "confidence": 0.8, "signals": ["test"]},
        ],
        "projects": [],
        "uncertain": [],
    }
    with (
        patch("mempalace.onboarding.scan_for_detection", return_value=["file.txt"]),
        patch("mempalace.onboarding.detect_entities", return_value=fake_detected),
    ):
        result = _auto_detect(str(tmp_path), known)
    names = [p["name"] for p in result]
    assert "Alice" not in names
    assert "Bob" in names


def test_auto_detect_filters_low_confidence(tmp_path):
    fake_detected = {
        "people": [{"name": "Bob", "confidence": 0.5, "signals": ["test"]}],
        "projects": [],
        "uncertain": [],
    }
    with (
        patch("mempalace.onboarding.scan_for_detection", return_value=["file.txt"]),
        patch("mempalace.onboarding.detect_entities", return_value=fake_detected),
    ):
        result = _auto_detect(str(tmp_path), [])
    assert len(result) == 0


def test_auto_detect_handles_exception(tmp_path):
    with patch("mempalace.onboarding.scan_for_detection", side_effect=Exception("boom")):
        result = _auto_detect(str(tmp_path), [])
    assert result == []


# ── run_onboarding ────────────────────────────────────────────────────


def test_run_onboarding_basic_flow(tmp_path):
    """Test the full onboarding flow with minimal mocking."""
    with (
        patch("mempalace.onboarding._ask_mode", return_value="work"),
        patch(
            "mempalace.onboarding._ask_people",
            return_value=([{"name": "Bob", "relationship": "boss", "context": "work"}], {}),
        ),
        patch("mempalace.onboarding._ask_projects", return_value=["Acme"]),
        patch("mempalace.onboarding._ask_wings", return_value=["projects", "team"]),
        patch("mempalace.onboarding._yn", return_value=False),
        patch("mempalace.onboarding._warn_ambiguous", return_value=[]),
    ):
        registry = run_onboarding(directory=".", config_dir=tmp_path, auto_detect=False)
    assert "Bob" in registry.people
    assert "Acme" in registry.projects


def test_run_onboarding_with_ambiguous_names(tmp_path):
    """Onboarding prints a warning for ambiguous names."""
    with (
        patch("mempalace.onboarding._ask_mode", return_value="personal"),
        patch(
            "mempalace.onboarding._ask_people",
            return_value=([{"name": "Grace", "relationship": "friend", "context": "personal"}], {}),
        ),
        patch("mempalace.onboarding._ask_projects", return_value=[]),
        patch("mempalace.onboarding._ask_wings", return_value=["family"]),
        patch("mempalace.onboarding._yn", return_value=False),
    ):
        registry = run_onboarding(directory=".", config_dir=tmp_path, auto_detect=False)
    assert "Grace" in registry.people
