"""Contract tests for canonical Hermes conversation exports."""

import hashlib
import importlib
import json

import pytest


def _module():
    return importlib.import_module("mempalace.normalized_conversations")


def _write_pair(
    tmp_path,
    *,
    transcript=None,
    room="communication",
    authored_from="2026-07-29T23:55:00Z",
    authored_to="2026-07-30T00:05:00Z",
):
    transcript_path = tmp_path / "session.md"
    transcript_text = transcript or (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> teh exact user text must stay teh same\n"
        "The assistant answer also stays byte-for-byte intact.\n"
    )
    transcript_path.write_text(transcript_text)
    sidecar = {
        "schema": "mempalace-normalized-conversation/v1",
        "room": room,
        "authored_from": authored_from,
        "authored_to": authored_to,
        "source_fingerprint": f"sha256:{hashlib.sha256(b'source rows').hexdigest()}",
        "transformations": ("hermes-compaction-dedup;hermes-synthetic-filter;secret-redaction"),
        "exporter_version": "1.0.0",
        "hermes_profile": "amber",
        "hermes_session_id": "session-123",
        "hermes_source": "telegram",
    }
    sidecar_path = tmp_path / "session.md.meta.json"
    sidecar_path.write_text(json.dumps(sidecar))
    return transcript_path, sidecar_path, transcript_text, sidecar


def test_one_exchange_preserves_exact_transformed_text_without_normalization(tmp_path):
    normalized = _module()
    transcript_path, _, transcript_text, _ = _write_pair(tmp_path)

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )
    chunks = normalized.chunk_normalized_conversation(contract, chunk_size=800)

    assert len(chunks) == 1
    assert chunks[0]["content"] == transcript_text
    assert "teh exact user text must stay teh same" in chunks[0]["content"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_exchange_chunks_reconstruct_exact_transcript_line_endings(tmp_path, newline):
    normalized = _module()
    transcript = newline.join(
        [
            '<!-- mempalace-exchange {"messages":['
            '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
            '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->',
            "> exact user text",
            "Assistant answer.",
            "",
            '<!-- mempalace-exchange {"messages":['
            '{"id":"43","role":"user","timestamp":"2026-07-30T00:04:00Z"},'
            '{"id":"44","role":"assistant","timestamp":"2026-07-30T00:05:00Z"}]} -->',
            "> second user text",
            "Second assistant answer.",
            "",
        ]
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)
    transcript_path.write_bytes(transcript.encode())

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )
    chunks = normalized.chunk_normalized_conversation(contract, chunk_size=37)

    assert "".join(chunk["content"] for chunk in chunks) == transcript


def test_assistant_markdown_blockquote_remains_verbatim(tmp_path):
    normalized = _module()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> user asks about the quoted text\n"
        "Assistant context:\n"
        "> this is an assistant Markdown blockquote\n"
        "Assistant conclusion.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )

    assert (
        "".join(
            chunk["content"]
            for chunk in normalized.chunk_normalized_conversation(contract, chunk_size=53)
        )
        == transcript
    )


def test_multi_day_exchange_bounds_are_preserved_per_chunk(tmp_path):
    normalized = _module()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> first question with enough content to keep\n"
        "First answer with enough content to keep.\n"
        '<!-- mempalace-exchange {"messages":['
        '{"id":"43","role":"user","timestamp":"2026-07-30T00:04:00Z"},'
        '{"id":"44","role":"assistant","timestamp":"2026-07-30T00:05:00Z"}]} -->\n'
        "> second question with enough content to keep\n"
        "Second answer with enough content to keep.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )
    chunks = normalized.chunk_normalized_conversation(contract, chunk_size=800)

    assert [chunk["authored_from"] for chunk in chunks] == [
        "2026-07-29T23:55:00Z",
        "2026-07-30T00:04:00Z",
    ]
    assert [chunk["authored_to"] for chunk in chunks] == [
        "2026-07-29T23:56:00Z",
        "2026-07-30T00:05:00Z",
    ]
    assert [chunk["message_from"] for chunk in chunks] == ["41", "43"]
    assert [chunk["message_to"] for chunk in chunks] == ["42", "44"]


def test_multi_assistant_exchange_has_ordered_unique_message_provenance(tmp_path):
    normalized = _module()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"51","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"52","role":"assistant","timestamp":"2026-07-29T23:55:30Z"},'
        '{"id":"53","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> one user message with enough content\n"
        "First assistant body.\n"
        "Second assistant body.\n"
    )
    transcript_path, _, transcript_text, _ = _write_pair(tmp_path, transcript=transcript)

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )
    chunks = normalized.chunk_normalized_conversation(contract, chunk_size=800)

    assert len(chunks) == 1
    assert chunks[0]["content"] == transcript_text
    assert chunks[0]["message_from"] == "51"
    assert chunks[0]["message_to"] == "53"
    assert chunks[0]["message_count"] == 3


@pytest.mark.parametrize(
    "messages,match",
    [
        (
            [
                {"id": "1", "role": "assistant", "timestamp": "2026-07-29T23:55:00Z"},
                {"id": "2", "role": "user", "timestamp": "2026-07-29T23:56:00Z"},
            ],
            "first message.*user",
        ),
        (
            [
                {"id": "1", "role": "user", "timestamp": "2026-07-29T23:55:00Z"},
                {"id": "1", "role": "assistant", "timestamp": "2026-07-29T23:56:00Z"},
            ],
            "unique",
        ),
    ],
)
def test_malformed_exchange_provenance_fails_closed(tmp_path, messages, match):
    normalized = _module()
    transcript = (
        f"<!-- mempalace-exchange {json.dumps({'messages': messages})} -->\n"
        "> user message with enough content\n"
        "Assistant answer with enough content.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    with pytest.raises(normalized.NormalizedConversationError, match=match):
        normalized.load_normalized_conversation(transcript_path, tmp_path, extract_mode="exchange")


def test_oversized_exchange_provenance_fails_closed(tmp_path):
    normalized = _module()
    messages = [
        {
            "id": f"message-{i}-" + ("x" * 128),
            "role": "user" if i == 0 else "assistant",
            "timestamp": f"2026-07-29T23:55:{i:02d}Z",
        }
        for i in range(120)
    ]
    transcript = (
        f"<!-- mempalace-exchange {json.dumps({'messages': messages})} -->\n"
        "> user message with enough content\n"
        "Assistant answer with enough content.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    with pytest.raises(normalized.NormalizedConversationError, match="envelope.*too large"):
        normalized.load_normalized_conversation(transcript_path, tmp_path, extract_mode="exchange")


def test_sidecar_only_change_changes_composite_source_version(tmp_path):
    normalized = _module()
    transcript_path, sidecar_path, _, sidecar = _write_pair(tmp_path)
    first = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )

    sidecar["room"] = "presence"
    sidecar_path.write_text(json.dumps(sidecar))
    second = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )

    assert first.source_version != second.source_version


def test_general_mode_refuses_normalized_hermes_contract(tmp_path):
    normalized = _module()
    transcript_path, _, _, _ = _write_pair(tmp_path)

    with pytest.raises(
        normalized.NormalizedConversationError,
        match="extract_mode=general",
    ):
        normalized.load_normalized_conversation(transcript_path, tmp_path, extract_mode="general")


def test_exchange_bounds_use_min_and_max_when_timestamps_move_backward(tmp_path):
    normalized = _module()
    messages = [
        {"id": "1", "role": "user", "timestamp": "2026-07-29T23:56:00Z"},
        {"id": "2", "role": "assistant", "timestamp": "2026-07-29T23:55:00Z"},
    ]
    transcript = (
        f"<!-- mempalace-exchange {json.dumps({'messages': messages})} -->\n"
        "> user message with enough content\n"
        "Assistant answer with enough content.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    contract = normalized.load_normalized_conversation(
        transcript_path, tmp_path, extract_mode="exchange"
    )
    chunks = normalized.chunk_normalized_conversation(contract, chunk_size=800)

    assert chunks[0]["authored_from"] == "2026-07-29T23:55:00Z"
    assert chunks[0]["authored_to"] == "2026-07-29T23:56:00Z"
    assert chunks[0]["message_from"] == "1"
    assert chunks[0]["message_to"] == "2"


def test_message_ids_must_be_unique_across_the_transcript(tmp_path):
    normalized = _module()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"1","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"2","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> first user message\n"
        "First answer.\n"
        '<!-- mempalace-exchange {"messages":['
        '{"id":"1","role":"user","timestamp":"2026-07-30T00:04:00Z"},'
        '{"id":"3","role":"assistant","timestamp":"2026-07-30T00:05:00Z"}]} -->\n'
        "> second user message\n"
        "Second answer.\n"
    )
    transcript_path, _, _, _ = _write_pair(tmp_path, transcript=transcript)

    with pytest.raises(normalized.NormalizedConversationError, match="across the transcript"):
        normalized.load_normalized_conversation(
            transcript_path,
            tmp_path,
            extract_mode="exchange",
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda sidecar: sidecar.update(schema="unknown/v9"), "schema"),
        (lambda sidecar: sidecar.update(room="../communication"), "room"),
        (lambda sidecar: sidecar.update(room="decisions"), "room"),
        (lambda sidecar: sidecar.update(extra={"nested": True}), "field"),
    ],
)
def test_invalid_sidecar_contract_fails_closed(tmp_path, mutate, match):
    normalized = _module()
    transcript_path, sidecar_path, _, sidecar = _write_pair(tmp_path)
    mutate(sidecar)
    sidecar_path.write_text(json.dumps(sidecar))

    with pytest.raises(normalized.NormalizedConversationError, match=match):
        normalized.load_normalized_conversation(transcript_path, tmp_path, extract_mode="exchange")


def test_symlink_sidecar_fails_closed(tmp_path):
    normalized = _module()
    transcript_path, sidecar_path, _, sidecar = _write_pair(tmp_path)
    target = tmp_path / "outside.json"
    target.write_text(json.dumps(sidecar))
    sidecar_path.unlink()
    sidecar_path.symlink_to(target)

    with pytest.raises(normalized.NormalizedConversationError, match="symlink"):
        normalized.load_normalized_conversation(transcript_path, tmp_path, extract_mode="exchange")


def test_conversation_scan_does_not_treat_sidecar_as_a_source(tmp_path):
    from mempalace.convo_miner import scan_convos

    transcript_path, _, _, _ = _write_pair(tmp_path)

    assert scan_convos(str(tmp_path)) == [transcript_path]


def test_miner_stamps_normalized_room_chronology_and_source_version(tmp_path):
    import chromadb

    from mempalace.convo_miner import mine_convos

    source = tmp_path / "source"
    source.mkdir()
    transcript_path, _, _, _ = _write_pair(source)
    palace = tmp_path / "palace"

    mine_convos(str(source), str(palace), wing="wing_amber")

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    rows = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["documents", "metadatas"],
    )
    assert len(rows["ids"]) == 1
    assert "teh exact user text must stay teh same" in rows["documents"][0]
    metadata = rows["metadatas"][0]
    assert metadata["wing"] == "wing_amber"
    assert metadata["room"] == "communication"
    assert metadata["authored_from"] == "2026-07-29T23:55:00Z"
    assert metadata["authored_to"] == "2026-07-29T23:56:00Z"
    assert metadata["message_from"] == "41"
    assert metadata["message_to"] == "42"
    assert metadata["source_version"].startswith("sha256:")
    assert metadata["source_chunk_count"] == 1


def test_sidecar_only_change_rebuilds_stable_source_path(tmp_path, capsys):
    import chromadb

    from mempalace.convo_miner import mine_convos

    source = tmp_path / "source"
    source.mkdir()
    transcript_path, sidecar_path, _, sidecar = _write_pair(source)
    palace = tmp_path / "palace"
    mine_convos(str(source), str(palace), wing="wing_amber")
    capsys.readouterr()

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    first = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )
    first_version = first["metadatas"][0]["source_version"]
    del collection

    sidecar["room"] = "presence"
    sidecar_path.write_text(json.dumps(sidecar))
    mine_convos(str(source), str(palace), wing="wing_amber")
    output = capsys.readouterr().out

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    second = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )
    assert "Files skipped (already filed): 0" in output
    assert {meta["room"] for meta in second["metadatas"]} == {"presence"}
    assert {meta["source_version"] for meta in second["metadatas"]} != {first_version}


def test_complete_normalized_source_skips_unchanged_pair(tmp_path, capsys):
    from mempalace.convo_miner import mine_convos

    source = tmp_path / "source"
    source.mkdir()
    _write_pair(source)
    palace = tmp_path / "palace"

    mine_convos(str(source), str(palace), wing="wing_amber")
    capsys.readouterr()
    mine_convos(str(source), str(palace), wing="wing_amber")
    output = capsys.readouterr().out

    assert "Files skipped (already filed): 1" in output


def test_unchanged_pair_skips_exchange_materialization(tmp_path, monkeypatch):
    import mempalace.convo_miner as convo_miner

    source = tmp_path / "source"
    source.mkdir()
    _write_pair(source)
    palace = tmp_path / "palace"
    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")

    def unexpected_materialization(*_args, **_kwargs):
        raise AssertionError("unchanged source should not parse exchanges")

    monkeypatch.setattr(
        convo_miner,
        "load_normalized_conversation",
        unexpected_materialization,
    )

    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")


def test_generation_recipe_change_rebuilds_without_reusing_drawer_ids(tmp_path, monkeypatch):
    import chromadb

    import mempalace.convo_miner as convo_miner

    source = tmp_path / "source"
    source.mkdir()
    transcript_path, _, _, _ = _write_pair(source)
    palace = tmp_path / "palace"
    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    before = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )
    old_ids = set(before["ids"])
    monkeypatch.setattr(
        convo_miner,
        "NORMALIZE_VERSION",
        convo_miner.NORMALIZE_VERSION + 1,
    )

    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")
    after = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )

    assert old_ids.isdisjoint(after["ids"])
    assert {metadata["normalize_version"] for metadata in after["metadatas"]} == {
        convo_miner.NORMALIZE_VERSION
    }


def test_same_source_reconciles_independently_in_two_wings(tmp_path, capsys):
    import chromadb

    from mempalace.convo_miner import mine_convos, normalized_conversation_delta

    source = tmp_path / "source"
    source.mkdir()
    transcript_path, _, _, _ = _write_pair(source)
    palace = tmp_path / "palace"

    mine_convos(str(source), str(palace), wing="wing_amber")
    capsys.readouterr()
    mine_convos(str(source), str(palace), wing="wing_daphne")
    capsys.readouterr()

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    rows = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )
    assert {metadata["wing"] for metadata in rows["metadatas"]} == {
        "wing_amber",
        "wing_daphne",
    }
    for wing in ("wing_amber", "wing_daphne"):
        report = normalized_conversation_delta(str(source), str(palace), wing=wing)
        assert report["unchanged"] == [str(transcript_path.resolve())]
        assert report["new"] == []
        assert report["changed"] == []


def test_miner_general_mode_refuses_normalized_pair(tmp_path):
    from mempalace.convo_miner import mine_convos

    source = tmp_path / "source"
    source.mkdir()
    _write_pair(source)

    with pytest.raises(
        _module().NormalizedConversationError,
        match="extract_mode=general",
    ):
        mine_convos(
            str(source),
            str(tmp_path / "palace"),
            wing="wing_amber",
            extract_mode="general",
        )


def test_normalized_rebuild_treats_purge_failure_as_fatal(tmp_path, monkeypatch):
    import mempalace.convo_miner as convo_miner

    source = tmp_path / "source"
    source.mkdir()
    _, sidecar_path, _, sidecar = _write_pair(source)
    palace = tmp_path / "palace"
    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")
    sidecar["room"] = "presence"
    sidecar_path.write_text(json.dumps(sidecar))

    def fail_purge(*_args, **_kwargs):
        raise RuntimeError("purge unavailable")

    monkeypatch.setattr("mempalace.backends.chroma.ChromaCollection.delete", fail_purge)
    with pytest.raises(RuntimeError, match="purge unavailable"):
        convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")


def test_failed_target_stage_preserves_complete_generation_and_repairs(tmp_path, monkeypatch):
    import chromadb

    import mempalace.convo_miner as convo_miner
    from mempalace.backends.chroma import ChromaCollection

    source = tmp_path / "source"
    source.mkdir()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> preserve the complete generation while staging\n"
        + ("Assistant evidence remains exact. " * 160)
        + "\n"
    )
    transcript_path, sidecar_path, _, sidecar = _write_pair(source, transcript=transcript)
    palace = tmp_path / "palace"
    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")

    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    before = collection.get(
        where={
            "$and": [
                {"source_file": str(transcript_path.resolve())},
                {"wing": "wing_amber"},
            ]
        },
        include=["metadatas"],
    )
    old_version = before["metadatas"][0]["source_version"]
    old_count = len(before["ids"])

    sidecar["room"] = "presence"
    sidecar_path.write_text(json.dumps(sidecar))
    original_upsert = ChromaCollection.upsert
    calls = 0

    def fail_second_batch(self, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("target stage interrupted")
        return original_upsert(self, **kwargs)

    monkeypatch.setattr(convo_miner, "DRAWER_UPSERT_BATCH_SIZE", 1)
    monkeypatch.setattr(ChromaCollection, "upsert", fail_second_batch)
    with pytest.raises(RuntimeError, match="target stage interrupted"):
        convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")

    interrupted = collection.get(
        where={
            "$and": [
                {"source_file": str(transcript_path.resolve())},
                {"wing": "wing_amber"},
            ]
        },
        include=["metadatas"],
    )
    old_rows = [
        metadata
        for metadata in interrupted["metadatas"]
        if metadata["source_version"] == old_version
    ]
    assert len(old_rows) == old_count
    assert {metadata["source_chunk_count"] for metadata in old_rows} == {old_count}

    monkeypatch.setattr(ChromaCollection, "upsert", original_upsert)
    convo_miner.mine_convos(str(source), str(palace), wing="wing_amber")
    repaired = collection.get(
        where={
            "$and": [
                {"source_file": str(transcript_path.resolve())},
                {"wing": "wing_amber"},
            ]
        },
        include=["metadatas"],
    )
    assert {metadata["room"] for metadata in repaired["metadatas"]} == {"presence"}
    assert {metadata["source_version"] for metadata in repaired["metadatas"]} != {old_version}
    assert {metadata["source_chunk_count"] for metadata in repaired["metadatas"]} == {
        len(repaired["ids"])
    }


def test_mixed_or_partial_normalized_source_repairs_on_retry(tmp_path, capsys):
    import chromadb

    from mempalace.convo_miner import mine_convos

    source = tmp_path / "source"
    source.mkdir()
    transcript = (
        '<!-- mempalace-exchange {"messages":['
        '{"id":"41","role":"user","timestamp":"2026-07-29T23:55:00Z"},'
        '{"id":"42","role":"assistant","timestamp":"2026-07-29T23:56:00Z"}]} -->\n'
        "> keep the whole normalized exchange exact\n"
        + ("Assistant evidence remains exact. " * 80)
        + "\n"
    )
    transcript_path, _, _, _ = _write_pair(source, transcript=transcript)
    palace = tmp_path / "palace"
    mine_convos(str(source), str(palace), wing="wing_amber")
    capsys.readouterr()

    client = chromadb.PersistentClient(path=str(palace))
    collection = client.get_collection("mempalace_drawers")
    rows = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["documents", "metadatas"],
    )
    assert len(rows["ids"]) > 1
    corrupt_metadata = dict(rows["metadatas"][0])
    corrupt_metadata["source_version"] = "sha256:" + ("0" * 64)
    collection.update(ids=[rows["ids"][0]], metadatas=[corrupt_metadata])
    del collection, client

    mine_convos(str(source), str(palace), wing="wing_amber")
    output = capsys.readouterr().out
    collection = chromadb.PersistentClient(path=str(palace)).get_collection("mempalace_drawers")
    repaired = collection.get(
        where={"source_file": str(transcript_path.resolve())},
        include=["metadatas"],
    )
    versions = {meta["source_version"] for meta in repaired["metadatas"]}
    expected_counts = {meta["source_chunk_count"] for meta in repaired["metadatas"]}

    assert "Files skipped (already filed): 0" in output
    assert len(versions) == 1
    assert expected_counts == {len(repaired["ids"])}


def test_delta_report_is_read_only_and_classifies_source_changes(tmp_path):
    from mempalace.convo_miner import mine_convos, normalized_conversation_delta

    source = tmp_path / "source"
    source.mkdir()
    first_dir = source / "first"
    first_dir.mkdir()
    first_path, first_sidecar_path, _, first_sidecar = _write_pair(first_dir)
    removed_dir = source / "removed"
    removed_dir.mkdir()
    removed_path, _, _, _ = _write_pair(removed_dir)
    palace = tmp_path / "palace"
    mine_convos(str(source), str(palace), wing="wing_amber")

    before = (palace / "chroma.sqlite3").stat().st_mtime_ns
    first_sidecar["room"] = "presence"
    first_sidecar_path.write_text(json.dumps(first_sidecar))
    removed_path.unlink()
    removed_path.with_name(removed_path.name + ".meta.json").unlink()
    new_dir = source / "new"
    new_dir.mkdir()
    new_path, _, _, _ = _write_pair(new_dir)

    report = normalized_conversation_delta(
        str(source),
        str(palace),
        wing="wing_amber",
    )

    assert report["new"] == [str(new_path.resolve())]
    assert report["changed"] == [str(first_path.resolve())]
    assert report["unchanged"] == []
    assert report["removed"] == [str(removed_path.resolve())]
    assert report["replacement_drawers"] >= 1
    assert isinstance(report["net_drawers"], int)
    assert (palace / "chroma.sqlite3").stat().st_mtime_ns == before


def _coverage_receipt(**overrides):
    receipt = {
        "schema": "mempalace-applied-coverage/v1",
        "wing": "wing_amber",
        "applied_snapshot_at": "2026-07-30T01:00:00Z",
        "covered_sources": "cli;openclaw;telegram;whatsapp",
        "authored_from": "2025-01-01T00:00:00Z",
        "authored_to": "2026-07-30T00:05:00Z",
        "accepted_session_count": 240,
        "quarantine_count": 3,
        "verified_profile_receipt": "sha256:" + ("1" * 64),
        "source_version": "exporter-1.0.0",
    }
    receipt.update(overrides)
    return receipt


def test_coverage_registry_advances_atomically_and_is_private(tmp_path):
    normalized = _module()
    palace = tmp_path / "palace"
    palace.mkdir()

    committed = normalized.commit_applied_coverage(
        palace,
        _coverage_receipt(),
    )
    registry_path = palace / "normalized-conversation-coverage.json"

    assert committed["wing"] == "wing_amber"
    assert normalized.read_applied_coverage(palace)["wing_amber"] == committed
    assert registry_path.stat().st_mode & 0o777 == 0o600
    assert "telegram" in registry_path.read_text()
    before = registry_path.stat().st_mtime_ns
    assert normalized.commit_applied_coverage(palace, _coverage_receipt()) == committed
    assert registry_path.stat().st_mtime_ns == before


def test_coverage_registry_rejects_stale_and_conflicting_receipts(tmp_path):
    normalized = _module()
    palace = tmp_path / "palace"
    palace.mkdir()
    current = _coverage_receipt(
        applied_snapshot_at="2026-07-30T03:00:00Z",
        authored_to="2026-07-30T02:00:00Z",
    )
    normalized.commit_applied_coverage(palace, current)
    registry_path = palace / "normalized-conversation-coverage.json"
    before = registry_path.read_bytes()

    with pytest.raises(normalized.NormalizedConversationError, match="move backward"):
        normalized.commit_applied_coverage(
            palace,
            _coverage_receipt(applied_snapshot_at="2026-07-30T02:30:00Z"),
        )
    with pytest.raises(normalized.NormalizedConversationError, match="conflicts"):
        normalized.commit_applied_coverage(
            palace,
            _coverage_receipt(
                applied_snapshot_at="2026-07-30T03:00:00Z",
                accepted_session_count=241,
            ),
        )
    with pytest.raises(normalized.NormalizedConversationError, match="watermark"):
        normalized.commit_applied_coverage(
            palace,
            _coverage_receipt(
                applied_snapshot_at="2026-07-30T04:00:00Z",
                authored_to="2026-07-30T01:00:00Z",
            ),
        )

    assert registry_path.read_bytes() == before
    assert normalized.read_applied_coverage(palace)["wing_amber"] == current


@pytest.mark.parametrize("forbidden", ["content", "messages", "excerpt"])
def test_coverage_registry_rejects_content_fields(tmp_path, forbidden):
    normalized = _module()
    palace = tmp_path / "palace"
    palace.mkdir()
    receipt = _coverage_receipt()
    receipt[forbidden] = "private transcript text"

    with pytest.raises(normalized.NormalizedConversationError, match="field mismatch"):
        normalized.commit_applied_coverage(palace, receipt)

    assert normalized.read_applied_coverage(palace) == {}


def test_failed_coverage_replace_preserves_prior_watermark(tmp_path, monkeypatch):
    normalized = _module()
    palace = tmp_path / "palace"
    palace.mkdir()
    normalized.commit_applied_coverage(palace, _coverage_receipt())
    prior = normalized.read_applied_coverage(palace)

    def fail_replace(*_args, **_kwargs):
        raise OSError("replace failed")

    monkeypatch.setattr(normalized.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        normalized.commit_applied_coverage(
            palace,
            _coverage_receipt(
                applied_snapshot_at="2026-07-30T02:00:00Z",
                accepted_session_count=241,
            ),
        )

    assert normalized.read_applied_coverage(palace) == prior


def test_status_reads_coverage_without_advancing_it(tmp_path, monkeypatch):
    import mempalace.mcp_server as mcp_server

    normalized = _module()
    palace = tmp_path / "palace"
    palace.mkdir()
    normalized.commit_applied_coverage(palace, _coverage_receipt())
    monkeypatch.setattr(
        mcp_server,
        "_config",
        mcp_server.MempalaceConfig(palace_path=str(palace)),
    )
    monkeypatch.setattr(
        mcp_server,
        "_sqlite_taxonomy",
        lambda: (0, {}),
    )
    monkeypatch.setattr(mcp_server, "_backend_db_exists", lambda: False)
    monkeypatch.setattr(mcp_server, "_refresh_vector_disabled_flag", lambda: None)
    monkeypatch.setattr(mcp_server, "_vector_disabled", False)

    before = (palace / "normalized-conversation-coverage.json").read_bytes()
    status = mcp_server.tool_status()

    assert status["applied_coverage"]["wing_amber"]["accepted_session_count"] == 240
    assert (palace / "normalized-conversation-coverage.json").read_bytes() == before


def test_raw_mcp_registers_delta_and_operator_coverage_tools():
    from mempalace import mcp_server

    delta = mcp_server.TOOLS["mempalace_normalized_conversation_delta"]
    commit = mcp_server.TOOLS["mempalace_commit_applied_coverage"]

    assert delta["handler"] is mcp_server.tool_normalized_conversation_delta
    assert delta["input_schema"]["required"] == ["source", "wing"]
    assert delta["input_schema"]["additionalProperties"] is False
    assert commit["handler"] is mcp_server.tool_commit_applied_coverage
    assert commit["input_schema"]["required"] == ["receipt"]
    assert commit["input_schema"]["additionalProperties"] is False
    receipt_schema = commit["input_schema"]["properties"]["receipt"]
    assert receipt_schema["additionalProperties"] is False
    assert receipt_schema["properties"]["schema"]["const"] == ("mempalace-applied-coverage/v1")
    assert receipt_schema["properties"]["verified_profile_receipt"]["pattern"].startswith(
        "^sha256:"
    )
    assert "mempalace_commit_applied_coverage" in mcp_server._MUTATING_TOOLS


def test_operator_coverage_tool_commits_verified_receipt(tmp_path, monkeypatch):
    from mempalace import mcp_server

    palace = tmp_path / "palace"
    palace.mkdir()
    monkeypatch.setattr(
        mcp_server,
        "_config",
        mcp_server.MempalaceConfig(palace_path=str(palace)),
    )

    result = mcp_server.tool_commit_applied_coverage(_coverage_receipt())

    assert result["success"] is True
    assert result["coverage"]["wing"] == "wing_amber"
    assert (
        mcp_server.tool_status()["applied_coverage"]["wing_amber"]["accepted_session_count"] == 240
    )


def test_operator_tools_return_stable_no_palace_error(monkeypatch):
    from types import SimpleNamespace

    from mempalace import mcp_server

    monkeypatch.setattr(
        mcp_server,
        "_config",
        SimpleNamespace(palace_path=""),
    )

    for result in (
        mcp_server.tool_normalized_conversation_delta("/tmp/source", "wing_amber"),
        mcp_server.tool_commit_applied_coverage(_coverage_receipt()),
    ):
        assert result == {
            "success": False,
            "error": "no palace configured",
            "error_class": "PalaceNotConfigured",
        }
