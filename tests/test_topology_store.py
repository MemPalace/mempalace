"""The topology store: upsert and read with provenance, expiry, conflicts, secret refusal.

Every name here is synthetic. Time is a datetime the tests move; nothing sleeps.
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from commonplace.policy import Policy
from commonplace.topology import TOPOLOGY_WING, Observer, SecretRefused, TopologyError, model, store

T0 = datetime(2026, 11, 11, 10, 0, 0, tzinfo=timezone.utc)
RUNNER = Observer("runner", "mach_0123456789abcdef")
AGENT = Observer("agent", "conn_agent_one")
OWNER = Observer("owner", "usr_owner")
EVIDENCE = "uname -a: Linux web1 6.8.0 x86_64"


@pytest.fixture
def db(tmp_path):
    topo = store.TopologyStore(tmp_path / "ws_one")
    yield topo
    topo.close()


@pytest.fixture
def other(tmp_path):
    topo = store.TopologyStore(tmp_path / "ws_two")
    yield topo
    topo.close()


def later(**delta):
    return T0 + timedelta(**delta)


def node(db, fields, observer=RUNNER, evidence=EVIDENCE, key="web1", now=T0, **write):
    return store.upsert_node(
        db, key, fields, observer=observer, evidence=evidence, now=now, **write
    )


def stored(db, table="topology_facts", where="1"):
    with db._read() as conn:
        return (
            [] if conn is None else conn.execute(f"SELECT 1 FROM {table} WHERE {where}").fetchall()
        )


# ── Upsert and read back ──────────────────────────────────────────────────
def test_node_reads_back_with_provenance_and_expiry(db):
    result = node(
        db,
        {
            "kind": "server",
            "name": "Web one",
            "role": "app host",
            "hostname": "web1.example.test",
            "aliases": ["web1", "app"],
            "runner_id": "mach_0123456789abcdef",
            "wings": ["shared"],
            "os": "Linux 6.8",
            "resources": {"cores": 8, "memory_gb": 32, "cpu": "x86_64"},
        },
    )
    assert {item["action"] for item in result} == {"added"}

    read = store.get_node(db, "web1", now=T0)
    assert read["key"] == "web1" and read["wing"] == TOPOLOGY_WING
    assert read["values"]["kind"] == "server"
    assert read["values"]["aliases"] == ["app", "web1"]
    assert read["values"]["resources"] == {"cores": 8, "memory_gb": 32, "cpu": "x86_64"}
    assert read["stale"] is False and read["conflicted"] is False
    field = read["fields"]["os"]
    assert field["value"] == "Linux 6.8" and field["stale"] is False
    assert field["observed_at"] == "2026-11-11T10:00:00Z"
    assert field["expires_at"] == model.stamp(later(seconds=model.DEFAULT_TTL_SECONDS))
    assert field["provenance"] == [
        {
            "observer": {"kind": "runner", "id": "mach_0123456789abcdef"},
            "evidence": EVIDENCE,
            "first_observed_at": "2026-11-11T10:00:00Z",
            "observed_at": "2026-11-11T10:00:00Z",
            "expires_at": field["expires_at"],
            "stale": False,
            "seen_count": 1,
        }
    ]


def test_link_and_route_read_back_as_a_chain(db):
    node(db, {"kind": "website"}, key="example.test")
    node(db, {"kind": "cloud", "name": "DNS and CDN"}, key="cdn")
    node(db, {"kind": "server"}, key="web1")
    store.upsert_link(
        db, "example.test", "cdn", "dns", observer=AGENT, evidence="dig example.test", now=T0
    )
    store.upsert_link(
        db,
        "cdn",
        "web1",
        "reverse_proxy",
        {"status": "up", "port": 8080, "alias": "web1"},
        observer=AGENT,
        evidence="proxy config: example.test -> web1:8080",
        now=T0,
    )
    hops = ["example.test>cdn:dns", "cdn>web1:reverse_proxy", "web1>app:hosts"]
    store.upsert_route(db, "example.test", hops, observer=AGENT, evidence="traced by hand", now=T0)

    link = store.get_link(db, "cdn", "web1", "reverse_proxy", now=T0)
    assert (link["from"], link["to"], link["method"]) == ("cdn", "web1", "reverse_proxy")
    assert link["values"] == {
        "method": "reverse_proxy",
        "status": "up",
        "port": 8080,
        "alias": "web1",
    }

    route = store.get_route(db, "example.test", now=T0)
    assert [hop["link"] for hop in route["hops"]] == hops
    assert [(hop["known"], hop["status"]) for hop in route["hops"]] == [
        (True, "unknown"),
        (True, "up"),
        (False, "unknown"),
    ]
    graph = store.read_graph(db, now=T0)
    assert sorted(item["key"] for item in graph["nodes"]) == ["cdn", "example.test", "web1"]
    assert len(graph["links"]) == 2 and len(graph["routes"]) == 1 and graph["conflicts"] == []


def test_same_value_again_refreshes_and_keeps_both_pieces_of_evidence(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    again = node(db, {"os": "Linux 6.8"}, now=later(days=1))
    other = node(db, {"os": "Linux 6.8"}, AGENT, "ssh web1 uname -r", now=later(days=2))
    assert [item["action"] for item in again + other] == ["refreshed", "refreshed"]

    field = store.get_node(db, "web1", now=later(days=2))["fields"]["os"]
    assert field["observed_at"] == model.stamp(later(days=2))
    assert [
        (seen["observer"]["kind"], seen["evidence"], seen["seen_count"])
        for seen in field["provenance"]
    ] == [
        ("agent", "ssh web1 uname -r", 1),
        ("runner", EVIDENCE, 2),
    ]
    assert len(stored(db, where="predicate='os'")) == 1


def test_first_write_for_a_node_needs_its_kind(db):
    with pytest.raises(TopologyError, match="needs its kind"):
        node(db, {"name": "Web one"})
    node(db, {"kind": "vm"})
    node(db, {"name": "Web one"})
    assert store.get_node(db, "web1", now=T0)["values"] == {"kind": "vm", "name": "Web one"}


def test_a_refused_item_stores_nothing_from_the_same_write(db):
    with pytest.raises(TopologyError):
        store.upsert(
            db,
            [
                {"node": "web1", "fields": {"kind": "server"}},
                {"link": ["web1", "web1", "direct"]},
            ],
            observer=RUNNER,
            evidence=EVIDENCE,
            now=T0,
        )
    assert store.read_graph(db, now=T0)["nodes"] == []


@pytest.mark.parametrize(
    "items, message",
    [
        ([{"node": "Web One", "fields": {"kind": "server"}}], "node key"),
        ([{"node": "web1", "fields": {"kind": "mainframe"}}], "must be one of"),
        ([{"node": "web1", "fields": {"kind": "server", "colour": "red"}}], "unknown field"),
        (
            [{"node": "web1", "fields": {"kind": "server", "resources": {"ram": 1}}}],
            "unknown field",
        ),
        (
            [{"node": "web1", "fields": {"kind": "server", "runner_id": "web1"}}],
            "runner machine id",
        ),
        ([{"link": ["a", "b", "carrier-pigeon"]}], "must be one of"),
        ([{"link": ["a", "b", "direct"], "fields": {"port": 70000}}], "port number"),
        ([{"link": ["a", "b", "direct"], "fields": {"method": "relay"}}], "match its id"),
        ([{"route": "site.test", "hops": ["other>b:dns"]}], "one chain"),
        ([{"route": "site.test", "hops": ["site.test>b:dns", "c>d:proxy"]}], "one chain"),
        ([{"route": "site.test", "hops": []}], "1 to"),
        ([{"node": "web1", "fields": {"kind": "server"}, "extra": 1}], "unknown field"),
        ([], "at least one item"),
    ],
)
def test_values_outside_the_allow_list_are_refused(db, items, message):
    with pytest.raises(TopologyError, match=message):
        store.upsert(db, items, observer=RUNNER, evidence=EVIDENCE, now=T0)
    assert stored(db) == []


# ── Expiry ────────────────────────────────────────────────────────────────
def test_a_stale_fact_fades_from_values_but_stays_readable(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    node(db, {"os": "Linux 6.8"}, ttl_seconds=3600)
    store.upsert_link(
        db,
        "laptop",
        "web1",
        "tailscale",
        {"status": "up"},
        observer=RUNNER,
        evidence="ping ok",
        now=T0,
    )

    fresh = store.get_link(db, "laptop", "web1", "tailscale", now=later(minutes=14))
    assert fresh["values"]["status"] == "up"
    faded = store.get_link(db, "laptop", "web1", "tailscale", now=later(minutes=15))
    assert "status" not in faded["values"] and faded["values"]["method"] == "tailscale"
    assert faded["fields"]["status"]["stale"] is True
    assert faded["fields"]["status"]["value"] == "up"
    assert faded["stale"] is False

    gone = store.get_node(db, "web1", now=later(days=31))
    assert gone["values"] == {} and gone["stale"] is True
    assert gone["fields"]["kind"]["provenance"][0]["stale"] is True


def test_a_route_hop_over_a_stale_link_says_so(db):
    store.upsert_link(
        db, "site.test", "web1", "dns", {"status": "up"}, observer=AGENT, evidence="dig", now=T0
    )
    store.upsert_route(
        db, "site.test", ["site.test>web1:dns"], observer=AGENT, evidence="dig", now=T0
    )
    hop = store.get_route(db, "site.test", now=later(days=40))["hops"][0]
    assert hop == {
        "link": "site.test>web1:dns",
        "from": "site.test",
        "to": "web1",
        "method": "dns",
        "known": True,
        "stale": True,
        "status": "unknown",
    }


def test_a_fresh_observation_replaces_an_expired_value_without_a_conflict(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    result = node(db, {"os": "Linux 7.0"}, AGENT, "ssh web1 uname -r", now=later(days=31))
    assert result[0]["action"] == "superseded" and result[0]["previous"] == "Linux 6.8"
    assert store.list_conflicts(db, now=later(days=31)) == []


@pytest.mark.parametrize("ttl", [0, 59, model.MAX_TTL_SECONDS + 1, 1.5, True, "60"])
def test_ttl_outside_the_limits_is_refused(db, ttl):
    with pytest.raises(TopologyError, match="ttl_seconds"):
        node(db, {"kind": "server"}, ttl_seconds=ttl)


# ── Conflicts ─────────────────────────────────────────────────────────────
def test_two_observers_disagreeing_is_a_conflict_not_an_overwrite(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    result = node(db, {"os": "Windows 11"}, AGENT, "nmap guess: Windows", now=later(hours=1))
    assert result[0]["action"] == "conflict" and result[0]["current"] == "Linux 6.8"

    read = store.get_node(db, "web1", now=later(hours=1))
    assert read["values"]["os"] == "Linux 6.8" and read["conflicted"] is True
    contender = read["fields"]["os"]["conflicts"][0]
    assert contender["value"] == "Windows 11"
    assert contender["conflict_id"] == result[0]["conflict_id"]
    assert contender["provenance"][0]["evidence"] == "nmap guess: Windows"

    conflict = store.list_conflicts(db, now=later(hours=1))[0]
    assert conflict["state"] == "open" and conflict["predicate"] == "os"
    assert conflict["current"]["value"] == "Linux 6.8"
    assert conflict["current"]["provenance"][0]["observer"]["kind"] == "runner"
    assert conflict["contender"]["value"] == "Windows 11"
    assert store.read_graph(db, now=later(hours=1))["conflicts"] == [conflict]

    # The same disagreement again is the same record with one more observation.
    node(db, {"os": "Windows 11"}, AGENT, "second scan", now=later(hours=2))
    assert len(store.list_conflicts(db, now=later(hours=2))) == 1


def test_an_observer_changing_its_own_value_supersedes_and_keeps_history(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    result = node(db, {"os": "Linux 7.0"}, evidence="uname after upgrade", now=later(days=1))
    assert result[0]["action"] == "superseded"
    assert store.get_node(db, "web1", now=later(days=1))["values"]["os"] == "Linux 7.0"

    past = store.history(db, "node:web1", predicate="os", now=later(days=1))
    assert [(fact["value"], fact["state"]) for fact in past] == [
        ("Linux 7.0", "current"),
        ("Linux 6.8", "superseded"),
    ]
    assert past[1]["superseded_by"] == past[0]["fact_id"]
    assert past[1]["provenance"][0]["evidence"] == EVIDENCE


def test_accepting_a_conflict_makes_the_contender_current(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    conflict_id = node(db, {"os": "Linux 7.0"}, AGENT, "ssh uname")[0]["conflict_id"]
    resolved = store.resolve_conflict(
        db, conflict_id, accept=True, actor="usr_owner", now=later(hours=1)
    )
    assert resolved["state"] == "accepted" and resolved["resolved_by"] == "usr_owner"
    read = store.get_node(db, "web1", now=later(hours=1))
    assert read["values"]["os"] == "Linux 7.0" and read["conflicted"] is False
    states = [fact["state"] for fact in store.history(db, "node:web1", predicate="os")]
    assert sorted(states) == ["current", "superseded"]
    with pytest.raises(TopologyError, match="already accepted"):
        store.resolve_conflict(db, conflict_id, accept=False, actor="usr_owner")


def test_rejecting_a_conflict_keeps_the_old_fact(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    conflict_id = node(db, {"os": "Linux 7.0"}, AGENT, "ssh uname")[0]["conflict_id"]
    store.resolve_conflict(db, conflict_id, accept=False, actor="usr_owner", now=T0)
    read = store.get_node(db, "web1", now=T0)
    assert read["values"]["os"] == "Linux 6.8" and read["conflicted"] is False
    assert store.list_conflicts(db, open_only=False, now=T0)[0]["state"] == "rejected"


def test_an_observer_that_comes_round_withdraws_its_own_contender(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    node(db, {"os": "Linux 7.0"}, AGENT, "ssh uname")
    node(db, {"os": "Linux 6.8"}, AGENT, "ssh uname, read properly", now=later(hours=1))
    assert store.list_conflicts(db, now=later(hours=1)) == []
    assert store.list_conflicts(db, open_only=False)[0]["state"] == "withdrawn"


def test_replace_is_the_authoritative_write_and_closes_conflicts(db):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    node(db, {"os": "Linux 7.0"}, AGENT, "ssh uname")
    result = node(db, {"os": "FreeBSD 14"}, OWNER, "approved in the app", replace=True)
    assert result[0]["action"] == "superseded"
    read = store.get_node(db, "web1", now=T0)
    assert read["values"]["os"] == "FreeBSD 14" and read["conflicted"] is False
    assert store.list_conflicts(db, open_only=False)[0]["state"] == "replaced"
    assert len(store.history(db, "node:web1", predicate="os")) == 3


def test_retract_takes_a_subject_off_the_map_and_keeps_history(db):
    node(db, {"kind": "server", "os": "Linux 6.8", "role": "app host"})
    with pytest.raises(TopologyError, match="whole subject"):
        store.retract(db, "node:web1", predicate="kind", observer=OWNER, evidence="gone")
    assert store.retract(db, "node:web1", predicate="role", observer=OWNER, evidence="no role") == 1
    assert "role" not in store.get_node(db, "web1", now=T0)["fields"]
    assert store.retract(db, "node:web1", observer=OWNER, evidence="decommissioned", now=T0) == 2
    assert store.get_node(db, "web1", now=T0) is None
    past = store.history(db, "node:web1", predicate="kind")
    assert past[0]["state"] == "retracted"
    assert past[0]["provenance"][0]["evidence"] == "decommissioned"
    assert node(db, {"kind": "vm"})[0]["action"] == "added"


# ── Secret filter ─────────────────────────────────────────────────────────
PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----"
GITHUB_TOKEN = "ghp_" + "a1B2" * 9
SECRETS = [
    PRIVATE_KEY,
    GITHUB_TOKEN,
    "password=correct-horse-battery",
    "the root password is swordfish",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
    "https://deploy:hunter2222@git.example.test/repo.git",
    "api_token: 9f8e7d6c5b4a32109f8e7d6c5b4a3210",
]


@pytest.mark.parametrize("secret", SECRETS)
def test_secret_looking_evidence_is_refused_with_a_clear_message(db, secret):
    with pytest.raises(SecretRefused) as refused:
        node(db, {"kind": "server"}, evidence=f"cat config:\n{secret}")
    message = str(refused.value)
    assert message.startswith("no: evidence looks like it holds a secret (")
    assert "never keys, tokens, passwords or private key material" in message
    assert secret not in message and secret.split()[-1] not in message
    assert stored(db) == []
    assert stored(db, "topology_observations") == []


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "server", "name": GITHUB_TOKEN},
        {"kind": "server", "role": "login with password: correct-horse-battery"},
        {"kind": "server", "hardware": "the root password is swordfish"},
        {"kind": "server", "resources": {"gpu": "token=9f8e7d6c5b4a32109f8e7d6c5b4a3210"}},
        {"kind": "server", "password": "anything"},
        {"kind": "server", "ssh_private_key": "anything"},
        {"kind": "server", "resources": {"api_key": "anything"}},
    ],
)
def test_secret_looking_fields_are_refused(db, fields):
    with pytest.raises(SecretRefused, match="never keys, tokens, passwords"):
        node(db, fields)
    assert stored(db) == []


def test_what_the_card_allows_is_not_mistaken_for_a_secret(db):
    node(
        db,
        {
            "kind": "server",
            "hostname": "web1.example.test",
            "aliases": ["web1"],
            "tailscale_name": "web1.tailnet.example.test",
            "runner_id": "mach_0123456789abcdef",
            "wings": ["shared", "topology"],
            "hardware": "8 cores, 32 GB, sha256 fingerprint not stored",
        },
        evidence="Host web1\n  HostName web1.example.test\n  IdentityFile ~/.ssh/id_ed25519",
    )
    assert store.get_node(db, "web1", now=T0)["values"]["aliases"] == ["web1"]


def test_an_observer_id_cannot_smuggle_a_secret():
    with pytest.raises(TopologyError):
        Observer("agent", GITHUB_TOKEN + " x")
    with pytest.raises(SecretRefused):
        Observer("agent", GITHUB_TOKEN)
    with pytest.raises(TopologyError, match="observer kind"):
        Observer("stranger", "conn_1")


def test_retract_evidence_goes_through_the_same_filter(db):
    node(db, {"kind": "server"})
    with pytest.raises(SecretRefused):
        store.retract(db, "node:web1", observer=OWNER, evidence=SECRETS[2])
    assert store.get_node(db, "web1", now=T0) is not None


def test_facts_for_checks_a_proposal_without_storing_it():
    assert model.facts_for([{"link": ["a", "b", "relay"], "fields": {"via": "hub"}}]) == [
        ("link:a>b:relay", "via", "hub"),
        ("link:a>b:relay", "method", "relay"),
    ]
    with pytest.raises(SecretRefused):
        model.facts_for([{"node": "a", "fields": {"kind": "pc", "name": SECRETS[2]}}])


# ── Workspaces, old workspaces, wing grants ───────────────────────────────
def test_an_old_workspace_without_the_wing_keeps_working(tmp_path, other):
    directory = tmp_path / "ws_two"
    directory.mkdir()
    (directory / "boards.sqlite3").write_bytes(b"existing workspace data")
    assert store.read_graph(other, now=T0) == {
        "wing": "topology",
        "nodes": [],
        "links": [],
        "routes": [],
        "conflicts": [],
    }
    assert store.get_node(other, "web1", now=T0) is None
    assert store.get_link(other, "a", "b", "direct", now=T0) is None
    assert store.get_route(other, "site.test", now=T0) is None
    with pytest.raises(TopologyError, match="site"):
        store.get_route(other, "Not A Site", now=T0)
    assert store.list_conflicts(other, open_only=False) == []
    assert store.history(other, "node:web1") == []
    # Reading never creates the file or touches what the workspace already holds.
    assert sorted(path.name for path in directory.iterdir()) == ["boards.sqlite3"]

    store.upsert_node(other, "web1", {"kind": "pc"}, observer=AGENT, evidence="seen", now=T0)
    assert (directory / store.FILENAME).exists()
    assert (directory / "boards.sqlite3").read_bytes() == b"existing workspace data"
    assert store.get_node(other, "web1", now=T0)["values"] == {"kind": "pc"}


def test_workspaces_never_see_each_others_facts(db, other):
    node(db, {"kind": "mac"})
    store.upsert_node(other, "web1", {"kind": "pc"}, observer=AGENT, evidence="seen", now=T0)
    assert store.get_node(db, "web1", now=T0)["fields"]["kind"]["conflicts"] == []
    assert store.get_node(db, "web1", now=T0)["values"]["kind"] == "mac"
    assert store.get_node(other, "web1", now=T0)["values"]["kind"] == "pc"
    conflict_id = node(db, {"kind": "vm"}, AGENT, "looks virtual")[0]["conflict_id"]
    with pytest.raises(store.NotFound):
        store.resolve_conflict(other, conflict_id, accept=True, actor="usr_owner")


def test_facts_survive_reopening_and_a_second_schema_pass(tmp_path):
    first = store.TopologyStore(tmp_path / "ws")
    store.upsert_node(first, "web1", {"kind": "server"}, observer=RUNNER, evidence=EVIDENCE, now=T0)
    first.close()
    second = store.TopologyStore(lambda: tmp_path / "ws")
    try:
        assert store.get_node(second, "web1", now=T0)["values"] == {"kind": "server"}
        assert (
            store.upsert_node(
                second, "web1", {"kind": "server"}, observer=RUNNER, evidence=EVIDENCE, now=T0
            )[0]["action"]
            == "refreshed"
        )
    finally:
        second.close()


def test_open_topology_follows_the_workspace_directory(tmp_path):
    class Supervisor:
        generation = "a"

        def workspace_dir(self, workspace_id):
            return tmp_path / self.generation / workspace_id

    class Gateway:
        supervisor = Supervisor()

    gateway = Gateway()
    topo = store.open_topology(gateway, "ws_one")
    assert store.open_topology(gateway, "ws_one") is topo
    assert store.open_topology(gateway, "ws_two") is not topo
    store.upsert_node(topo, "web1", {"kind": "server"}, observer=RUNNER, evidence=EVIDENCE, now=T0)
    assert (tmp_path / "a" / "ws_one" / store.FILENAME).exists()
    gateway.supervisor.generation = "b"
    assert store.get_node(topo, "web1", now=T0) is None
    gateway.supervisor.generation = "a"
    assert store.get_node(topo, "web1", now=T0)["values"] == {"kind": "server"}
    for opened in gateway._topology_stores.values():
        opened.close()


def test_a_crash_inside_a_write_leaves_the_previous_facts(db, monkeypatch):
    node(db, {"kind": "server", "os": "Linux 6.8"})
    real = store._observe
    calls = []

    def crash(*args):
        calls.append(1)
        if len(calls) == 2:
            raise sqlite3.OperationalError("disk I/O error")
        return real(*args)

    monkeypatch.setattr(store, "_observe", crash)
    with pytest.raises(sqlite3.OperationalError):
        node(db, {"os": "Linux 7.0", "role": "app host"}, now=later(hours=1))
    monkeypatch.undo()
    read = store.get_node(db, "web1", now=later(hours=1))
    assert read["values"] == {"kind": "server", "os": "Linux 6.8"}
    assert len(store.history(db, "node:web1")) == 2


def test_the_wing_is_shared_through_ordinary_wing_grants():
    scopes = ["memory:read", "memory:write"]
    granted = Policy.build(
        scopes=scopes, read_wings=[TOPOLOGY_WING], write_wings=[TOPOLOGY_WING], trust="trusted"
    )
    other = Policy.build(
        scopes=scopes, read_wings=["shared"], write_wings=["shared"], trust="trusted"
    )
    assert granted.can_read_wing(TOPOLOGY_WING) and granted.can_write_wing(TOPOLOGY_WING)
    assert not other.can_read_wing(TOPOLOGY_WING) and not other.can_write_wing(TOPOLOGY_WING)
