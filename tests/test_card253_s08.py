"""Protected reference spellings never rewrite verbatim citations or fact text."""

import json
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import quote

import pytest

from commonplace import capture, continuity_schema, import_grant, import_sources, sessions
from commonplace.handoff.models import ThreadMessage
from tests._commonplace_helpers import (  # noqa: F401
    functional_worker_transport,
    hub_fixture,
)

SCOPES = ["memory:read", "memory:write", "private:read", "threads:read", "kg:read", "kg:write"]
TOOLS = ["mempalace_kg_query", "mempalace_kg_add"]
FULLWIDTH = "ｃａｐｔｕｒｅ：ｕ１：ｃｏｄｅｘ：ｏｐａｑｕｅ７"
SPELLINGS = {
    "space": "capture: u1:codex:opaque7",
    "nbsp": "capture:\u00a0u1:codex:opaque7",
    "no_separator": "captureu1:codex:opaque7",
    "fullwidth": FULLWIDTH,
    "encoded_fullwidth": quote(FULLWIDTH, safe=""),
}
ORDINARY = {
    "url": ("source_url", "https://example.test/capturing-notes"),
    "file": ("source_file", "capture-notes.md"),
    "bare": ("source_file", "capture"),
    "notes": ("source_file", "capture-notes"),
    "opaque": ("source_file", "captureopaque7"),
    "fork_url": ("source_url", "https://example.test/capture-fork-notes"),
    "import_path": ("source_file", "notes/import-private-notes.md"),
    "inside_word": ("source_file", "recaptureu1:codex:opaque7"),
}
UNPRODUCED = {
    "unknown_source": "captureu1:src:opaque7",
    "numeric_source": "capturenotes:2026:11",
    "prose": "Capture: Q3: Notes: Draft",
}
PRODUCERS = [
    "fork-source_thread",
    "fork-source_url",
    *("capture-" + source for source in capture.SOURCES),
    *("import-" + source for source in (*import_sources.SOURCES, "native-memory")),
    "import-hash",
]


@pytest.fixture
def case(hub, kg, monkeypatch, functional_worker_transport):  # noqa: F811
    h = hub
    h.supervisor.get(h.workspace)
    user = h.db.create_user("reader@example.test")
    h.db.add_member(h.workspace, user, "member", actor=h.admin)
    token = h.token(
        "reader",
        user=user,
        tools=TOOLS,
        scopes=[s for s in SCOPES if s != "private:read"],
        read_wings=["*"],
        write_wings=["shared"],
        trust="trusted",
    )
    member = h.db.authenticate_token(token)
    owner = sessions.person_principal(h.db, h.admin, h.workspace)
    link = sessions.create_login_link(h.db, "owner@example.com", h.workspace)
    cookie = sessions.redeem_login_link(h.db, link)
    assert member.role == "member" and not member.person
    assert owner.role == "owner" and owner.person and owner.auth == "session"
    for principal in (member, owner):
        assert principal.policy.trust == "trusted"
        assert principal.policy.sees_all_wings == principal.person
        assert all(principal.policy.has_scope(scope) for scope in SCOPES if scope != "private:read")
        assert principal.policy.can_write_wing("shared")
        assert all(principal.policy.refusal(tool) is None for tool in TOOLS)
    store = h.gateway.handoff.store_for(h.workspace)
    reached = []
    original = h.gateway.mediator.workers.post

    def worker(*args, **kwargs):
        reached.append(args[1].get("params", {}).get("name"))
        return original(*args, **kwargs)

    monkeypatch.setattr(h.gateway.mediator.workers, "post", worker)
    c = SimpleNamespace(
        h=h,
        kg=kg,
        member=member,
        owner=owner,
        token=token,
        cookie=cookie,
        store=store,
        reached=reached,
    )
    c.primary = stored(c, "capture:primary:codex:readable")
    assert all(h.gateway.handoff.recall.can_read(p, c.primary.id) for p in (member, owner))
    return c


def stored(c, source, wing="shared"):
    row = c.store.store(
        c.h.workspace,
        source,
        [ThreadMessage(0, "user", "message", "Exact synthetic words", "Exact synthetic words")],
        actor=c.member.user_id,
        wing=wing,
    )
    assert c.store.by_source(c.h.workspace, source).id == row.id
    return row


def invoke(c, role, tool, **args):
    headers = {"Cookie": f"{sessions.COOKIE}={c.cookie}"} if role == "owner" else None
    status, raw = c.h.raw(
        None if role == "owner" else c.token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mempalace_" + tool, "arguments": args},
        },
        headers=headers,
    )
    assert status == 200, raw
    reply = json.loads(raw)
    assert "error" not in reply, reply
    return json.loads(reply["result"]["content"][0]["text"])


def fact(c, role="member"):
    result = invoke(c, role, "kg_query", entity="SpellingFact")
    assert "mempalace_kg_query" in c.reached
    assert result["count"] == 1, result
    row = result["facts"][0]
    assert (row["subject"], row["predicate"], row["object"]) == (
        "SpellingFact",
        "keeps",
        "Exact  original words: capture: u1:codex:opaque7",
    )
    return row


def args(c, field, value):
    return dict(
        subject="SpellingFact",
        predicate="keeps",
        object="Exact  original words: capture: u1:codex:opaque7",
        wing="shared",
        sensitivity="public",
        **{"source_thread": c.primary.id, field: value},
    )


def check_citation(c, role, field, value, write):
    if write:
        result = invoke(c, role, "kg_add", **args(c, field, value))
        assert result.get("success"), result
        assert "mempalace_kg_add" in c.reached
    else:
        data = args(c, field, value)
        seed(c, data)
    row = fact(c, role)
    public_field = "origin_link" if field == "source_url" else field
    public_row = row["sources"][0] if field == "source_file" else row
    assert public_row.get(public_field) == value
    assert row["sources"][0].get(public_field) == value
    with c.kg._lock:
        raw = c.kg._conn().execute("SELECT * FROM triples").fetchone()
        assert raw[public_field] == value


def seed(c, data):
    # Legacy citations can predate current navigable-URL write validation.
    url = data.pop("source_url", None)
    tid = c.kg.add_triple(data.pop("subject"), data.pop("predicate"), data.pop("object"), **data)
    if url is not None:
        with c.kg._lock:
            conn = c.kg._conn()
            conn.execute("UPDATE triples SET origin_link=? WHERE id=?", (url, tid))
            conn.execute("UPDATE fact_sources SET origin_link=? WHERE triple_id=?", (url, tid))
            conn.commit()


def navigable(value):
    return "https://example.test/?ref=" + quote(value, safe=":#-")


def producer(c, kind, role):
    principal = getattr(c, role)
    if kind.startswith("fork-"):
        value = continuity_schema.fork_id("u1", "readable-stream")
        field = kind.removeprefix("fork-")
    elif kind.startswith("capture-"):
        _, value = capture._identity(
            replace(c.owner, user_id="u1"),
            {"source": kind.removeprefix("capture-"), "thread_id": "opaque7"},
        )
        field = "source_url"
    else:
        source = "codex" if kind == "import-hash" else kind.removeprefix("import-")
        value = import_grant.grant_id(principal.user_id, source, "opaque7")
        if kind == "import-hash":
            value += "#sha256:" + "a" * 64
        field = "source_url"
    row = stored(c, value)
    assert c.h.gateway.handoff.recall.can_read(principal, row.id)
    return field, navigable(value) if field == "source_url" else value


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_card253_s08_spelling(case, spelling):
    c = case
    _, source = capture._identity(
        replace(c.owner, user_id="u1"),
        {"source": "codex", "thread_id": "opaque7"},
    )
    assert source == "capture:u1:codex:opaque7"
    hidden = stored(c, source, capture.PRIVATE_WING)
    assert not c.h.gateway.handoff.recall.can_read(c.member, hidden.id)
    data = args(c, "source_url", SPELLINGS[spelling])
    seed(c, data)
    row = fact(c)
    # source_url is persisted/projected as origin_link by the production KG.
    assert "origin_link" not in row, "s08 hidden shadow citation retained"
    assert all("origin_link" not in source for source in row["sources"])


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", ORDINARY)
def test_card253_s08_ordinary_citation(case, role, kind):
    check_citation(case, role, *ORDINARY[kind], write=False)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", ORDINARY)
def test_card253_s08_ordinary_write(case, role, kind):
    check_citation(case, role, *ORDINARY[kind], write=True)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", PRODUCERS)
def test_card253_s08_producer_citation(case, role, kind):
    check_citation(case, role, *producer(case, kind, role), write=False)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", PRODUCERS)
def test_card253_s08_producer_write(case, role, kind):
    check_citation(case, role, *producer(case, kind, role), write=True)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", UNPRODUCED)
@pytest.mark.parametrize("field", ["source_file", "source_url"])
def test_card253_s08_unproduced_citation(case, role, kind, field):
    check_citation(case, role, field, UNPRODUCED[kind], write=False)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("kind", UNPRODUCED)
@pytest.mark.parametrize("field", ["source_file", "source_url"])
def test_card253_s08_unproduced_write(case, role, kind, field):
    check_citation(
        case,
        role,
        field,
        navigable(UNPRODUCED[kind]) if field == "source_url" else UNPRODUCED[kind],
        write=True,
    )


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
def test_card253_s08_thread_boundary(case, role, write):
    check_citation(case, role, "source_file", " " + case.primary.id + " ", write)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("spelling", [*SPELLINGS, "case", "structural_space"])
def test_card253_s08_head_readable_shadow(case, role, write, spelling):
    stored(case, "capture:u1:codex:opaque7")
    value = {
        **SPELLINGS,
        "case": "Capture: u1:Codex:opaque7",
        "structural_space": "capture:u1: codex: opaque7",
    }[spelling]
    check_citation(case, role, "source_file", value, write)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize("state", ["missing", "unreadable"])
def test_card253_s08_head_refused_shadow(case, role, state):
    c = case
    # A real different filer makes the capture unreadable even to the owner.
    other = c.h.db.create_user("other-filer@example.test")
    _, source = capture._identity(
        replace(c.owner, user_id=other),
        {"source": "codex", "thread_id": "opaque7"},
    )
    if state == "unreadable":
        row = stored(c, source, capture.PRIVATE_WING)
        assert not c.h.gateway.handoff.recall.can_read(getattr(c, role), row.id)
    else:
        assert c.store.by_source(c.h.workspace, source) is None
    shadow = source.replace("capture:", "Capture: ", 1).replace(":codex:", ":Codex:")
    # Use a valid navigable URL to reach provenance authorization before storage.
    result = invoke(c, role, "kg_add", **args(c, "source_url", navigable(shadow)))
    canonical = invoke(c, role, "kg_add", **args(c, "source_url", navigable(source)))
    assert result == canonical == {"success": False, "error": "Source not found"}
    assert c.kg.query_entity("SpellingFact") == []
    # Exercise the real transport on a read even when the write correctly stops early.
    assert invoke(c, role, "kg_query", entity="SpellingFact")["count"] == 0


@pytest.mark.parametrize("kind", PRODUCERS)
def test_card253_s08_head_producer_shadow(case, kind):
    c = case
    field, value = producer(c, kind, "member")
    if field == "source_url":
        value = value.removeprefix("https://example.test/?ref=")
    shadow = value.replace(":", ": ", 1)
    check_citation(c, "member", "source_file", shadow, write=True)


@pytest.mark.parametrize(
    "boundary", ["", " ", "\u00a0", "/", "?", "&", "#", "=", "(", "<", '"', "'"]
)
def test_card253_s08_head_boundary(boundary):
    from commonplace.handoff.provenance_refs import refs

    assert refs(boundary + "captureu1:codex:opaque7").tokens == ("capture:u1:codex:opaque7",)


@pytest.mark.parametrize(
    "value",
    [
        "recaptureu1:codex:opaque7",
        "_captureu1:codex:opaque7",
        ".captureu1:codex:opaque7",
        "capture-u1:codex:opaque7",
        "capture_u1:codex:opaque7",
        "capture.u1:codex:opaque7",
        "capture: u1:src:opaque7",
        "captureu1:cursor:opaque7",
        "import-privateu1:src:opaque7",
        "captureu1/other:codex:opaque7",
        "captureu1\\other:codex:opaque7",
        "captureu1:co dex:opaque7",
        "capture-forku1:" + "a" * 31,
        "capture-forku1:" + "A" * 32,
        "capture: u1:\u2009codex:opaque7",
        "capture\u0301u1:codex:opaque7",
        "Capture: Q3: Notes: Draft",
    ],
)
def test_card253_s08_head_incomplete_shape(value):
    from commonplace.handoff.provenance_refs import _PRIVATE, refs

    # Existing ordinary tokens remain recognized, even for legacy shapes.
    assert refs(value).tokens == tuple(m[0] for m in _PRIVATE.finditer(value))


@pytest.mark.parametrize("kind", PRODUCERS)
def test_card253_s08_head_no_phantom(case, kind):
    from commonplace.handoff.provenance_refs import refs

    field, value = producer(case, kind, "member")
    if field == "source_url":
        value = value.removeprefix("https://example.test/?ref=")
    assert refs(value).tokens == (value,)
    # Fixture requires functional transport; also check public canonical retention.
    check_citation(
        case, "member", field, navigable(value) if field == "source_url" else value, False
    )


@pytest.mark.parametrize(
    "value", ["bad\x00cite", "bad\u200dcite", "%E2%80%8D", "%ff", "%2525252541"]
)
def test_card253_s08_head_decode_refusal(value):
    from commonplace.handoff.provenance_refs import REFUSE, refs

    assert refs(value) is REFUSE


def test_card253_s08_head_decode_union_and_case():
    from commonplace.handoff.provenance_refs import refs

    value = "Capture: UserX:Codex:OpaqueX:tail#sha256:abc"
    expected = "capture:UserX:codex:OpaqueX:tail#sha256:abc"
    assert refs(value).tokens == (expected,)
    for _ in range(4):
        value = quote(value, safe="")
    assert refs(value).tokens == (expected,)
    assert refs("capture:legacy:outside:domain").tokens == ("capture:legacy:outside:domain",)
    assert refs("capture: u1:codex:thr_abcd").tokens == ("thr_abcd", "capture:u1:codex:thr_abcd")


def test_card253_s08_head_hash_is_exact(case):
    c = case
    base = import_grant.grant_id(c.member.user_id, "codex", "opaque7:part2")
    hidden = stored(c, base + "#sha256:" + "a" * 64, capture.PRIVATE_WING)
    readable = stored(c, base + "#sha256:" + "b" * 64)
    assert not c.h.gateway.handoff.recall.can_read(c.member, hidden.id)
    assert c.h.gateway.handoff.recall.can_read(c.member, readable.id)
    source = base.replace("import-private:", "import-private: ", 1) + "#sha256:" + "a" * 64
    result = invoke(c, "member", "kg_add", **args(c, "source_file", source))
    assert result == {"success": False, "error": "Source not found"}
    assert invoke(c, "member", "kg_query", entity="SpellingFact")["count"] == 0


def test_card253_s08_head_source_domains():
    from commonplace.handoff.provenance_refs import _CAPTURE_SOURCES, _IMPORT_SOURCES

    assert _CAPTURE_SOURCES == set(capture.SOURCES)
    assert _IMPORT_SOURCES == set(import_sources.SOURCES) | {"native-memory"}


def test_card253_s08_head_nfkc_composition():
    from commonplace.handoff.provenance_refs import refs

    assert refs("capture: User:codex:Cafe\u0301").tokens == ("capture:User:codex:Café",)


FORK_TRAILERS = [")", ".", "/", "z", "a", "#sha256:a"]
FORK_SPELLINGS = ["space", "nbsp", "no_separator", "fullwidth", "encoded_fullwidth"]


def fork_spelling(value, spelling):
    if spelling == "canonical":
        return value
    if spelling in {"space", "nbsp"}:
        return value.replace(":", ": " if spelling == "space" else ":\u00a0", 1)
    if spelling == "no_separator":
        return value.replace(":", "", 1)
    fullwidth = "".join(chr(ord(char) + 0xFEE0) for char in value)
    return quote(fullwidth, safe="") if spelling == "encoded_fullwidth" else fullwidth


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("spelling", FORK_SPELLINGS)
@pytest.mark.parametrize("trailer", FORK_TRAILERS)
def test_card253_s08_fix1_hidden_fork_trailer(case, write, spelling, trailer):
    c = case
    value = continuity_schema.fork_id("u2", "hidden-stream")
    hidden = stored(c, value, capture.PRIVATE_WING)
    assert not c.h.gateway.handoff.recall.can_read(c.member, hidden.id)
    citation = navigable(fork_spelling(value, spelling) + trailer)
    if write:
        result = invoke(c, "member", "kg_add", **args(c, "source_url", citation))
        assert result == {"success": False, "error": "Source not found"}, (
            "protected fork with trailing character accepted",
            result,
        )
        assert c.kg.query_entity("SpellingFact") == []
        assert invoke(c, "member", "kg_query", entity="SpellingFact")["count"] == 0
    else:
        seed(c, args(c, "source_url", citation))
        row = fact(c)
        assert "origin_link" not in row, "protected fork with trailing character leaked"
        assert all("origin_link" not in source for source in row["sources"])


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("spelling", FORK_SPELLINGS)
@pytest.mark.parametrize("trailer", FORK_TRAILERS)
def test_card253_s08_fix1_readable_fork_trailer(case, write, spelling, trailer):
    c = case
    value = continuity_schema.fork_id("u1", "readable-stream")
    row = stored(c, value)
    assert c.h.gateway.handoff.recall.can_read(c.member, row.id)
    check_citation(c, "member", "source_file", fork_spelling(value, spelling) + trailer, write)


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("trailer", FORK_TRAILERS)
def test_card253_s08_fix1_head_canonical_fork_trailer(case, write, trailer):
    value = continuity_schema.fork_id("u1", "readable-stream")
    stored(case, value)
    check_citation(case, "member", "source_file", value + trailer, write)


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("source_url", "https://example.test/capturing-notes:codex:v2"),
        ("source_file", "notes/capturing-notes:codex:v2"),
        ("source_file", "Captured: Codex: session notes"),
    ],
)
def test_card253_s08_fix1_ordinary_shape_preserved(case, write, field, value):
    check_citation(case, "member", field, value, write)


@pytest.mark.parametrize(
    "trailer", [chr(i) for i in range(32, 127)] + ["\u00a0", "\u2009", "λ", "）"]
)
@pytest.mark.parametrize("spelling", ["canonical", *FORK_SPELLINGS])
def test_card253_s08_fix1_head_fork_any_trailer(trailer, spelling):
    from commonplace.handoff.provenance_refs import refs

    value = continuity_schema.fork_id("u1", "readable-stream")
    assert refs(fork_spelling(value, spelling) + trailer).tokens == (value,)


def embedded_source(c, kind):
    if kind == "fork":
        return continuity_schema.fork_id("u2", "embedded-stream")
    if kind == "capture":
        return capture._identity(
            replace(c.owner, user_id="u2"),
            {"source": "codex", "thread_id": "opaque7"},
        )[1]
    return import_grant.grant_id("u2", "codex", "opaque7")


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("field", ["source_file", "source_url"])
@pytest.mark.parametrize("separator", ["", ";"], ids=["adjacent", "semicolon"])
@pytest.mark.parametrize("spelling", ["canonical", *FORK_SPELLINGS])
@pytest.mark.parametrize("kind", ["capture", "fork", "import"])
def test_card253_s08_fix2_hidden_overlap(case, write, field, separator, spelling, kind):
    c = case
    prefix = continuity_schema.fork_id("u1", "readable-stream")
    readable = stored(c, prefix)
    source = embedded_source(c, kind)
    hidden = stored(c, source, capture.PRIVATE_WING)
    assert c.h.gateway.handoff.recall.can_read(c.member, readable.id)
    assert not c.h.gateway.handoff.recall.can_read(c.member, hidden.id)
    value = prefix + separator + fork_spelling(source, spelling)
    citation = navigable(value) if field == "source_url" else value
    if write:
        result = invoke(c, "member", "kg_add", **args(c, field, citation))
        assert result == {"success": False, "error": "Source not found"}, (
            "embedded protected reference accepted",
            result,
        )
        assert c.kg.query_entity("SpellingFact") == []
        assert invoke(c, "member", "kg_query", entity="SpellingFact")["count"] == 0
    else:
        seed(c, args(c, field, citation))
        row = fact(c)
        public_field = "origin_link" if field == "source_url" else field
        assert public_field not in row, "embedded protected reference leaked"
        assert all(public_field not in item for item in row["sources"])


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("field", ["source_file", "source_url"])
@pytest.mark.parametrize("spelling", FORK_SPELLINGS)
@pytest.mark.parametrize("kind", ["capture", "fork", "import"])
def test_card253_s08_fix2_readable_overlap(case, write, field, spelling, kind):
    c = case
    prefix = continuity_schema.fork_id("u1", "readable-stream")
    source = embedded_source(c, kind)
    if kind == "import":
        # Private imports use the real authenticated filer, as canonical guards
        # do; a shared wing alone never grants another filer's import authority.
        source = import_grant.grant_id(c.member.user_id, "codex", "opaque7")
    for value in (prefix, source):
        readable = stored(c, value)
        assert c.h.gateway.handoff.recall.can_read(c.member, readable.id)
    # Neither spelling generates an ordinary token on develop. These are exact
    # public byte-preservation guards, including non-boundary embedded shadows.
    value = fork_spelling(prefix, "fullwidth") + fork_spelling(source, spelling)
    check_citation(c, "member", field, navigable(value) if field == "source_url" else value, write)


@pytest.mark.parametrize("separator", ["", ";", ",", ".", ":", "-", "_", "z"])
@pytest.mark.parametrize("spelling", ["canonical", *FORK_SPELLINGS])
@pytest.mark.parametrize("prefix_spelling", ["canonical", "space", "fullwidth"])
def test_card253_s08_head_overlapping_candidates(separator, spelling, prefix_spelling):
    from commonplace.handoff.provenance_refs import refs

    first = continuity_schema.fork_id("u1", "first-stream")
    second = continuity_schema.fork_id("u2", "second-stream")
    # Later candidates start inside earlier matches. All need proof, including
    # starts after letters, punctuation and compatibility characters.
    for last in ("capture:u3:codex:opaque7", "import-private:u4:codex:opaque8"):
        value = (
            fork_spelling(first, prefix_spelling)
            + separator
            + fork_spelling(second, spelling)
            + separator
            + fork_spelling(last, spelling)
        )
        tokens = refs(value).tokens
        assert first in tokens and second in tokens and last in tokens


@pytest.mark.timing
@pytest.mark.skipif(sys.platform != "linux", reason="Linux process resource budget")
@pytest.mark.parametrize(
    "pattern",
    ["capture:u1:codex:", "=capturea", "ｃａｐｔｕｒｅ：ｕ１：ｃｏｄｅｘ：", "mixed_threads"],
)
@pytest.mark.parametrize("size", [16_384, 1_048_576])
def test_card253_s08_fix3_scan_budget(pattern, size, request):
    # Serial, informational measurements only. Performance never gates behavior;
    # the deterministic candidate-work guard below carries the cap contract.
    script = """
import json, resource, sys, time
from pathlib import Path
from commonplace.handoff.provenance_refs import REFUSE, refs
resource.setrlimit(resource.RLIMIT_AS, (256 * 1024**2, 256 * 1024**2))
def peak_kib():
    # /proc resets on exec; getrusage can inherit the pytest parent's high-water
    # mark on Linux and would make the budget depend on test order.
    fields = Path("/proc/self/status").read_text().splitlines()
    return int(next(line for line in fields if line.startswith("VmHWM:")).split()[1])
pattern, size = sys.argv[1], int(sys.argv[2])
value = (pattern * (size // len(pattern) + 1))[:size]
if pattern == "mixed_threads":
    half = size // 2
    prefix = "capture:u1:codex:"
    value = (prefix * (half // len(prefix) + 1))[:half]
    value += ("thr_a" * (size // 5 + 1))[:size-half]
started = time.perf_counter()
try:
    result = refs(value)
except MemoryError:
    print(json.dumps({"failure": "memory budget exhausted", "seconds": time.perf_counter()-started,
                      "peak_kib": peak_kib()}))
    sys.exit(1)
print(json.dumps({"seconds": time.perf_counter()-started,
                  "peak_kib": peak_kib(),
                  "refused": result is REFUSE}))
"""
    try:
        run = subprocess.run(
            [sys.executable, "-c", script, pattern, str(size)],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        request.node.user_properties.extend(
            [("process_deadline_seconds", 300), ("failure", "informational hang guard reached")]
        )
        return
    assert run.stdout, run.stderr
    measurement = json.loads(run.stdout)
    measurement["exit_code"] = run.returncode
    measurement["within_scan_budget"] = measurement["seconds"] < 3
    measurement["within_rss_budget"] = measurement["peak_kib"] < 128 * 1024
    request.node.user_properties.extend(measurement.items())


@pytest.mark.parametrize(
    "write,field,kind",
    [
        pytest.param(write, field, kind, id=f"{kind}-{field}-{'write' if write else 'read'}")
        for kind in ("oversize", "overlimit", "nonboundary")
        for field in ("source_file", "source_url")
        for write in (False, True)
        # A token-free URL over 2048 characters is already an invalid write on
        # the base. Its read and the file write independently prove the cap.
        if not (kind == "overlimit" and field == "source_url" and write)
    ],
)
def test_card253_s08_fix3_public_refusal(case, write, field, kind):
    c = case
    prefix = continuity_schema.fork_id("u1", "readable-stream")
    readable = stored(c, prefix)
    assert c.h.gateway.handoff.recall.can_read(c.member, readable.id)
    if kind == "nonboundary":
        value = "notes." + prefix + ")"
    elif kind == "overlimit":
        value = "notes/" + "x" * 16_385
    else:
        # Real hidden protected spelling beyond the cap must never be truncated
        # away and accepted. Readable primary fact text remains available.
        source = embedded_source(c, "capture")
        hidden = stored(c, source, capture.PRIVATE_WING)
        assert not c.h.gateway.handoff.recall.can_read(c.member, hidden.id)
        value = "x" * 16_385 + "/" + source
    citation = navigable(value) if field == "source_url" else value
    if write:
        result = invoke(c, "member", "kg_add", **args(c, field, citation))
        error = (
            "Source not found"
            if kind == "nonboundary"
            else f"{field}: citation length limit of 16384 Unicode characters exceeded "
            "(decoded or NFKC-normalized)"
        )
        assert result == {"success": False, "error": error}
        assert c.kg.query_entity("SpellingFact") == []
        assert invoke(c, "member", "kg_query", entity="SpellingFact")["count"] == 0
    else:
        seed(c, args(c, field, citation))
        row = fact(c)
        public_field = "origin_link" if field == "source_url" else field
        assert public_field not in row
        assert all(public_field not in item for item in row["sources"])
        with c.kg._lock:
            # Authorization drops only the projected citation, never storage.
            raw = c.kg._conn().execute("SELECT * FROM triples").fetchone()
            assert raw[public_field] == citation


@pytest.mark.parametrize("encoded", [False, True])
def test_card253_s08_fix3_length_boundary(encoded):
    from commonplace.handoff.provenance_refs import REFUSE, refs

    for length in (16_383, 16_384):
        value = "x" * length
        citation = "%78" * length if encoded else value
        assert refs(citation).text == value
        assert refs(citation).tokens == ()
    value = "x" * 16_385
    assert refs("%78" * 16_385 if encoded else value) is REFUSE


def test_card253_s08_fix3_normalized_limit():
    from commonplace.handoff.provenance_refs import REFUSE, refs

    # U+FB03 expands to three authorization characters. Bound that copy too,
    # independently of the decoded-length limit (both raw inputs are shorter).
    assert refs("ﬃ" * 5_461).tokens == ()
    assert refs("ﬃ" * 5_462) is REFUSE


@pytest.mark.parametrize("write", [False, True], ids=["read", "write"])
@pytest.mark.parametrize("field", ["source_file", "source_url"])
def test_card253_s08_fix3_readable_limit(case, write, field):
    # A long ordinary citation at the exact cap is still byte-preserved.
    value = "https://example.test/" if field == "source_url" else "notes/"
    limit = 2_048 if field == "source_url" else 16_384
    value += "x" * (limit - len(value))
    check_citation(case, "member", field, value, write)


@pytest.mark.parametrize("role", ["member", "owner"])
@pytest.mark.parametrize(
    "field", ["source_thread", "source_file", "source_url", "source_closet", "source_drawer_id"]
)
@pytest.mark.parametrize("kind", ["plain", "encoded", "normalized"])
def test_card253_s08_fix4_length_error_preserves_facts(case, role, field, kind):
    c = case
    seed(c, args(c, "source_file", "notes/original.md"))
    before = fact(c, role)
    with c.kg._lock:
        original = {
            table: [tuple(row) for row in c.kg._conn().execute(f"SELECT * FROM {table}")]
            for table in ("triples", "fact_sources")
        }
    value = {
        "plain": "x" * 16_385,
        "encoded": "%78" * 16_385,
        "normalized": "ﬃ" * 5_462,
    }[kind]
    pending = args(c, field, value)
    pending["object"] = "Pending  exact words: ﬃ %78 capture:u1:codex:verbatim"
    result = invoke(c, role, "kg_add", **pending)
    assert result == {
        "success": False,
        "error": f"{field}: citation length limit of 16384 Unicode characters exceeded "
        "(decoded or NFKC-normalized)",
    }
    assert "mempalace_kg_add" not in c.reached
    assert pending["object"] == "Pending  exact words: ﬃ %78 capture:u1:codex:verbatim"
    assert fact(c, role) == before
    with c.kg._lock:
        for table, rows in original.items():
            assert [tuple(row) for row in c.kg._conn().execute(f"SELECT * FROM {table}")] == rows


@pytest.mark.parametrize("kind", ["plain", "encoded", "normalized", "hidden_suffix"])
def test_card253_s08_fix4_over_limit_does_no_candidate_work(monkeypatch, kind):
    from commonplace.handoff import provenance_refs as p

    value = {
        "plain": "x" * 1_048_576,
        "encoded": "%78" * 16_385,
        "normalized": "ﬃ" * 5_462,
        "hidden_suffix": "x" * 16_385 + "/capture:u2:codex:hidden",
    }[kind]
    calls = []

    def candidates(*args):
        calls.append(args)
        return iter(())

    monkeypatch.setattr(p, "_shadows", candidates)
    monkeypatch.setattr(p, "_private_matches", candidates)
    assert p.refs(value) is p.REFUSE
    assert calls == []
