"""Contracts for skills shipped by the Claude marketplace package."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_SKILLS = REPO_ROOT / "skills"
CLAUDE_SKILLS = REPO_ROOT / ".claude-plugin" / "skills"
EXPECTED_SKILLS = {"mempalace", "mempalace-recall", "mempalace-task"}


def test_claude_marketplace_bundles_canonical_skills_without_drift():
    """The nested marketplace source must carry every canonical skill exactly."""
    canonical = {path.parent.name: path for path in CANONICAL_SKILLS.glob("*/SKILL.md")}
    bundled = {path.parent.name: path for path in CLAUDE_SKILLS.glob("*/SKILL.md")}

    assert set(canonical) == EXPECTED_SKILLS
    assert set(bundled) == EXPECTED_SKILLS
    for name in EXPECTED_SKILLS:
        assert bundled[name].read_bytes() == canonical[name].read_bytes(), (
            f"Claude marketplace skill {name!r} drifted from skills/{name}/SKILL.md"
        )
