"""Keep the generic untangle-codebase skill well-formed and its helper working."""

import importlib.util
import json
import re
from pathlib import Path

import yaml

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "untangle-codebase"
SKILL_FILE = SKILL_DIR / "SKILL.md"
MEASURE = SKILL_DIR / "scripts" / "measure.py"

REQUIRED_HEADINGS = [
    "## When to use",
    "## The loop",
    "## 1. Measure first",
    "## 2. Pick one bounded target",
    "## 3. Pin behaviour before changing it",
    "## 4. Change in small, reviewed steps",
    "## 5. Measure again and report",
    "## Worked example",
    "## Guardrails",
]
# Absolute local paths and home-relative paths do not belong in a generic skill.
LOCAL_PATH = re.compile(r"(?<![\w.])(/(?:home|Users|root|tmp|var|opt|mnt|srv)/|~/|[A-Za-z]:\\)")


def _split(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML front matter"
    end = text.find("\n---\n", 4)
    assert end != -1, "front matter is not closed"
    return yaml.safe_load(text[4:end]), text[end + 5 :]


def _load_measure():
    spec = importlib.util.spec_from_file_location("untangle_measure", MEASURE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_front_matter_has_name_matching_directory_and_trigger_description():
    meta, body = _split(SKILL_FILE.read_text(encoding="utf-8"))
    assert meta["name"] == SKILL_DIR.name == "untangle-codebase"
    description = meta["description"]
    assert isinstance(description, str) and 80 <= len(description) <= 1024
    assert "Use when" in description
    assert body.strip()


def test_required_sections_present_in_order():
    _, body = _split(SKILL_FILE.read_text(encoding="utf-8"))
    positions = [body.find(heading) for heading in REQUIRED_HEADINGS]
    assert all(pos != -1 for pos in positions), dict(zip(REQUIRED_HEADINGS, positions))
    assert positions == sorted(positions), "required sections are out of order"


def test_skill_and_helper_contain_no_absolute_local_paths():
    for path in (SKILL_FILE, MEASURE):
        offenders = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if LOCAL_PATH.search(line)
        ]
        assert not offenders, f"{path.name} has local paths: {offenders}"


def test_skill_links_stay_inside_the_skill_directory():
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert "](../" not in text
    for match in re.findall(r"scripts/[\w./-]+\.py", text):
        assert (SKILL_DIR / match).is_file(), f"SKILL.md references missing {match}"


def test_measure_reports_largest_files_and_duplicates_without_writing(tmp_path, capsys):
    measure = _load_measure()
    shared = "\n".join(f"value_{i} = compute({i})" for i in range(6))
    (tmp_path / "a.py").write_text(shared + "\nprint('a')\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("x = 1\ny = 2\n" + shared + "\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "skipped.py").write_text(shared, encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00")
    before = sorted(p.name for p in tmp_path.rglob("*"))

    assert measure.main([str(tmp_path), "--json", "--dup-lines", "6"]) == 0
    report = json.loads(capsys.readouterr().out)

    assert report["files_scanned"] == 2
    assert report["largest_files"][0]["path"] == "b.py"
    assert report["duplicate_blocks"] >= 1
    assert report["duplicate_samples"][0]["files"] == ["a.py", "b.py"]
    assert sorted(p.name for p in tmp_path.rglob("*")) == before
