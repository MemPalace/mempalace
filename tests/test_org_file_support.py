"""Org-mode files should be treated as first-class text inputs."""

from pathlib import Path

from mempalace.convo_miner import CONVO_EXTENSIONS, scan_convos
from mempalace.entity_detector import PROSE_EXTENSIONS, READABLE_EXTENSIONS as DETECTOR_READABLE_EXTENSIONS, scan_for_detection
from mempalace.miner import READABLE_EXTENSIONS as MINER_READABLE_EXTENSIONS, scan_project


def test_org_is_readable_for_project_mining(tmp_path: Path):
    note = tmp_path / "notes.org"
    note.write_text("* Project notes\nThis org file should be mined.\n", encoding="utf-8")

    assert ".org" in MINER_READABLE_EXTENSIONS
    assert note in scan_project(str(tmp_path))


def test_org_is_prose_for_entity_detection(tmp_path: Path):
    note = tmp_path / "people.org"
    note.write_text("* People\nAlice met Bob. Alice and Bob discussed Alice.\n", encoding="utf-8")

    assert ".org" in PROSE_EXTENSIONS
    assert ".org" in DETECTOR_READABLE_EXTENSIONS
    assert note in scan_for_detection(str(tmp_path), max_files=10)


def test_org_is_supported_for_conversation_mining(tmp_path: Path):
    transcript = tmp_path / "chat.org"
    transcript.write_text("> How do I use this?\nYou can run mempalace mine.\n", encoding="utf-8")

    assert ".org" in CONVO_EXTENSIONS
    assert transcript in scan_convos(str(tmp_path))
