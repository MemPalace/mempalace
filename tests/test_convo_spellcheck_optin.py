"""Spell correction of user turns is opt-in and stamped (W1-09, D-01)."""

import json
from unittest.mock import patch

import chromadb
import pytest

from mempalace.config import MempalaceConfig
from mempalace.convo_miner import mine_convos
from mempalace.normalize import _messages_to_transcript, recording_transforms


def _export(*turns):
    """A chat export, so the turns go through the importer that used to
    correct them by default, not only the plain-text pass-through."""
    roles = ("user", "assistant")
    return json.dumps(
        {"messages": [{"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]}
    )


EXPORT = _export(
    "i knoe the question befor we start the review",
    "That is fine, we can begin with the outline.",
    "why does it matter for the palace layout",
    "It enables continuity across every session.",
)
CLEAN_EXPORT = _export(
    "what does the hall index point at",
    "Each hall points at the drawers filed under it.",
    "and the closets",
    "Closets hold the compressed index of a room.",
)


class _Speller:
    def __call__(self, word):
        return {"knoe": "know", "befor": "before"}.get(word, word)


@pytest.fixture
def speller():
    """Pretend autocorrect is installed, whatever this machine has."""
    with (
        patch("mempalace.spellcheck._get_speller", return_value=_Speller()),
        patch("mempalace.spellcheck._get_system_words", return_value=set()),
        patch("mempalace.spellcheck._load_known_names", return_value=set()),
    ):
        yield


def _mine(tmp_path, files=None):
    convo_dir = tmp_path / "convos"
    convo_dir.mkdir(exist_ok=True)
    for name, text in (files or {"chat.json": EXPORT}).items():
        (convo_dir / name).write_text(text, encoding="utf-8")
    palace_path = str(tmp_path / "palace")
    mine_convos(str(convo_dir), palace_path, wing="test")
    col = chromadb.PersistentClient(path=palace_path).get_collection("mempalace_drawers")
    rows = col.get(include=["documents", "metadatas"])
    return rows["documents"], rows["metadatas"]


def test_spellcheck_is_off_by_default(tmp_path, monkeypatch, speller):
    monkeypatch.delenv("MEMPALACE_CONVO_SPELLCHECK", raising=False)
    docs, metas = _mine(tmp_path)
    assert any("i knoe the question befor" in d for d in docs)
    assert not any("know the question before" in d for d in docs)
    assert all("transforms" not in m for m in metas)


def test_opted_in_spellcheck_stamps_every_drawer_of_a_file_it_changed(
    tmp_path, monkeypatch, speller
):
    monkeypatch.setenv("MEMPALACE_CONVO_SPELLCHECK", "1")
    docs, metas = _mine(tmp_path, {"chat.json": EXPORT, "clean.json": CLEAN_EXPORT})
    assert any("i know the question before" in d for d in docs)
    for meta in metas:
        if meta["source_file"].endswith("chat.json"):
            assert meta.get("transforms") == "spellcheck_user"
        else:
            assert "transforms" not in meta


def test_a_corrected_line_longer_than_a_chunk_still_stamps_its_drawers(
    tmp_path, monkeypatch, speller
):
    long_line = " ".join(["i knoe the question befor we start"] * 120)
    monkeypatch.setenv("MEMPALACE_CONVO_SPELLCHECK", "1")
    docs, metas = _mine(tmp_path, {"chat.json": _export(long_line, "ok, noted.")})
    assert len(docs) > 1
    assert not any("knoe" in d for d in docs)
    assert all(m.get("transforms") == "spellcheck_user" for m in metas)


def test_spellcheck_corrects_every_line_of_a_user_turn_and_no_assistant_text(speller):
    msgs = [
        ("user", "i knoe this\nand befor that"),
        ("assistant", "> quoted befor\nplain befor"),
    ]
    with recording_transforms(spellcheck=True) as run:
        result = _messages_to_transcript(msgs)
    assert "> i know this\nand before that" in result
    assert "> quoted befor\nplain befor" in result
    assert run.applied == {"spellcheck_user"}


def test_spellchecking_disabled_keeps_turns_and_records_nothing(speller):
    with recording_transforms(spellcheck=False) as run:
        result = _messages_to_transcript([("user", "i knoe"), ("assistant", "ok")])
    assert "> i knoe" in result
    assert run.applied == set()


def test_convo_spellcheck_setting(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMPALACE_CONVO_SPELLCHECK", raising=False)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    assert cfg.convo_spellcheck is False

    cfg._file_config = {"convo": {"spellcheck": True}}
    assert cfg.convo_spellcheck is True

    monkeypatch.setenv("MEMPALACE_CONVO_SPELLCHECK", "off")
    assert cfg.convo_spellcheck is False


@pytest.mark.parametrize("section", [True, None, "yes", ["spellcheck"]])
def test_convo_spellcheck_ignores_a_convo_section_that_is_not_an_object(
    section, monkeypatch, tmp_path
):
    monkeypatch.delenv("MEMPALACE_CONVO_SPELLCHECK", raising=False)
    cfg = MempalaceConfig(config_dir=str(tmp_path))
    cfg._file_config = {"convo": section}
    assert cfg.convo_spellcheck is False
