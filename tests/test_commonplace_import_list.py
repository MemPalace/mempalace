"""Owner discovery of grant imports through public HTTP, using synthetic data."""

import base64
import json
from urllib.parse import quote

import pytest

from commonplace import imports
from commonplace.handoff.thread_store import ThreadStore
from commonplace.import_sources import parse_import
from tests.test_commonplace_email_auth import fresh as fresh
from tests.test_commonplace_import_grant import IMPORT_SCOPES, body, journey as journey, native


def listing(journey, path="/api/imports", **kwargs):
    status, _, raw = journey.request(path, **kwargs)
    assert status == 200
    return json.loads(raw)


def batch(*identities):
    data = body(identities[0], "  café 🦉\r\nExact words.\t")
    data["threads"] = [dict(data["threads"][0], thread_id=value) for value in identities]
    return data


def test_owner_lists_own_grant_imports(journey):
    data = batch("S", "T")
    token = journey.pat()["token"]
    assert journey.upload(data, token=token)["accepted"] == 2
    page = listing(journey, owner=journey.owner)
    assert page["next"] is None
    assert len(page["imports"]) == 2
    for row, submitted in zip(page["imports"], data["threads"]):
        assert row["source"] == "claude-code"
        assert row["thread_id"] == submitted["thread_id"]
        assert row["stored_messages"] == 1
        assert row["conflict_of"] is None
        assert row["created_at"]
        assert row["stored_thread_id"].startswith("thr_")
        status, _, raw = journey.request(
            f"/api/threads/{row['stored_thread_id']}/messages?start=0&end=199",
            owner=journey.owner,
        )
        assert status == 200
        archive, message = json.loads(raw)
        assert archive["raw"] == submitted["transcript"]
        assert message["text"] == "  café 🦉\r\nExact words.\t"


def test_import_list_refuses_agent_tokens(journey):
    grant = journey.pat()["token"]
    _, oauth = journey.oauth(scope=IMPORT_SCOPES)
    plain = journey.pat(grant=False)["token"]
    for token in (grant, oauth["access_token"], plain):
        status, _, raw = journey.request("/api/imports", token=token)
        assert status == 403
        assert json.loads(raw) == {"error": "Sign in to list your imports"}
    status, _, _ = journey.request("/api/imports")
    assert status == 401


def test_import_list_shows_no_foreign_rows(journey):
    a = journey.owner
    b = journey.signup("b@example.test")
    for owner, identity in ((a, "A"), (b, "B")):
        token = journey.pat(owner=owner)["token"]
        assert journey.upload(body(identity), token=token)["accepted"] == 1
    assert journey.upload(body("ACL"), owner=a)["imported"] == 1
    for owner, identity in ((a, "A"), (b, "B")):
        rows = listing(journey, owner=owner)["imports"]
        assert [row["thread_id"] for row in rows] == [identity]
    status, _, _ = journey.request("/app/logout", "POST", owner=a, data={})
    assert status == 204
    status, _, _ = journey.request("/api/imports", owner=a)
    assert status == 401


def test_list_source_range_boundaries(tmp_path):
    assert hasattr(ThreadStore, "list_source_range")
    store = ThreadStore(str(tmp_path / "threads.sqlite3"))
    try:
        identities = [
            "import-private:usr_a:S",
            "import-private:usr_a:T",
            "import-private:usr_a0:S",
            "import-private:usr_a;x:S",
            "import-private:usrZa:S",
            "import-private:USR_a:S",
            "import:S",
        ]
        messages = parse_import("claude-code", native(), "claude-code")
        for identity in identities:
            store.store("A", identity, messages, actor="synthetic")
        store.store("B", identities[0], messages, actor="synthetic")
        rows = store.list_source_range("A", "import-private:usr_a:", limit=100)
        assert [r.source_thread_id for r in rows] == identities[:2]
        after = None
        seen = []
        for _ in range(4):
            page = store.list_source_range("A", "import-private:usr_a:", after=after, limit=1)
            if not page:
                break
            seen.extend(r.id for r in page)
            after = (page[-1].source_thread_id, page[-1].id)
        assert seen == [r.id for r in rows]
        with pytest.raises(ValueError, match="colon"):
            store.list_source_range("A", "import-private:usr_a", limit=1)
        assert (
            store.list_source_range("A", "import-private:usr_a:", after=("z", "z"), limit=2) == []
        )
    finally:
        store.close()


def test_import_list_paging_filters_after_page(journey, monkeypatch):
    assert hasattr(imports, "readable")
    # fresh enables plans and journey pins the clock: one POST avoids cadence refusal.
    assert journey.upload(batch("A", "B", "C", "D"), token=journey.pat()["token"])["accepted"] == 4
    rows = listing(journey, owner=journey.owner)["imports"]
    allowed = {r["stored_thread_id"] for r in rows[::2]}
    original = imports.readable
    monkeypatch.setattr(imports, "readable", lambda p, r: r.id in allowed and original(p, r))
    for limit in (1, 2):
        cursor = None
        seen = []
        for _ in range(6):
            path = f"/api/imports?limit={limit}"
            if cursor:
                path += "&cursor=" + quote(cursor)
            page = listing(journey, path, owner=journey.owner)
            seen.extend(r["stored_thread_id"] for r in page["imports"])
            cursor = page["next"]
            if cursor is None:
                break
        assert cursor is None
        assert len(seen) == len(set(seen))
        assert set(seen) == allowed


def test_import_list_rejects_invalid_paging(journey):
    malformed = [
        "!",
        base64.urlsafe_b64encode(json.dumps([{}, []]).encode()).decode(),
        base64.urlsafe_b64encode(b'["one"]').decode(),
        base64.urlsafe_b64encode(b'["\\ud800", "x"]').decode(),
    ]
    for query in ["limit=0", "limit=101", "limit=abc"] + ["cursor=" + quote(c) for c in malformed]:
        status, _, raw = journey.request("/api/imports?" + query, owner=journey.owner)
        assert status == 400
        assert "error" in json.loads(raw)
