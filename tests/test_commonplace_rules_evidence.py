"""Rule evidence: events, counters, authorization and storage safety through the real gateway."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from commonplace import sessions
from commonplace.extensions import ToolError
from commonplace.rules import RuleStore, evidence, open_store
from tests import test_commonplace_rules as rules_tests
from tests._commonplace_helpers import hub_fixture  # noqa: F401
from tests.test_commonplace_boards import api, mcp
from tests.test_commonplace_rules import publish, rule, service

env = rules_tests.env


def feedback(hub, token, **args):
    ok, text = mcp(hub, token, "rules_feedback", **args)
    return ok, (json.loads(text) if ok else text)


def counts(conn):
    return {
        (r["rule_id"], r["kind"]): r["count"]
        for r in conn.execute("SELECT rule_id,kind,count(*) AS count FROM rule_events GROUP BY 1,2")
    }


def test_write_and_read_back_over_mcp(hub, env):
    publish(env)
    ok, first = feedback(hub, env.token, rule="one", kind="followed")
    assert ok and first["key"] == "one" and first["total"] == 1
    assert first["events"][0]["kind"] == "followed" and first["events"][0]["source"] == "agent"
    assert first["events"][0]["actor"] == env.agent.actor
    note = 'Skipped it: "quoted"\nsecond line'
    assert feedback(hub, env.token, rule="one", kind="violated", note=note)[0]
    ok, read = feedback(hub, env.token, rule="one")
    assert ok and read["total"] == 2
    assert [e["kind"] for e in read["events"]] == ["violated", "followed"]
    assert read["events"][0]["note"] == note
    assert {k: v["count"] for k, v in read["kinds"].items()} == {"followed": 1, "violated": 1}
    ok, overview = feedback(hub, env.token)
    assert ok and overview == {"rules": [{"key": "one", "total": 2, "kinds": read["kinds"]}]}


def test_invalid_feedback_is_refused_and_leaves_nothing(hub, env):
    publish(env)
    for args in (
        dict(rule="one", kind="bogus"),
        dict(rule="one", kind="note"),
        dict(rule="one", kind="followed", note="x" * 501),
        dict(rule="missing", kind="followed"),
        dict(kind="followed"),
        dict(rule="one", note="orphan"),
    ):
        ok, text = feedback(hub, env.token, **args)
        assert not ok and text.startswith("no:"), args
    with env.owner.store.read() as conn:
        assert conn.execute("SELECT count(*) FROM rule_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM rule_stats").fetchone()[0] == 0


def test_refuses_readers_people_and_untrusted_writers_get_only_their_own(hub, env):
    current = publish(env)
    reader = hub.token("reader", profile="slim", scopes=["memory:read"])
    ok, text = feedback(hub, reader, rule="one", kind="followed")
    assert not ok and "Unknown tool" in text or "memory:write" in text
    with pytest.raises(ToolError, match="agent connections only"):
        env.owner.rules_feedback(rule="one", kind="followed")
    with pytest.raises(ToolError, match="memory:write"):
        service(hub, hub.gateway.authenticate(f"Bearer {reader}")).rules_feedback(
            rule="one", kind="followed"
        )
    with env.owner.store.read() as conn:
        assert conn.execute("SELECT count(*) FROM rule_events").fetchone()[0] == 0
    assert env.owner.feedback(current["id"])["total"] == 0


def test_agent_cannot_reach_another_agents_rule_or_notes(hub, env):
    publish(env)
    shared = env.worker.rules_publish([rule(key="shared", text="For all")], agent="*")["rules"][0]
    other = hub.owner_token("other")
    assert not feedback(hub, other, rule="one", kind="followed")[0]
    assert feedback(hub, env.token, rule="one", kind="followed", note="mine")[0]
    # Shared rules take feedback from several agents; each reads back only its own notes,
    # while the counters are the rule's.
    assert feedback(hub, env.token, rule="shared", kind="helped", note="from agent")[0]
    ok, theirs = feedback(hub, other, rule="shared", kind="conflict", note="from other")
    assert ok and theirs["total"] == 2
    assert [e["note"] for e in theirs["events"]] == ["from other"]
    ok, mine = feedback(hub, env.token, rule="shared")
    assert [e["note"] for e in mine["events"]] == ["from agent"]
    assert env.owner.feedback(shared["id"])["events"][0]["note"] == "from other"
    assert len(env.owner.feedback(shared["id"])["events"]) == 2


def test_other_workspace_is_isolated(hub, env, tmp_path):
    publish(env)
    assert feedback(hub, env.token, rule="one", kind="followed")[0]
    second = hub.db.create_workspace("second", hub.admin)
    token = hub.token(
        "agent",
        workspace=second,
        profile="full",
        scopes=["memory:read", "memory:write"],
        trust="trusted",
        read_wings=["*"],
        write_wings=["*"],
    )
    assert not feedback(hub, token, rule="one", kind="followed")[0]
    assert feedback(hub, token) == (True, {"rules": []})
    ok, text = mcp(hub, token, "rules_publish", rules=[rule()])
    assert ok
    ok, theirs = feedback(hub, token, rule="one")
    assert ok and theirs["total"] == 0 and theirs["events"] == []
    # A bare store elsewhere has its own empty evidence tables.
    with open_store(tmp_path / "bare").read() as conn:
        assert conn.execute("SELECT count(*) FROM rule_stats").fetchone()[0] == 0


def test_stats_are_derived_from_events_and_survive_pruning(hub, env):
    current = publish(env)
    kinds = ["followed"] * 7 + ["violated"] * 3 + ["unclear"]
    for kind in kinds:
        assert feedback(hub, env.token, rule="one", kind=kind)[0]
    with env.owner.store.read() as conn:
        assert counts(conn) == {
            (current["id"], "followed"): 7,
            (current["id"], "violated"): 3,
            (current["id"], "unclear"): 1,
        }
        stats = evidence.stats(conn)[current["id"]]
    assert stats["total"] == 11
    assert {k: v["count"] for k, v in stats["kinds"].items()} == {
        "followed": 7,
        "violated": 3,
        "unclear": 1,
    }
    assert stats["kinds"]["followed"]["first_at"] <= stats["kinds"]["followed"]["last_at"]
    # Overflow one writer: events stay bounded, the counters keep the full history.
    for _ in range(evidence.KEEP_PER_ACTOR + 5):
        assert feedback(hub, env.token, rule="one", kind="helped")[0]
    with env.owner.store.read() as conn:
        assert (
            conn.execute("SELECT count(*) FROM rule_events").fetchone()[0]
            == evidence.KEEP_PER_ACTOR
        )
        assert (
            evidence.stats(conn)[current["id"]]["kinds"]["helped"]["count"]
            == evidence.KEEP_PER_ACTOR + 5
        )
        assert evidence.stats(conn)[current["id"]]["total"] == 11 + evidence.KEEP_PER_ACTOR + 5


def test_a_noisy_writer_does_not_evict_other_writers(hub, env):
    shared = env.worker.rules_publish([rule(key="shared", text="For all")], agent="*")["rules"][0]
    other = hub.owner_token("other")
    assert feedback(hub, other, rule="shared", kind="unclear", note="keep me")[0]
    for _ in range(evidence.KEEP_PER_ACTOR + 20):
        assert feedback(hub, env.token, rule="shared", kind="followed")[0]
    with env.owner.store.read() as conn:
        stored = [
            r["note"]
            for r in conn.execute("SELECT note FROM rule_events WHERE rule_id=?", (shared["id"],))
        ]
        # The page is the newest READ_LIMIT; the older writer's event is stored, not evicted.
        assert "keep me" in stored and len(stored) == evidence.KEEP_PER_ACTOR + 1
    assert len(env.owner.feedback(shared["id"])["events"]) == evidence.READ_LIMIT


def test_per_rule_cap_and_events_are_append_only(hub, env):
    current = publish(env)
    with env.worker.transaction() as conn:
        row = conn.execute("SELECT * FROM rules").fetchone()
        for i in range(evidence.KEEP_PER_RULE + 10):
            evidence.record(conn, row, "followed", source="system", actor=f"guard:{i}")
    with env.owner.store.read() as conn:
        assert (
            conn.execute("SELECT count(*) FROM rule_events").fetchone()[0] == evidence.KEEP_PER_RULE
        )
        assert evidence.stats(conn)[current["id"]]["total"] == evidence.KEEP_PER_RULE + 10
    with env.owner.store.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE rule_events SET note='changed'")
    with pytest.raises(ToolError, match="invalid evidence"):
        with env.worker.transaction() as conn:
            evidence.record(conn, row, "Bad Kind", source="system", actor="x")


def test_failed_write_rolls_back_event_and_counter(hub, env):
    publish(env)
    with pytest.raises(RuntimeError):
        with env.worker.transaction() as conn:
            evidence.record(
                conn,
                conn.execute("SELECT * FROM rules").fetchone(),
                "followed",
                source="system",
                actor="x",
            )
            raise RuntimeError("crash before commit")
    with env.owner.store.read() as conn:
        assert conn.execute("SELECT count(*) FROM rule_events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM rule_stats").fetchone()[0] == 0


def test_evidence_does_not_touch_rules_or_their_sequence(hub, env):
    current = publish(env)
    before = env.owner.store.seq
    assert feedback(hub, env.token, rule="one", kind="outdated")[0]
    assert env.owner.store.seq == before
    assert env.worker.rules_get()["rules"] == [current]


def test_old_database_without_the_tables_keeps_working(hub, tmp_path):
    directory = tmp_path / "old"
    directory.mkdir()
    schema = Path(RuleStore.__init__.__code__.co_filename).with_name("schema.sql").read_text()
    with closing(sqlite3.connect(directory / "rules.sqlite3")) as conn:
        conn.executescript(schema)
        conn.execute(
            "INSERT INTO rules(agent,key,text,kind,scope,status,source,seq,created_at,updated_at,updated_by) "
            "VALUES ('*','old','Old rule','limit','global','accepted','owner',1,'t','t','u')"
        )
        conn.commit()
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name IN ('rule_events','rule_stats')"
        ).fetchone()
    store = RuleStore(directory)
    with store.transaction() as conn:
        row = conn.execute("SELECT * FROM rules WHERE key='old'").fetchone()
        assert row["text"] == "Old rule"
        evidence.record(conn, row, "followed", source="system", actor="guard:x")
    with store.read() as conn:
        assert evidence.stats(conn)[row["id"]]["total"] == 1
        assert conn.execute("SELECT text FROM rules WHERE key='old'").fetchone()[0] == "Old rule"
    RuleStore(directory)  # reopening is idempotent


def test_rest_owner_writes_people_read_agents_refused(hub, env):
    current = publish(env)
    path = f"/api/rules/{current['id']}/feedback"
    status, written = api(hub, env.cookie, "POST", path, {"kind": "outdated", "note": "Old"})
    assert status == 201, written
    assert written["total"] == 1 and written["events"][0]["source"] == "person"
    assert written["events"][0]["actor"].startswith("user:")
    status, read = api(hub, env.cookie, "GET", path)
    assert status == 200 and read["events"] == written["events"]
    status, listing = api(hub, env.cookie, "GET", "/api/rules/feedback")
    assert status == 200 and listing["stats"][str(current["id"])]["total"] == 1
    assert api(hub, env.cookie, "POST", path, {"kind": "bogus"})[0] == 400
    assert api(hub, env.cookie, "POST", path, {"kind": "note", "extra": 1})[0] == 400
    assert api(hub, env.cookie, "POST", path, {})[0] == 400
    assert api(hub, env.cookie, "GET", "/api/rules/999/feedback")[0] == 404
    assert api(hub, env.cookie, "GET", "/api/rules/x/feedback")[0] == 404
    # Agents have the MCP tool; REST is for people. Members read, only owners write.
    for method, url, body in (
        ("GET", path, None),
        ("POST", path, {"kind": "followed"}),
        ("GET", "/api/rules/feedback", None),
    ):
        assert api(hub, env.token, method, url, body)[0] == 403
    assert api(hub, None, "GET", path)[0] in (401, 403)
    member_id = hub.db.create_user("member@example.test")
    hub.db.add_member(hub.workspace, member_id, role="member", actor=hub.admin)
    member = sessions.person_principal(hub.db, member_id, hub.workspace)
    link = sessions.create_login_link(hub.db, member.user_email, hub.workspace)
    cookie = {"Cookie": f"{sessions.COOKIE}={sessions.redeem_login_link(hub.db, link)}"}
    assert api(hub, cookie, "GET", path)[0] == 200
    assert api(hub, cookie, "POST", path, {"kind": "followed"})[0] == 403
    assert api(hub, env.cookie, "GET", path)[1]["total"] == 1
