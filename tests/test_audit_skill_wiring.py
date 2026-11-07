"""The audit-and-repair session is reachable from every harness the repo ships."""

from pathlib import Path

from mempalace.instructions_cli import AVAILABLE, INSTRUCTIONS_DIR

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_audit_instructions_exist_and_cover_the_repair_session():
    assert "audit" in AVAILABLE
    text = (INSTRUCTIONS_DIR / "audit.md").read_text(encoding="utf-8")
    assert "mempalace audit --json" in text
    # One structured question per layer, and the safety rules that bound it.
    for anchor in (
        "wing_drift",
        "room_drift",
        "wing_prefix_pairs",
        "tiny_wings",
        "tunnels prune",
        "artifact_sample",
        "one_off_sample",
        "mempalace_update_drawer",
        "mempalace_follow_tunnels",
        "--prune-spellings",
        "wings split",
        "tunnels propose",
        "kg normalize",
        "hallways --rebuild",
        "mempalace_diary_write",
        "One question at a time",
        "Moves over deletions",
    ):
        assert anchor in text, anchor
    assert "mempalace repair" in text  # explicitly told not to run it


def test_parser_accepts_instructions_audit(monkeypatch):
    import mempalace.cli as cli

    seen = {}
    monkeypatch.setattr(cli, "cmd_instructions", lambda args: seen.update(vars(args)))
    monkeypatch.setattr("sys.argv", ["mempalace", "instructions", "audit"])
    cli.main()
    assert seen["name"] == "audit"


def test_every_harness_points_at_the_audit_instructions():
    for rel in (
        "skills/mempalace/SKILL.md",
        ".claude-plugin/skills/mempalace/SKILL.md",
        ".antigravity-plugin/skills/mempalace/SKILL.md",
        ".codex-plugin/skills/audit/SKILL.md",
        ".claude-plugin/commands/audit.md",
        "commands/mempalace-audit.md",
    ):
        path = REPO_ROOT / rel
        assert path.is_file(), rel
        assert not path.is_symlink(), rel
        assert "audit" in path.read_text(encoding="utf-8"), rel
