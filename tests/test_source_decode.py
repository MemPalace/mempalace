"""Source bytes reach drawers without U+FFFD, and every byte comes back.

Every ingest path decodes through ``fsio.decode_source``: valid UTF-8 as is,
UTF-8 with stray bytes through ``surrogateescape``, and only files with no
UTF-8 in them at all as WHATWG windows-1252. A source that is not plain UTF-8
carries ``source_encoding`` on its drawers (RFC 002 section 5.6), and
``fsio.encode_source(text, source_encoding)`` gives its bytes back.
"""

import json

import pytest

from mempalace.fsio import (
    decode_source,
    decode_windows_1252,
    encode_source,
    encode_windows_1252,
    encoding_metadata,
    read_source_text,
)
from mempalace.palace import get_collection

SMART_QUOTES_CP1252 = b"\x93quoted\x94 and \x91single\x92"
SMART_QUOTES_TEXT = "“quoted” and ‘single’"
CAFE_WITH_STRAY_BYTE = "café notes ".encode("utf-8") * 3 + b"\xff tail"


@pytest.mark.parametrize(
    ("raw", "text", "encoding"),
    [
        (b"", "", "utf-8"),
        ("café ✨".encode("utf-8"), "café ✨", "utf-8"),
        (b"\xef\xbb\xbfwith a BOM", "with a BOM", "utf-8-sig"),
        (b"caf\xc3\xa9 r\xc3\xa9sum\xc3\xa9 \xff", "café résumé \udcff", "utf-8+surrogateescape"),
        (b"\xef\xbb\xbfab\xffc", "ab\udcffc", "utf-8-sig+surrogateescape"),
        (SMART_QUOTES_CP1252, SMART_QUOTES_TEXT, "windows-1252"),
        # An undefined cp1252 byte is a C1 control; the quotes stay quotes.
        (b"\x93a\x81b\x94 \xe9", "“a\x81b” é", "windows-1252"),
        # As many stray bytes as valid sequences: not enough to call it UTF-8.
        (b"caf\xc3\xa9 \xff", "cafÃ© ÿ", "windows-1252"),
    ],
    ids=[
        "empty",
        "utf-8",
        "bom",
        "stray-byte",
        "bom-stray-byte",
        "cp1252",
        "cp1252-undefined",
        "tie-is-legacy",
    ],
)
def test_decode_source_picks_the_reading_that_keeps_the_text(raw, text, encoding):
    assert decode_source(raw) == (text, encoding)


def test_a_cp1252_file_whose_bytes_happen_to_form_utf8_stays_cp1252():
    """``É`` then ``”`` in cp1252 is C9 94, a valid UTF-8 sequence; the file's
    other accented letters are still stray bytes, so it is not UTF-8."""
    text = "L’ÉTÉ” est arrivé, déjà très chaud. “Oui”, dit-il, ça ira."
    assert decode_source(text.encode("cp1252")) == (text, "windows-1252")


def test_a_long_utf8_file_with_one_stray_byte_stays_utf8():
    raw = "naïve résumé café ".encode("utf-8") * 200 + b"\xff"
    text, encoding = decode_source(raw)
    assert encoding == "utf-8+surrogateescape"
    assert text.endswith("café \udcff")


@pytest.mark.parametrize(
    "raw",
    [
        SMART_QUOTES_CP1252,
        b"\x93a\x81b\x94 \xe9",
        b"\xef\xbb\xbfplain",
        CAFE_WITH_STRAY_BYTE,
        b"\xef\xbb\xbfab\xffc",
        bytes(range(256)),
    ],
    ids=["cp1252", "cp1252-undefined", "bom", "stray-byte", "bom-stray-byte", "every-byte"],
)
def test_decode_source_never_replaces_a_byte_and_round_trips(raw):
    text, encoding = decode_source(raw)
    assert "�" not in text
    assert encode_source(text, encoding) == raw


def test_a_stray_byte_does_not_turn_utf8_text_into_mojibake():
    text, encoding = decode_source(CAFE_WITH_STRAY_BYTE)
    assert encoding == "utf-8+surrogateescape"
    assert "café notes" in text
    assert "cafÃ" not in text
    assert text.encode("utf-8", "surrogateescape") == CAFE_WITH_STRAY_BYTE


def test_windows_1252_maps_every_byte_and_back():
    every_byte = bytes(range(256))
    assert encode_windows_1252(decode_windows_1252(every_byte)) == every_byte
    assert decode_windows_1252(b"\x81\x8d\x8f\x90\x9d") == "\x81\x8d\x8f\x90\x9d"
    with pytest.raises(UnicodeEncodeError):
        encode_windows_1252("✨")


def test_encode_source_rejects_an_unknown_encoding():
    with pytest.raises(ValueError):
        encode_source("x", "latin-1")


def test_decode_source_reads_newlines_like_text_mode_only_when_asked():
    assert decode_source(b"a\r\nb\rc")[0] == "a\r\nb\rc"
    assert decode_source(b"a\r\nb\rc", universal_newlines=True)[0] == "a\nb\nc"


def test_encoding_metadata_names_only_non_utf8_sources():
    assert encoding_metadata("utf-8") == {}
    assert encoding_metadata("windows-1252") == {"source_encoding": "windows-1252"}


def test_read_source_text_returns_the_fstat_mtime_and_encoding(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_bytes(SMART_QUOTES_CP1252 + b"\r\nnext")
    text, encoding, mtime = read_source_text(path, max_bytes=1024)
    assert (text, encoding) == (SMART_QUOTES_TEXT + "\nnext", "windows-1252")
    assert mtime == path.stat().st_mtime


def test_read_source_text_refuses_a_file_over_the_cap(tmp_path):
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * 2048)
    with pytest.raises(OSError, match="too large"):
        read_source_text(path, max_bytes=1024)


def _backend_collection(kind, tmp_path):
    from mempalace.backends.base import PalaceRef

    if kind == "chroma":
        from mempalace.backends.chroma import ChromaBackend as Backend
    else:
        from mempalace.backends.sqlite_exact import SQLiteExactBackend as Backend
    return Backend().get_collection(
        palace=PalaceRef(id=str(tmp_path), local_path=str(tmp_path)),
        collection_name="mempalace_drawers",
        create=True,
    )


@pytest.mark.parametrize("kind", ["chroma", "sqlite_exact"])
def test_a_stray_byte_drawer_stays_findable_by_full_text_and_round_trips(tmp_path, kind):
    text, encoding = decode_source(CAFE_WITH_STRAY_BYTE)
    col = _backend_collection(kind, tmp_path)
    vectors = {"embeddings": [[0.1, 0.2, 0.3, 0.4]]} if kind == "sqlite_exact" else {}
    col.add(
        documents=[text],
        ids=["d1"],
        metadatas=[{"wing": "w", **encoding_metadata(encoding)}],
        **vectors,
    )

    hits = col.lexical_search(query="café", n_results=5).hits
    assert [hit.id for hit in hits] == ["d1"]
    assert encode_source(hits[0].document, hits[0].metadata["source_encoding"]) == (
        CAFE_WITH_STRAY_BYTE
    )


def test_the_embedder_never_sees_a_lone_surrogate():
    """Tokenizers reject lone surrogates ("TextInputSequence must be str"), so
    the embedder gets U+FFFD where the stored text keeps the stray byte."""
    from mempalace.backends.embedding_wrapper import _embeddable

    assert _embeddable(["café \udcff", "plain"]) == ["café �", "plain"]


def _rows(palace, source_file):
    got = get_collection(palace).get(
        where={"source_file": str(source_file)}, include=["documents", "metadatas"]
    )
    return list(zip(got["documents"], got["metadatas"]))


def _paragraphs(body: bytes) -> bytes:
    return b"\n\n".join(b"paragraph %d. " % i + body * 12 for i in range(4))


@pytest.mark.parametrize(
    ("raw", "expected", "encoding"),
    [
        (_paragraphs(SMART_QUOTES_CP1252 + b" "), SMART_QUOTES_TEXT, "windows-1252"),
        (_paragraphs(b"\x93a\x81b\x94 "), "“a\x81b”", "windows-1252"),
        (b"\xef\xbb\xbf" + _paragraphs(b"plain words "), "plain words", "utf-8-sig"),
        (
            _paragraphs("résumé ".encode("utf-8")) + b"\xff tail",
            "résumé",
            "utf-8+surrogateescape",
        ),
    ],
    ids=["cp1252", "cp1252-undefined", "bom", "stray-byte"],
)
def test_project_mine_keeps_every_byte_and_stamps_the_encoding(tmp_path, raw, expected, encoding):
    from mempalace import miner

    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_bytes(raw)
    palace = str(tmp_path / "palace")
    miner.mine(str(src), palace, wing_override="proj", agent="test")

    rows = _rows(palace, doc)
    assert rows
    assert all("�" not in text and "﻿" not in text for text, _ in rows)
    assert expected in rows[0][0]
    assert {meta.get("source_encoding") for _, meta in rows} == {encoding}


def test_project_mine_of_a_stray_byte_file_is_searchable(tmp_path):
    from mempalace import miner

    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_bytes(_paragraphs("café notes ".encode("utf-8")) + b"\xff tail")
    palace = str(tmp_path / "palace")
    miner.mine(str(src), palace, wing_override="proj", agent="test")

    hits = get_collection(palace).lexical_search(query="café", n_results=5).hits
    assert hits
    assert all(hit.metadata["source_encoding"] == "utf-8+surrogateescape" for hit in hits)


def test_project_mine_stamps_nothing_for_plain_utf8(tmp_path):
    from mempalace import miner

    src = (tmp_path / "proj").resolve()
    src.mkdir()
    doc = src / "notes.md"
    doc.write_bytes(_paragraphs("café ".encode("utf-8")))
    palace = str(tmp_path / "palace")
    miner.mine(str(src), palace, wing_override="proj", agent="test")

    rows = _rows(palace, doc)
    assert rows
    assert all("source_encoding" not in meta for _, meta in rows)


def test_convo_mine_keeps_cp1252_turns_and_stamps_the_encoding(tmp_path):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    doc = src / "chat.txt"
    turns = [
        b"> question %d " % i + SMART_QUOTES_CP1252 + b" asked" * 20 + b"\n" + b"answer " * 40
        for i in range(3)
    ]
    doc.write_bytes(b"\n\n".join(turns) + b"\n")
    palace = str(tmp_path / "palace")
    convo_miner.mine_convos(str(src), palace, wing="chats")

    rows = _rows(palace, doc)
    assert rows
    assert all("�" not in text for text, _ in rows)
    assert any(SMART_QUOTES_TEXT in text for text, _ in rows)
    assert {meta.get("source_encoding") for _, meta in rows} == {"windows-1252"}


def test_convo_mine_of_a_stray_byte_transcript_files_and_hashes(tmp_path):
    from mempalace import convo_miner

    src = (tmp_path / "chats").resolve()
    src.mkdir()
    doc = src / "chat.txt"
    turns = [
        b"> question %d caf\xc3\xa9 r\xc3\xa9sum\xc3\xa9 " % i
        + b"asked " * 20
        + b"\xff\n"
        + b"answer " * 40
        for i in range(3)
    ]
    doc.write_bytes(b"\n\n".join(turns) + b"\n")
    palace = str(tmp_path / "palace")
    convo_miner.mine_convos(str(src), palace, wing="chats")

    rows = _rows(palace, doc)
    assert rows
    assert any("café" in text for text, _ in rows)
    assert {meta.get("source_encoding") for _, meta in rows} == {"utf-8+surrogateescape"}


def test_sweeper_decodes_a_cp1252_line_and_names_the_encoding(tmp_path):
    from mempalace.sweeper import parse_claude_jsonl

    record = {
        "type": "user",
        "message": {"role": "user", "content": "PLACEHOLDER"},
        "timestamp": "2026-01-01T00:00:00Z",
        "uuid": "u1",
        "sessionId": "s1",
    }
    line = json.dumps(record).encode("ascii").replace(b"PLACEHOLDER", SMART_QUOTES_CP1252)
    path = tmp_path / "session.jsonl"
    path.write_bytes(line + b"\n")

    (message,) = list(parse_claude_jsonl(str(path)))
    assert message["content"] == SMART_QUOTES_TEXT
    assert message["encoding"] == "windows-1252"


def test_diary_ingest_keeps_cp1252_text_and_stamps_the_encoding(tmp_path):
    from mempalace.diary_ingest import ingest_diaries

    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    entry = b"## 09:00\n\n" + SMART_QUOTES_CP1252 + b" " + b"a day of work " * 10 + b"\n"
    (diary_dir / "2026-01-02.md").write_bytes(entry)
    palace = str(tmp_path / "palace")
    ingest_diaries(diary_dir, palace)

    got = get_collection(palace).get(include=["documents", "metadatas"])
    rows = list(zip(got["documents"], got["metadatas"]))
    assert rows
    assert all("�" not in text for text, _ in rows)
    assert any(SMART_QUOTES_TEXT in text for text, _ in rows)
    assert {meta.get("source_encoding") for _, meta in rows} == {"windows-1252"}


def test_diary_ingest_hashes_a_stray_byte_diary(tmp_path):
    from mempalace.diary_ingest import ingest_diaries

    diary_dir = tmp_path / "diary"
    diary_dir.mkdir()
    entry = b"## 09:00\n\ncaf\xc3\xa9 r\xc3\xa9sum\xc3\xa9 " + b"a day of work " * 10 + b"\xff\n"
    (diary_dir / "2026-01-02.md").write_bytes(entry)
    palace = str(tmp_path / "palace")
    ingest_diaries(diary_dir, palace)

    got = get_collection(palace).get(include=["metadatas"])
    assert {meta.get("source_encoding") for meta in got["metadatas"]} == {"utf-8+surrogateescape"}


def test_hook_recent_messages_read_a_stray_byte_line(tmp_path):
    from mempalace.hooks_cli import _extract_recent_messages

    entry = {"message": {"role": "user", "content": "PLACEHOLDER"}}
    line = (
        json.dumps(entry)
        .encode("ascii")
        .replace(b"PLACEHOLDER", b"caf\xc3\xa9 r\xc3\xa9sum\xc3\xa9 \xff ok")
    )
    path = tmp_path / "t.jsonl"
    path.write_bytes(line + b"\n")

    (message,) = _extract_recent_messages(str(path))
    assert message.startswith("café ")
    assert "�" not in message
