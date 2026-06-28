"""Tests for no-LLM structural entity extraction."""
from mempalace.entities import entities_metadata, extract_structural_entities


def test_extracts_code_symbols_paths_urls():
    text = (
        "We patched `_extract_authored_at` in rag/convo_miner.py so MemoryStack and "
        "ChromaBackend agree. See module.func and pkg.Class.method, plus do_thing_now. "
        "Ref https://github.com/MemPalace/mempalace/pull/1890 for details."
    )
    ents = set(extract_structural_entities(text))
    assert "_extract_authored_at" in ents
    assert "rag/convo_miner.py" in ents
    assert "MemoryStack" in ents
    assert "ChromaBackend" in ents
    assert "module.func" in ents
    assert "pkg.Class.method" in ents
    assert "do_thing_now" in ents
    assert any(e.startswith("https://github.com/MemPalace") for e in ents)


def test_excludes_prose_noise():
    text = "This is a normal sentence, e.g. with i.e. abbreviations and version 1.2.3 here."
    ents = extract_structural_entities(text)
    # No plain prose words, no "e.g"/"i.e", no bare version numbers.
    assert ents == []


def test_ranked_by_frequency_then_order():
    text = "alpha_one alpha_one alpha_one beta_two beta_two gamma_three"
    ents = extract_structural_entities(text)
    assert ents[:3] == ["alpha_one", "beta_two", "gamma_three"]


def test_dedup_case_insensitive_keeps_first_form():
    text = "`MemoryStack` and memorystack and MEMORYSTACK"
    ents = extract_structural_entities(text)
    assert ents.count("MemoryStack") == 1
    assert ents == ["MemoryStack"]


def test_respects_max_entities():
    text = " ".join(f"sym_{i}_x" for i in range(50))
    assert len(extract_structural_entities(text, max_entities=10)) == 10


def test_metadata_is_semicolon_joined():
    text = "`one_thing` and TwoThing"
    md = entities_metadata(text)
    assert md == "one_thing;TwoThing"
    assert entities_metadata("") == ""
    assert entities_metadata("just plain prose with nothing structural") == ""
