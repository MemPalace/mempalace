"""Import/live continuity and unchanged per-import discovery metadata."""

from commonplace.handoff.thread_store import canonical_sha256
from commonplace.handoff.transcripts import parse
from commonplace.import_sources import parse_import
from tests.commonplace_sync_fixtures import (
    IMPORT_KEYS,
    accepted,
    actors,
    appended,
    counts,
    hub as hub,
    item,
    parts,
    raw,
    reached,
    records,
    reset_reached as reset_reached,
    sha,
)


def test_oauth_import_then_live(hub):
    h = hub()
    p, token, _ = actors(h)
    oauth = h.oauth(p)
    F, G = records("S"), records("S", 1, "g")
    accepted(h.imp("claude-code", [item("S", raw(F))], token=oauth))
    h.at(901)
    appended(h.push(token, "claude-code", "S", raw(F + G)))
    o = h.oracle(p, F)
    assert counts(o, F) == [1] * len(F)  # COUNTED:L3
    reached("L3")
    assert counts(h.oracle(p, F + G), F + G) == [1] * len(F + G)


def test_t3_underlying_native_file_joins_group(hub):
    h = hub()
    p, token, grant = actors(h)
    F, G = records("S"), records("S", 1, "g")
    accepted(h.imp("t3", [item("S", raw(F))], token=grant))
    h.at(901)
    appended(h.push(token, "claude-code", "S", raw(F + G)))
    o = h.oracle(p, F)
    assert counts(o, F) == [1] * len(F)  # COUNTED:L7
    reached("L7")
    assert h.read(p, h.import_thread(p, "S", "t3")) == raw(F + G)


def test_private_person_import_then_live(hub):
    h = hub()
    p, token, _ = actors(h)
    F, G = records("S"), records("S", 1, "g")
    private = h.imp("claude-code", [item("S", raw(F))], cookie=p.cookie, private=True)
    assert private.status == 200  # COUNTED:L8
    reached("L8")
    accepted(private)
    h.at(901)
    appended(h.push(token, "claude-code", "S", raw(F + G)))
    assert counts(h.oracle(p, F + G), F + G) == [1] * len(F + G)


def test_native_memory_import_versions(hub):
    h = hub()
    p, token, grant = actors(h)
    appended(h.push(token, "claude-code", "S", raw(records("S"))))
    text = "  synthetic memory café 🦉\r\nKeep every word.\t"
    tid = f"notes:folder/note.md:{sha(text.encode())}"
    h.at(10)
    memory = h.imp("native-memory", [item(tid, text.encode(), "file")], token=grant)
    assert memory.status == 200  # COUNTED:M1
    reached("M1")
    accepted(memory)
    before = h.imports(p)
    accepted(h.imp("native-memory", [item(tid, text.encode(), "file")], token=grant))
    assert h.imports(p) == before
    assert h.read(p, h.import_thread(p, tid, "native-memory")) == text.encode()
    wrong = h.imp("native-memory", [item("notes:wrong:bad", text.encode(), "file")], token=grant)
    assert wrong.status == 200
    assert wrong.json["results"][0]["code"] == "invalid_thread"
    assert h.imp("native-memory", [item(tid, text.encode(), "file")], cookie=p.cookie).status == 400
    changed = text + "\nversion two"
    h.at(901)
    tid2 = f"notes:folder/note.md:{sha(changed.encode())}"
    accepted(h.imp("native-memory", [item(tid2, changed.encode(), "file")], token=grant))
    assert len(h.imports(p)) == len(before) + 1


def test_imports_listing_counts_match_base(hub):
    h = hub(plans="off")
    p, capture, grant = actors(h)
    F = {i: raw(records(f"s{i}")) for i in range(1, 9)}
    F[6] = F[6].replace(b"\r\n", b"\n")
    G = {i: raw(records(f"s{i}", 1, "g")) for i in range(1, 9)}
    n = {i: len(parse(value.decode())) for i, value in F.items()}
    accepted(h.imp("claude-code", [item("s1", F[1])], token=grant))
    split2 = parts("s2", F[2], len(raw(records("s2", 1))))
    accepted(h.imp("claude-code", [split2[0]], token=grant))
    accepted(h.imp("claude-code", [split2[1]], token=grant))
    appended(h.push(capture, "claude-code", "s1", F[1] + G[1]))
    appended(h.push(capture, "claude-code", "s2", F[2] + G[2]))
    appended(h.push(capture, "claude-code", "s3", F[3]))
    accepted(h.imp("claude-code", [item("s3", F[3])], token=grant))
    appended(h.push(capture, "claude-code", "s4", F[4]))
    split4 = parts("s4", F[4], len(raw(records("s4", 1))))
    accepted(h.imp("claude-code", [split4[0]], token=grant))
    accepted(h.imp("claude-code", [split4[1]], token=grant))
    accepted(h.imp("claude-code", [item("s1", F[1] + G[1])], token=grant))
    P5 = b'{"type":"user","sessionId":"s5","message":{"content":"unterminated'
    accepted(h.imp("claude-code", [item("s5", F[5] + P5)], token=grant))
    appended(h.push(capture, "claude-code", "s5", F[5]))
    accepted(h.imp("claude-code", [item("s6", F[6][:-1])], token=grant))
    appended(h.push(capture, "claude-code", "s6", F[6] + G[6]))
    broken = F[7].rsplit(b"\r\n", 2)[0] + b'\r\n{"type":"user","message":{"content":"unterminated'
    split7 = parts("s7", broken, len(raw(records("s7", 1))))
    first7 = accepted(h.imp("claude-code", [split7[0]], token=grant))
    refused = h.imp("claude-code", [split7[1]], token=grant)
    assert refused.status == 200
    assert refused.json["results"][0]["code"] == "invalid_transcript"
    P8 = b'{"type":"user","sessionId":"s8","message":{"content":"unterminated'
    R8 = P8 + b'"}}\n'
    accepted(h.imp("claude-code", [item("s8", F[8] + P8)], token=grant))
    accepted(h.imp("claude-code", [item("s8", F[8] + R8)], token=grant))
    canonical1 = canonical_sha256(parse_import("claude-code", (F[1] + G[1]).decode(), "auto"))
    canonical8 = canonical_sha256(parse_import("claude-code", (F[8] + R8).decode(), "auto"))
    expected = {
        ("s1", None): n[1],
        ("s1", f"sha256:{canonical1}"): n[1] + len(parse(G[1].decode())),
        ("s2", None): n[2] + 3,
        ("s3", None): n[3],
        ("s4", None): n[4] + 3,
        ("s5", None): len(parse((F[5] + P5).decode())),
        ("s6", None): len(parse(F[6][:-1].decode())),
        ("s7", None): first7["messages"] + 1,
        ("s8", None): len(parse((F[8] + P8).decode())),
        ("s8", f"sha256:{canonical8}"): len(parse((F[8] + R8).decode())),
    }
    listing = h.imports(p)
    assert all(set(row) == IMPORT_KEYS for row in listing)
    assert len(listing) == len(expected)
    assert {(r["thread_id"], r["conflict_of"]): r["stored_messages"] for r in listing} == expected
