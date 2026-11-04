"""Public owner consumption through the documented ACL import/history route."""

import json
from uuid import uuid4

from commonplace import urls
from tests.test_commonplace_import_grant import fresh as fresh, native


def test_public_owner_reads_imported_native_archive_via_history_route(fresh, monkeypatch):
    """Obtain the history ID from HTTP, then read exact text with the owner's cookie."""
    monkeypatch.setenv("COMMONPLACE_APP_URL", "http://127.0.0.1")
    client, mailbox = fresh()
    monkeypatch.setattr(urls, "AGENT_URL", urls.APP_URL)
    identity = "native-" + uuid4().hex
    cookie = client.signin(mailbox, identity + "@example.test")
    owner = client.me(cookie)
    # Read-only identity checks; signup and all data writes use public HTTP.
    assert not client.db.user(owner["user"])["is_admin"]
    assert client.db.workspace(owner["workspace"])["personal_of"] == owner["user"]
    assert client.db.role(owner["workspace"], owner["user"]) == "owner"

    text = f"  {identity} café 🦉\r\nKeep  every word.\t"
    transcript = native(text)
    status, _, raw = client.request(
        "/api/import",
        "POST",
        cookie=cookie,
        headers={"Origin": urls.APP_URL},
        data={
            "source": "claude-code",
            "threads": [{"thread_id": identity, "format": "claude-code", "transcript": transcript}],
        },
    )
    assert status == 200
    report = json.loads(raw)
    assert report["errors"] == 0 and report["imported"] == 1
    (result,) = report["results"]
    assert result["status"] == "imported"

    # The documented read uses only the ID returned by import, never a store lookup.
    status, _, raw = client.request(
        f"/api/threads/{result['stored_thread_id']}/messages?start=0&end=199",
        cookie=cookie,
    )
    assert status == 200
    archive, message = json.loads(raw)
    assert archive["seq"] == 0 and archive["raw"] == transcript
    assert message["seq"] == 1 and message["role"] == "user"
    assert message["text"] == text
